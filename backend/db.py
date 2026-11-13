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
    "gmail_address": "",
    "memory_enabled": "1",
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
CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL,
    due TEXT NOT NULL,              -- local time, ISO format
    delivered INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS memories (  -- lasting facts about the user; the user can see, edit and delete all of them
    id INTEGER PRIMARY KEY,
    text TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL DEFAULT 'fact',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS audit (  -- every tool call JARVIS made, and whether the user approved it
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    tool TEXT NOT NULL,
    args TEXT NOT NULL,
    decision TEXT NOT NULL,         -- auto | approved | declined | blocked
    ok INTEGER NOT NULL,
    summary TEXT NOT NULL
);
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


def add_reminder(text: str, due: str):
    with db() as con:
        con.execute("INSERT INTO reminders (text, due) VALUES (?, ?)", (text, due))


def upcoming_reminders() -> list[dict]:
    with db() as con:
        return [dict(r) for r in con.execute("SELECT text, due FROM reminders WHERE delivered = 0 ORDER BY due")]


def take_due_reminders(now: str) -> list[dict]:
    """Reminders whose time has come. Each is returned once, then marked delivered."""
    with db() as con:
        due = [dict(r) for r in con.execute("SELECT id, text, due FROM reminders WHERE delivered = 0 AND due <= ?", (now,))]
        con.executemany("UPDATE reminders SET delivered = 1 WHERE id = ?", [(r["id"],) for r in due])
        return due


def add_memory(text: str, category: str) -> bool:
    """Returns False if exactly this memory already exists."""
    with db() as con:
        return con.execute("INSERT OR IGNORE INTO memories (text, category) VALUES (?, ?)", (text, category)).rowcount == 1


def list_memories() -> list[dict]:
    with db() as con:
        return [dict(r) for r in con.execute("SELECT id, text, category, created_at FROM memories ORDER BY id DESC")]


def update_memory(mid: int, text: str) -> bool:
    with db() as con:
        return con.execute("UPDATE OR IGNORE memories SET text = ? WHERE id = ?", (text, mid)).rowcount == 1


def delete_memories(ids: list[int]):
    with db() as con:
        con.executemany("DELETE FROM memories WHERE id = ?", [(i,) for i in ids])


def search_messages(words: list[str], limit: int = 8) -> list[dict]:
    """Past messages containing every word, newest first.
    ponytail: a LIKE scan over all messages; switch to SQLite FTS5 when the history is large enough to feel it."""
    where = " AND ".join("m.content LIKE ? ESCAPE '\\'" for _ in words)
    like = ["%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for w in words]
    with db() as con:
        return [dict(r) for r in con.execute(
            f"SELECT m.role, m.content, m.created_at, c.title FROM messages m JOIN conversations c ON c.id = m.conversation_id "
            f"WHERE {where} ORDER BY m.id DESC LIMIT ?", (*like, limit))]


def add_audit(tool: str, args: str, decision: str, ok: bool, summary: str):
    with db() as con:
        con.execute("INSERT INTO audit (tool, args, decision, ok, summary) VALUES (?, ?, ?, ?, ?)",
                    (tool, args, decision, ok, summary))


def list_audit(limit: int = 100) -> list[dict]:
    with db() as con:
        return [dict(r) for r in con.execute("SELECT at, tool, args, decision, ok, summary FROM audit ORDER BY id DESC LIMIT ?", (limit,))]


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
