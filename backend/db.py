"""SQLite persistence: conversations, messages, settings."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DEFAULT_SETTINGS = {
    "mode": "fast",
    "model_fast": "gemma4:e4b-it-q4_K_M",
    "model_balanced": "gemma4:e4b-it-q4_K_M",
    "model_advanced": "qwen3.5:9b",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def _path() -> Path:
    return Path(os.environ.get("JARVIS_DB", Path(__file__).parent.parent / "data" / "jarvis.db"))


@contextmanager
def db():
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with db() as con:
        con.executescript(SCHEMA)


def get_settings() -> dict:
    with db() as con:
        stored = {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM settings")}
    return DEFAULT_SETTINGS | stored


def set_settings(values: dict):
    with db() as con:
        con.executemany("INSERT INTO settings (key, value) VALUES (?, ?) "
                        "ON CONFLICT(key) DO UPDATE SET value = excluded.value", values.items())


def list_conversations() -> list[dict]:
    with db() as con:
        return [dict(r) for r in con.execute(
            "SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC, id DESC")]


def conversation_exists(cid: int) -> bool:
    with db() as con:
        return con.execute("SELECT 1 FROM conversations WHERE id = ?", (cid,)).fetchone() is not None


def create_conversation(title: str) -> int:
    with db() as con:
        return con.execute("INSERT INTO conversations (title) VALUES (?)", (title,)).lastrowid


def delete_conversation(cid: int):
    with db() as con:
        con.execute("DELETE FROM conversations WHERE id = ?", (cid,))


def get_messages(cid: int) -> list[dict]:
    with db() as con:
        return [dict(r) for r in con.execute(
            "SELECT role, content, created_at FROM messages WHERE conversation_id = ? ORDER BY id", (cid,))]


def add_message(cid: int, role: str, content: str):
    with db() as con:
        con.execute("INSERT INTO messages (conversation_id, role, content) VALUES (?, ?, ?)", (cid, role, content))
        con.execute("UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (cid,))
