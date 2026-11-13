"""Read-only Gmail over IMAP, signed in with a Google "app password".
The address is kept in the settings table; the app password is kept in Windows Credential Manager, never in
a file or the database. The mailbox is always opened read-only: nothing is marked read, moved or deleted."""
import email
import email.policy
import imaplib
import re
from email.utils import parsedate_to_datetime

import keyring

from . import db, web

SERVICE = "jarvis-gmail"
HOST = "imap.gmail.com"
PART_BYTES = 200_000  # per message; enough for the text, without pulling large attachments


class NotConnected(Exception):
    pass


def address() -> str | None:
    return db.get_settings().get("gmail_address") or None


def connect(addr: str, app_password: str):
    """Check that the credentials work, then store them. Raises imaplib.IMAP4.error if Gmail refuses them."""
    password = app_password.replace(" ", "")
    conn = imaplib.IMAP4_SSL(HOST, timeout=20)
    try:
        conn.login(addr, password)
    finally:
        try:
            conn.logout()
        except Exception:
            pass
    keyring.set_password(SERVICE, addr, password)
    db.set_settings({"gmail_address": addr})


def disconnect():
    addr = address()
    if addr:
        try:
            keyring.delete_password(SERVICE, addr)
        except keyring.errors.PasswordDeleteError:
            pass
    db.set_settings({"gmail_address": ""})


def _open() -> imaplib.IMAP4_SSL:
    addr = address()
    password = keyring.get_password(SERVICE, addr) if addr else None
    if not password:
        raise NotConnected("Gmail is not connected. Connect it in Settings.")
    conn = imaplib.IMAP4_SSL(HOST, timeout=20)
    conn.login(addr, password)
    conn.select("INBOX", readonly=True)
    return conn


def _text(message) -> str:
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        content = part.get_content()
    except (LookupError, ValueError):  # unknown charset or a body cut off mid-encoding
        content = (part.get_payload(decode=True) or b"").decode("utf-8", errors="replace")
    if part.get_content_type() == "text/html":
        content = web.extract(content)[1]
    return re.sub(r"\s+", " ", content).strip()


def _parse(uid: str, raw: bytes) -> dict:
    message = email.message_from_bytes(raw, policy=email.policy.default)
    try:
        when = parsedate_to_datetime(message["Date"]).astimezone().strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        when = ""
    return {"id": uid, "from": str(message["From"] or ""), "subject": str(message["Subject"] or "(no subject)"),
            "date": when, "text": _text(message)}


def _fetch(conn, uid: str) -> dict | None:
    _, data = conn.uid("FETCH", uid, f"(BODY.PEEK[]<0.{PART_BYTES}>)")  # PEEK: does not mark it as read
    raw = next((part[1] for part in data if isinstance(part, tuple)), None)
    return _parse(uid, raw) if raw else None


def search(query: str, limit: int = 8) -> list[dict]:
    """Newest messages matching a Gmail search (same syntax as the Gmail search box), e.g. 'is:unread'."""
    conn = _open()
    try:
        _, data = conn.uid("SEARCH", "X-GM-RAW", '"' + query.replace("\\", "").replace('"', "") + '"')
        uids = data[0].decode().split()[-limit:][::-1]
        return [m for m in (_fetch(conn, uid) for uid in uids) if m]
    finally:
        conn.logout()


def read(uid: str) -> dict | None:
    if not uid.isdigit():
        return None
    conn = _open()
    try:
        return _fetch(conn, uid)
    finally:
        conn.logout()


def unread_count() -> int:
    conn = _open()
    try:
        _, data = conn.uid("SEARCH", "UNSEEN")
        return len(data[0].split())
    finally:
        conn.logout()
