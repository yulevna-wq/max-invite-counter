import os
import time
import sqlite3
import requests

TOKEN = os.getenv("MAX_TOKEN")
BASE = "https://platform-api2.max.ru"
HEADERS = {"Authorization": TOKEN}

db = sqlite3.connect("stats.db", check_same_thread=False)
db.execute("""
CREATE TABLE IF NOT EXISTS additions (
    chat_id TEXT,
    new_user_id TEXT,
    inviter_id TEXT,
    PRIMARY KEY(chat_id, new_user_id)
)
""")
db.commit()

marker = None

def send(chat_id, text):
    requests.post(
        f"{BASE}/messages",
        headers=HEADERS,
        params={"chat_id": chat_id},
        json={"text": text},
        timeout=20
    )

def handle(update):
    event = update.get("update_type")

    # Подсчет добавленных участников
    if event == "user_added":
        inviter = update.get("inviter_id")
        new_user = update.get("user", {}).get("user_id")
        chat_id = update.get("chat_id")

        if inviter and new_user and chat_id and not update.get("is_channel"):
            try:
                db.execute(
                    "INSERT INTO additions VALUES (?, ?, ?)",
                    (str(chat_id), str(new_user), str(inviter))
                )
                db.commit()
                print("Засчитано добавление:", inviter)
            except sqlite3.IntegrityError:
                pass

    # Команды
    if event == "message_created":
        msg = update.get("message", {})
        text = (msg.get("body", {}).get("text") or "").strip()
        chat_id = msg.get("recipient", {}).get("chat_id")
        sender = msg.get("sender", {}).get("user_id")

        if not chat_id or not sender:
            return

        if text.startswith("/top"):
            rows = db.execute("""
                SELECT inviter_id, COUNT(*)
                FROM additions
                WHERE chat_id=?
                GROUP BY inviter_id
                ORDER BY COUNT(*) DESC
                LIMIT 10
            """, (str(chat_id),)).fetchall()

            if not rows:
                send(chat_id, "Пока никто не добавил участников.")
                return

            result = "🏆 Рейтинг добавивших:\n\n"
            for i, (user, count) in enumerate(rows, 1):
                result += f"{i}. ID {user} — {count}\n"
            send(chat_id, result)

        elif text.startswith("/my"):
            count = db.execute("""
                SELECT COUNT(*) FROM additions
                WHERE chat_id=? AND inviter_id=?
            """, (str(chat_id), str(sender))).fetchone()[0]

            send(chat_id, f"👥 Ты добавил: {count}")

def run():
    global marker

    while True:
        try:
            params = {"timeout": 30}
            if marker is not None:
                params["marker"] = marker

            response = requests.get(
                f"{BASE}/updates",
                headers=HEADERS,
                params=params,
                timeout=40
            )
            response.raise_for_status()
            data = response.json()

            for update in data.get("updates", []):
                handle(update)

            marker = data.get("marker", marker)

        except Exception as e:
            print("Ошибка:", e)
            time.sleep(5)

if __name__ == "__main__":
    run()
