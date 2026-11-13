"""Calendar and GitHub against a fake calendar file, fake GitHub replies and a fake Credential Manager.
Run: python -m tests.test_accounts"""
import asyncio
import os
import tempfile
from datetime import date, timedelta

os.environ["JARVIS_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

import httpx  # noqa: E402

from backend import accounts, db, desktop, main, tools  # noqa: E402

TODAY = date.today()
ICS = f"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:1
SUMMARY:Standup
DTSTART:{TODAY:%Y%m%d}T093000
DTEND:{TODAY:%Y%m%d}T094500
RRULE:FREQ=DAILY;COUNT=5
END:VEVENT
BEGIN:VEVENT
UID:2
SUMMARY:Project deadline
DTSTART;VALUE=DATE:{TODAY + timedelta(days=2):%Y%m%d}
END:VEVENT
END:VCALENDAR
""".encode()
CAL_URL = "https://calendar.google.com/calendar/ical/me%40gmail.com/private-abc123/basic.ics"


def fake_get(url, params=None, headers=None, **_):
    request = httpx.Request("GET", url)
    if url == CAL_URL:
        return httpx.Response(200, content=ICS, request=request)
    if headers and headers["Authorization"] != "Bearer good-token-123":
        return httpx.Response(401, json={}, request=request)
    replies = {"/user": {"login": "octo"},
               "/notifications": [{"repository": {"full_name": "octo/app"}, "subject": {"title": "Fix CI", "type": "PullRequest"}, "reason": "review_requested", "updated_at": "2026-11-12T10:00:00Z"}],
               "/search/issues": {"items": [{"repository_url": "https://api.github.com/repos/octo/app", "number": 7, "title": "Crash on start", "user": {"login": "octo"},
                                             "created_at": "2026-11-01T09:30:00Z", "updated_at": "2026-11-12T10:00:00Z", "comments": 3, "html_url": "https://github.com/octo/app/pull/7"}]},
               "/user/repos": [{"full_name": "octo/app", "private": True, "description": None, "language": "Python", "stargazers_count": 4,
                                "pushed_at": "2026-11-13T08:00:00Z", "open_issues_count": 2, "html_url": "https://github.com/octo/app"}]}
    return httpx.Response(200, json=replies[url.removeprefix(accounts.GITHUB)], request=request)


def fake_post(url, json=None, **_):
    assert url.endswith("/graphql") and json["variables"]["from"].startswith(str(TODAY.year))
    c = {"contributionCalendar": {"totalContributions": 321}, "totalCommitContributions": 300, "totalPullRequestContributions": 12,
         "totalPullRequestReviewContributions": 5, "totalIssueContributions": 4, "restrictedContributionsCount": 0}
    return httpx.Response(200, json={"data": {"viewer": {"contributionsCollection": c}}}, request=httpx.Request("POST", url))


def main_():
    db.init()
    vault = {}
    accounts.keyring.set_password = lambda s, u, p: vault.__setitem__((s, u), p)
    accounts.keyring.get_password = lambda s, u: vault.get((s, u))
    accounts.keyring.delete_password = lambda s, u: vault.pop((s, u))
    accounts.httpx.get = fake_get
    accounts.httpx.post = fake_post
    run = lambda name, **a: asyncio.run(tools.run(name, a))  # noqa: E731

    assert "not connected" in run("check_calendar", start=str(TODAY), days="1").text.lower()
    try:
        accounts.connect_calendar("https://evil.example/cal.ics")
        raise AssertionError("non-Google link accepted")
    except ValueError:
        pass
    accounts.connect_calendar(CAL_URL)
    week = run("check_calendar", start=str(TODAY), days="7").text
    assert week.count("Standup") == 5 and "Project deadline" in week and "(all day)" in week, week
    assert run("check_calendar", start=str(TODAY), days="1").text.count("Standup") == 1
    assert "Standup" in main.briefing_context()

    try:
        accounts.connect_github("bad-token-xyz")
        raise AssertionError("bad token accepted")
    except httpx.HTTPStatusError:
        pass
    assert accounts.connect_github("good-token-123") == "octo"
    assert "Fix CI" in run("check_github", kind="notifications").text
    prs = run("check_github", kind="prs").text
    assert "octo/app#7" in prs and "opened 2026-11-01" in prs and "pull/7" in prs, prs
    assert "321 in total" in run("check_github", kind="contributions").text
    assert "(private)" in run("check_github", kind="repos").text
    assert not run("check_github", kind="delete").ok
    assert "good-token" not in open(os.environ["JARVIS_DB"], "rb").read().decode("latin-1")

    accounts.disconnect_github()
    accounts.disconnect_calendar()
    assert not vault and accounts.github_login() is None
    print("accounts: all checks passed")


if __name__ == "__main__":
    main_()
