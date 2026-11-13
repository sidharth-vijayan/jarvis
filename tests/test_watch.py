"""Website monitoring against a fake web. Run: python -m tests.test_watch"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta

os.environ["JARVIS_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from backend import db, desktop, tools, web  # noqa: E402

PAGES = {}


async def fake_read(url, min_chars=200):
    return {"url": url, "title": "t", "text": PAGES[url]} if url in PAGES else None


def main():
    db.init()
    web.read = fake_read
    run = lambda tool, **a: asyncio.run(tools.run(tool, a))  # noqa: E731
    later = lambda minutes: datetime.now() + timedelta(minutes=minutes)  # noqa: E731
    check = lambda minutes: asyncio.run(desktop.check_watches(later(minutes)))  # noqa: E731
    due = lambda: [r["text"] for r in db.take_due_reminders(later(1000).isoformat(timespec="minutes"))]  # noqa: E731

    assert not run("watch_website", url="https://gone.example", name="x", look_for="any").ok
    PAGES["https://shop.example/ps5"] = "PS5 Out of stock"
    PAGES["https://uni.example/news"] = "No news yet"
    assert "not on the page yet" in run("watch_website", url="https://shop.example/ps5", name="PS5", look_for="in stock").text
    run("watch_website", url="https://uni.example/news", name="Uni news", look_for="any")

    check(10)  # not due yet: nothing fetched
    PAGES["https://shop.example/ps5"] = "PS5 In stock now"
    check(10)
    assert due() == []
    check(31)
    alerts = due()
    assert len(alerts) == 1 and "PS5" in alerts[0] and "in stock" in alerts[0], alerts

    PAGES["https://uni.example/news"] = "Results announced"
    check(62)
    alerts = due()
    assert alerts == ["Uni news has changed. https://uni.example/news"], alerts  # PS5 still in stock: no repeat
    check(93)
    assert due() == []  # nothing changed

    del PAGES["https://uni.example/news"]  # unreachable: no alert
    check(124)
    assert due() == []

    listing = run("list_watches").text
    assert "PS5" in listing and "Uni news" in listing
    wid = db.list_watches()[0]["id"]
    assert run("stop_watching", id=str(wid)).ok and not run("stop_watching", id=str(wid)).ok
    assert len(db.list_watches()) == 1
    print("watch: all checks passed")


if __name__ == "__main__":
    main()
