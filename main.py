"""
Счётчик приглашений для мессенджера MAX (long polling).

Правила зачёта:
  +1 — тому, кто ДОБАВИЛ участника (user_added с inviter_id);
   0 — вошедший по ссылке (inviter_id = null);
   0 — боты, самодобавление, каналы;
   0 — повторный вход уже засчитанного человека.

Хранение: /app/data/stats.db (на Bothost переживает обновления из Git).
Запуск:  MAX_TOKEN=<токен> python main.py
Рядом с файлом: russian_trusted_root_ca_pem.crt (корень Минцифры).
"""

import os
import sys
import time
import shutil
import sqlite3
from pathlib import Path

import certifi
import requests

BASE_URL = "https://platform-api2.max.ru"
TOKEN = os.getenv("MAX_TOKEN", "").strip()

# Персистентные данные — только здесь.
DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DB_PATH = DATA_DIR / "stats.db"

# Корневой сертификат Минцифры (промежуточный сервер присылает сам).
HERE = Path(__file__).resolve().parent
ROOT_CERT = HERE / "russian_trusted_root_ca_pem.crt"
# Бандл собираем во временном каталоге: /app/data переживает деплои,
# и там навсегда осталась бы устаревшая копия certifi.
CA_BUNDLE = Path(os.getenv("TMPDIR", "/tmp")) / "max-ca-bundle.pem"

POLL_TIMEOUT = 30          # секунды, допустимо 0..90
HTTP_TIMEOUT = 40          # должен быть больше POLL_TIMEOUT
POLL_PAUSE = 0.4           # минимум 300 мс между запросами /updates
UPDATE_TYPES = "message_created,user_added"

BOT_COMMANDS = [
    {"name": "top", "description": "Рейтинг участников по добавлениям"},
    {"name": "my", "description": "Сколько участников добавил лично ты"},
    {"name": "help", "description": "Как считается статистика"},
]


if not TOKEN:
    print("ОШИБКА: не задана переменная окружения MAX_TOKEN", flush=True)
    sys.exit(1)

if not ROOT_CERT.is_file():
    print(f"ОШИБКА: не найден корневой сертификат {ROOT_CERT}", flush=True)
    sys.exit(1)

try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(certifi.where(), CA_BUNDLE)
    with CA_BUNDLE.open("ab") as bundle:
        bundle.write(b"\n")
        bundle.write(ROOT_CERT.read_bytes())
        bundle.write(b"\n")
except OSError as exc:
    print(f"ОШИБКА подготовки SSL/хранилища: {exc}", flush=True)
    sys.exit(1)

HEADERS = {"Authorization": TOKEN, "Content-Type": "application/json"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.verify = str(CA_BUNDLE)
print("SSL: корневой сертификат Минцифры подключён.", flush=True)

# Перенос старой базы, если она лежала рядом с main.py.
old_db = Path.cwd() / "stats.db"
if old_db.exists() and old_db.resolve() != DB_PATH.resolve() and not DB_PATH.exists():
    try:
        shutil.copy2(old_db, DB_PATH)
        print(f"Старая база перенесена в {DB_PATH}", flush=True)
    except Exception as exc:
        print(f"Не удалось перенести старую базу: {exc}", flush=True)

db = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
db.execute(
    """
    CREATE TABLE IF NOT EXISTS additions (
        chat_id TEXT NOT NULL,
        new_user_id TEXT NOT NULL,
        inviter_id TEXT NOT NULL,
        created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
        PRIMARY KEY (chat_id, new_user_id)
    )
    """
)
db.execute(
    "CREATE TABLE IF NOT EXISTS names (user_id TEXT PRIMARY KEY, name TEXT NOT NULL)"
)
db.commit()


def api_request(method, path, **kwargs):
    response = SESSION.request(
        method,
        f"{BASE_URL}{path}",
        timeout=kwargs.pop("timeout", HTTP_TIMEOUT),
        **kwargs,
    )

    # Тело ошибки MAX важнее статус-кода: chat.denied, add.participant.privacy и т.д.
    if response.status_code >= 400:
        print(f"MAX API {response.status_code}: {response.text[:500]}", flush=True)

    response.raise_for_status()

    data = response.json() if response.content else {}
    if isinstance(data, dict) and data.get("success") is False:
        raise RuntimeError(
            f"MAX API: {data.get('code', 'error')}: {data.get('message', '')}"
        )
    return data


def send(chat_id, message_text):
    if not chat_id:
        return
    api_request(
        "POST",
        "/messages",
        params={"chat_id": chat_id},
        json={"text": message_text[:4000]},
    )


def setup_commands():
    """Подсказки команд в интерфейсе MAX. Не критично, если не получится."""
    try:
        api_request("PATCH", "/me/commands", json={"commands": BOT_COMMANDS})
        print("Команды зарегистрированы: /top, /my, /help", flush=True)
    except Exception as exc:
        print(f"Не удалось зарегистрировать команды (не критично): {exc}", flush=True)


def remember(user):
    """Кэшируем имя, чтобы в рейтинге были люди, а не ID."""
    if not user:
        return
    user_id = user.get("user_id")
    if user_id is None:
        return
    name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])).strip()
    if not name:
        return
    try:
        db.execute(
            "INSERT OR REPLACE INTO names (user_id, name) VALUES (?, ?)",
            (str(user_id), name),
        )
        db.commit()
    except sqlite3.Error as exc:
        print(f"Ошибка записи имени: {exc}", flush=True)


def display_name(user_id):
    row = db.execute(
        "SELECT name FROM names WHERE user_id = ?", (str(user_id),)
    ).fetchone()
    return row[0] if row else f"ID {user_id}"


def refresh_names(chat_id, limit_pages=5):
    """Добираем имена тех, кто ещё ничего не писал. Бот должен быть админом."""
    marker = None
    try:
        for _ in range(limit_pages):
            params = {"count": 100}
            if marker is not None:
                params["marker"] = marker
            data = api_request("GET", f"/chats/{chat_id}/members", params=params)
            for member in data.get("members", []):
                remember(member)
            marker = data.get("marker")
            if not marker:
                break
    except Exception as exc:
        print(f"Не удалось получить участников (бот админ?): {exc}", flush=True)


def command_name(raw_text):
    if not raw_text:
        return ""
    first = raw_text.strip().split()[0]
    return first.split("@", 1)[0].lower()


def handle(update):
    event = update.get("update_type")

    # --- Зачёт добавлений ---
    if event == "user_added":
        chat_id = update.get("chat_id")
        new_user = update.get("user") or {}
        new_user_id = new_user.get("user_id")
        inviter_id = update.get("inviter_id")

        remember(new_user)

        if (
            inviter_id is None                      # зашёл по ссылке — не считаем
            or new_user_id is None
            or chat_id is None
            or update.get("is_channel", False)      # каналы не считаем
            or new_user.get("is_bot", False)        # ботов не считаем
            or str(inviter_id) == str(new_user_id)  # сам себя не считаем
        ):
            return

        try:
            cursor = db.execute(
                """
                INSERT OR IGNORE INTO additions (chat_id, new_user_id, inviter_id)
                VALUES (?, ?, ?)
                """,
                (str(chat_id), str(new_user_id), str(inviter_id)),
            )
            db.commit()
        except sqlite3.Error as exc:
            print(f"Ошибка записи в SQLite: {exc}", flush=True)
            return

        if cursor.rowcount == 1:
            print(
                f"Засчитано добавление: chat={chat_id} inviter={inviter_id}",
                flush=True,
            )
        return

    # --- Команды ---
    if event != "message_created":
        return

    message = update.get("message") or {}
    body = message.get("body") or {}
    message_text = (body.get("text") or "").strip()
    recipient = message.get("recipient") or {}
    chat_id = recipient.get("chat_id") or update.get("chat_id")
    sender = message.get("sender") or {}
    sender_id = sender.get("user_id")

    if not chat_id or sender_id is None:
        return

    remember(sender)
    if sender.get("is_bot", False):
        return

    cmd = command_name(message_text)

    if cmd in ("/top", "/топ"):
        rows = db.execute(
            """
            SELECT inviter_id, COUNT(*) AS total
            FROM additions
            WHERE chat_id = ?
            GROUP BY inviter_id
            ORDER BY total DESC, inviter_id ASC
            LIMIT 10
            """,
            (str(chat_id),),
        ).fetchall()

        if not rows:
            send(
                chat_id,
                "🏆 Пока нет засчитанных приглашений.\n"
                "Учитываются только участники, добавленные напрямую.",
            )
            return

        unknown = any(
            db.execute("SELECT 1 FROM names WHERE user_id = ?", (r[0],)).fetchone() is None
            for r in rows
        )
        if unknown:
            refresh_names(chat_id)

        lines = ["🏆 Рейтинг участников по личным добавлениям:", ""]
        for index, (user_id, total) in enumerate(rows, 1):
            lines.append(f"{index}. {display_name(user_id)} — {total}")
        send(chat_id, "\n".join(lines))
        return

    if cmd in ("/my", "/моястатистика"):
        count = db.execute(
            "SELECT COUNT(*) FROM additions WHERE chat_id = ? AND inviter_id = ?",
            (str(chat_id), str(sender_id)),
        ).fetchone()[0]
        send(chat_id, f"👥 {display_name(sender_id)}, ты лично добавил(а): {count}")
        return

    if cmd in ("/help", "/помощь"):
        send(
            chat_id,
            "Команды бота:\n"
            "/my — твои личные приглашения\n"
            "/top — рейтинг участников\n\n"
            "В зачёт идут только прямые добавления. "
            "Вступления по ссылке не учитываются.",
        )
        return


def run():
    marker = None
    errors = 0

    print(f"Бот запускается. База данных: {DB_PATH}", flush=True)
    me = api_request("GET", "/me")
    print(f"MAX API подключён: {me.get('first_name', 'бот')}", flush=True)
    setup_commands()

    while True:
        try:
            params = {
                "timeout": POLL_TIMEOUT,
                "types": UPDATE_TYPES,
                "limit": 100,
            }
            if marker is not None:
                params["marker"] = marker

            data = api_request("GET", "/updates", params=params, timeout=HTTP_TIMEOUT)

            for update in data.get("updates", []):
                print(
                    f"Событие: {update.get('update_type')} chat={update.get('chat_id')}",
                    flush=True,
                )
                try:
                    handle(update)
                except Exception as exc:
                    print(f"Ошибка обработки события: {exc}", flush=True)

            # ВАЖНО: при marker=null позиция не теряется
            marker = data.get("marker") or marker
            errors = 0
            time.sleep(POLL_PAUSE)

        except requests.exceptions.SSLError as exc:
            print(f"SSL-ОШИБКА: {exc}", flush=True)
            time.sleep(10)

        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            print(f"HTTP-ошибка MAX API ({status}): {exc}", flush=True)

            if status == 401:
                print("Проверьте переменную MAX_TOKEN в настройках Bothost.", flush=True)
            if status == 400:
                marker = None  # протухший указатель — начинаем заново

            errors += 1
            time.sleep(min(60, 5 * (2 ** min(errors, 4))))

        except requests.exceptions.RequestException as exc:
            print(f"Ошибка HTTP-запроса к MAX API: {exc}", flush=True)
            errors += 1
            time.sleep(min(60, 5 * (2 ** min(errors, 4))))

        except Exception as exc:
            print(f"Ошибка цикла обновлений: {exc}", flush=True)
            time.sleep(5)


if __name__ == "__main__":
    run()
