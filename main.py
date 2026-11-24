
import os
import sys
import time
import sqlite3
import shutil
from pathlib import Path

import certifi
import requests

BASE_URL = "https://platform-api2.max.ru"
TOKEN = os.getenv("MAX_TOKEN", "").strip()

DATA_DIR = Path(os.getenv("DATA_DIR", "/app/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "stats.db"

# Корневой и промежуточный сертификаты Минцифры
# должны лежать рядом с main.py.

ROOT_CERT = Path(__file__).resolve().parent / "russian_trusted_root_ca_pem.crt"
SUB_CERT = Path(__file__).resolve().parent / "russian_trusted_sub_ca_pem.crt"
CA_BUNDLE = DATA_DIR / "max-ca-bundle.pem"

if not TOKEN:
    print("ОШИБКА: не задана переменная окружения MAX_TOKEN", flush=True)
    sys.exit(1)

if not ROOT_CERT.is_file():
    print("ОШИБКА: не найден корневой сертификат!", flush=True)
    sys.exit(1)

if not SUB_CERT.is_file():
    print("ОШИБКА: не найден промежуточный сертификат!", flush=True)
    sys.exit(1)

try:
    shutil.copyfile(certifi.where(), CA_BUNDLE)

    with CA_BUNDLE.open("ab") as bundle:
        bundle.write(b"\n")
        bundle.write(ROOT_CERT.read_bytes())
        bundle.write(b"\n")
        bundle.write(SUB_CERT.read_bytes())
        bundle.write(b"\n")

except OSError as exc:
    print(f"ОШИБКА подготовки SSL: {exc}", flush=True)
    sys.exit(1)

HEADERS = {
    "Authorization": TOKEN,
    "Content-Type": "application/json"
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
SESSION.verify = str(CA_BUNDLE)

print("Оба SSL-сертификата подключены.", flush=True)


# Перенос старой базы данных
old_db = Path.cwd() / "stats.db"

if old_db.exists() and old_db.resolve() != DB_PATH.resolve() and not DB_PATH.exists():
    try:
        shutil.copy2(old_db, DB_PATH)
        print(f"Старая база перенесена в {DB_PATH}", flush=True)

    except Exception as exc:
        print(f"Не удалось перенести старую базу: {exc}", flush=True)


# Подключение SQLite
db = sqlite3.connect(DB_PATH, check_same_thread=False)

db.execute("""
CREATE TABLE IF NOT EXISTS additions (
    chat_id TEXT NOT NULL,
    new_user_id TEXT NOT NULL,
    inviter_id TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
    PRIMARY KEY (chat_id, new_user_id)
)
""")

db.commit()


def api_request(method, path, **kwargs):
    response = SESSION.request(
        method,
        f"{BASE_URL}{path}",
        timeout=kwargs.pop("timeout", 40),
        **kwargs,
    )

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


def command_name(raw_text):
    if not raw_text:
        return ""

    first = raw_text.strip().split()[0]
    return first.split("@", 1)[0].lower()


def handle(update):
    event = update.get("update_type")

    # Обработка добавления пользователей
    if event == "user_added":
        inviter_id = update.get("inviter_id")
        new_user_id = (update.get("user") or {}).get("user_id")
        chat_id = update.get("chat_id")

        if (
            inviter_id is not None
            and new_user_id is not None
            and chat_id is not None
            and not update.get("is_channel", False)
        ):
            try:
                cursor = db.execute(
                    "INSERT OR IGNORE INTO additions "
                    "(chat_id, new_user_id, inviter_id) VALUES (?, ?, ?)",
                    (str(chat_id), str(new_user_id), str(inviter_id)),
                )

                db.commit()

                if cursor.rowcount == 1:
                    print(
                        f"Засчитано добавление: inviter_id={inviter_id}",
                        flush=True,
                    )

            except sqlite3.Error as exc:
                print(f"Ошибка записи в SQLite: {exc}", flush=True)

        return

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

    cmd = command_name(message_text)

    # ТОП участников
    if cmd in ("/top", "/топ"):
        rows = db.execute("""
            SELECT inviter_id, COUNT(*) AS total
            FROM additions
            WHERE chat_id = ?
            GROUP BY inviter_id
            ORDER BY total DESC, inviter_id ASC
            LIMIT 10
        """, (str(chat_id),)).fetchall()

        if not rows:
            send(
                chat_id,
                "🏆 Пока нет засчитанных приглашений.\n"
                "Учитываются только участники, добавленные напрямую.",
            )
            return

        result = [
            "🏆 Рейтинг участников по личным добавлениям:",
            ""
        ]

        for index, (user_id, total) in enumerate(rows, 1):
            result.append(f"{index}. ID {user_id} — {total}")

        send(chat_id, "\n".join(result))
        return

    # Личная статистика
    if cmd in ("/my", "/моястатистика"):
        count = db.execute("""
            SELECT COUNT(*)
            FROM additions
            WHERE chat_id = ? AND inviter_id = ?
        """, (str(chat_id), str(sender_id))).fetchone()[0]

        send(chat_id, f"👥 Ты лично добавил(а): {count}")
        return

    # Помощь
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

    print(f"Бот запускается. База данных: {DB_PATH}", flush=True)

    # Проверяем подключение к MAX API
    me = api_request("GET", "/me")

    print(
        f"MAX API подключён: {me.get('first_name', 'бот')}",
        flush=True
    )

    while True:
        try:
            params = {
                "timeout": 30,
                "types": "message_created,user_added",
            }

            if marker is not None:
                params["marker"] = marker

            data = api_request(
                "GET",
                "/updates",
                params=params,
                timeout=40,
            )

            # Обработка полученных событий
            for update in data.get("updates", []):
                print(
                    f"ПОЛУЧЕНО СОБЫТИЕ: {update}",
                    flush=True
                )

                try:
                    handle(update)

                except Exception as exc:
                    print(
                        f"Ошибка обработки события: {exc}",
                        flush=True
                    )

            marker = data.get("marker", marker)

        except requests.exceptions.SSLError as exc:
            print(f"SSL-ОШИБКА: {exc}", flush=True)
            time.sleep(10)

        except requests.HTTPError as exc:
            status = (
                exc.response.status_code
                if exc.response is not None
                else "?"
            )

            print(
                f"HTTP-ошибка MAX API ({status}): {exc}",
                flush=True
            )

            if status == 401:
                print(
                    "Проверьте переменную MAX_TOKEN в настройках Bothost.",
                    flush=True,
                )

            time.sleep(10)

        except requests.RequestException as exc:
            print(
                f"Ошибка HTTP-запроса к MAX API: {exc}",
                flush=True
            )
            time.sleep(10)

        except Exception as exc:
            print(
                f"Ошибка цикла обновлений: {exc}",
                flush=True
            )
            time.sleep(5)


if __name__ == "__main__":
    run()
