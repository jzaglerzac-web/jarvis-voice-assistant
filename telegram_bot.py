"""
Jarvis — Telegram Bot
Long-polls the Telegram Bot API and lets the allowed users talk to Jarvis by text.
Every incoming and outgoing message is stored in SQLite (telegram.db).
Senders that are not allowed get their numeric Telegram ID back and nothing else.
"""

import asyncio
import os
import sqlite3
import time

import httpx

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "telegram.db")
HISTORY_LENGTH = 16
MAX_MESSAGE_LENGTH = 4000  # Telegram limit is 4096


def init_db():
    with sqlite3.connect(DB_PATH) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            user_id INTEGER,
            username TEXT,
            direction TEXT NOT NULL,   -- 'in' (from user) or 'out' (from Jarvis)
            allowed INTEGER NOT NULL,
            text TEXT NOT NULL
        )""")


def store(chat_id, user_id, username, direction, allowed, text):
    with sqlite3.connect(DB_PATH) as db:
        db.execute(
            "INSERT INTO messages (ts, chat_id, user_id, username, direction, allowed, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), chat_id, user_id, username, direction, int(allowed), text),
        )


def history(chat_id):
    """Last messages of an allowed chat as Claude messages (oldest first)."""
    with sqlite3.connect(DB_PATH) as db:
        rows = db.execute(
            "SELECT direction, text FROM messages WHERE chat_id = ? AND allowed = 1 ORDER BY id DESC LIMIT ?",
            (chat_id, HISTORY_LENGTH),
        ).fetchall()
    msgs = [{"role": "user" if d == "in" else "assistant", "content": t} for d, t in reversed(rows)]
    # Claude needs the conversation to start with the user
    while msgs and msgs[0]["role"] != "user":
        msgs.pop(0)
    return msgs


async def run(token: str, allowed_ids: set, answer):
    """Poll Telegram forever. answer(messages) -> reply text (async)."""
    init_db()
    api = f"https://api.telegram.org/bot{token}"
    offset = 0
    async with httpx.AsyncClient(timeout=70) as http:
        me = (await http.get(f"{api}/getMe")).json()
        print(f"[telegram] Bot aktiv: @{me.get('result', {}).get('username', '?')}", flush=True)
        while True:
            try:
                resp = await http.get(f"{api}/getUpdates", params={"timeout": 50, "offset": offset})
                updates = resp.json().get("result", [])
            except Exception as e:
                print(f"[telegram] Verbindungsfehler: {e}", flush=True)
                await asyncio.sleep(10)
                continue

            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if not msg:
                    continue
                chat_id = msg["chat"]["id"]
                sender = msg.get("from", {})
                user_id = sender.get("id")
                username = sender.get("username", "")
                text = msg.get("text", "")
                allowed = user_id in allowed_ids

                store(chat_id, user_id, username, "in", allowed, text or "[keine Textnachricht]")
                print(f"[telegram] {'@' + username if username else user_id}: {text[:80]}", flush=True)

                if not allowed:
                    reply = f"Dieser Bot ist privat. Ihre Telegram-ID lautet {user_id}."
                elif not text:
                    reply = "Bitte schreiben Sie mir als Text, Sir. Sprachnachrichten verstehe ich hier noch nicht."
                else:
                    await http.post(f"{api}/sendChatAction", data={"chat_id": chat_id, "action": "typing"})
                    try:
                        reply = await answer(history(chat_id))
                    except Exception as e:
                        print(f"[telegram] Fehler bei der Antwort: {e}", flush=True)
                        reply = "Da ist etwas schiefgelaufen, Sir. Bitte versuchen Sie es gleich noch einmal."

                reply = (reply or "…")[:MAX_MESSAGE_LENGTH]
                await http.post(f"{api}/sendMessage", data={"chat_id": chat_id, "text": reply})
                store(chat_id, user_id, username, "out", allowed, reply)
