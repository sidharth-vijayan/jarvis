"""Read-only Google Calendar (its private iCal link) and GitHub (a read-only token).
Both secrets live in Windows Credential Manager, never in a file or the database."""
from datetime import date, datetime, timedelta

import httpx
import icalendar
import keyring
import recurring_ical_events

SERVICE = "jarvis-accounts"
CAL_PREFIX = "https://calendar.google.com/calendar/ical/"
GITHUB = "https://api.github.com"


class NotConnected(Exception):
    pass


def _secret(name: str) -> str | None:
    return keyring.get_password(SERVICE, name)


def _forget(name: str):
    try:
        keyring.delete_password(SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass


# ---- Calendar ------------------------------------------------------------------------------------
_cal_cache: tuple[float, icalendar.Calendar | None] = (0.0, None)


def _download(url: str) -> icalendar.Calendar:
    r = httpx.get(url, timeout=20, follow_redirects=False)
    r.raise_for_status()
    return icalendar.Calendar.from_ical(r.content)


def calendar_connected() -> bool:
    return bool(_secret("calendar"))


def connect_calendar(url: str):
    """Check that the link downloads a calendar, then store it. Only Google's private iCal links are accepted."""
    global _cal_cache
    url = url.strip()
    if not url.startswith(CAL_PREFIX) or not url.endswith(".ics"):
        raise ValueError("That is not a Google Calendar iCal link.")
    _cal_cache = (datetime.now().timestamp(), _download(url))
    keyring.set_password(SERVICE, "calendar", url)


def disconnect_calendar():
    global _cal_cache
    _forget("calendar")
    _cal_cache = (0.0, None)


def events(start: date, days: int = 1) -> list[dict]:
    """Events from the start of `start` for `days` days, repeating events expanded, sorted by start time."""
    global _cal_cache
    url = _secret("calendar")
    if not url:
        raise NotConnected("Google Calendar is not connected. Connect it in Settings.")
    if datetime.now().timestamp() - _cal_cache[0] > 300 or _cal_cache[1] is None:  # re-download every 5 min
        _cal_cache = (datetime.now().timestamp(), _download(url))
    found = []
    for e in recurring_ical_events.of(_cal_cache[1]).between(start, start + timedelta(days=days)):
        begin = e.decoded("DTSTART")
        all_day = not isinstance(begin, datetime)
        if not all_day:
            begin = begin.astimezone() if begin.tzinfo else begin
        found.append({"title": str(e.get("SUMMARY", "(no title)")), "all_day": all_day,
                      "start": begin.strftime("%Y-%m-%d") if all_day else begin.strftime("%Y-%m-%d %H:%M"),
                      "location": str(e.get("LOCATION", ""))})
    return sorted(found, key=lambda e: e["start"])


# ---- GitHub --------------------------------------------------------------------------------------
def _gh(path: str, token: str | None = None, **params):
    token = token or _secret("github")
    if not token:
        raise NotConnected("GitHub is not connected. Connect it in Settings.")
    r = httpx.get(GITHUB + path, params=params, timeout=20,
                  headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    r.raise_for_status()
    return r.json()


def github_login() -> str | None:
    from . import db
    return db.get_settings().get("github_login") or None


def connect_github(token: str):
    from . import db
    token = token.strip()
    login = _gh("/user", token)["login"]  # raises if GitHub refuses the token
    keyring.set_password(SERVICE, "github", token)
    db.set_settings({"github_login": login})
    return login


def disconnect_github():
    from . import db
    _forget("github")
    db.set_settings({"github_login": ""})


def _when(stamp: str) -> str:  # GitHub's UTC timestamp -> local "YYYY-MM-DD HH:MM"
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")


CONTRIBUTIONS = """query($from: DateTime!) { viewer { contributionsCollection(from: $from) {
  contributionCalendar { totalContributions } totalCommitContributions totalPullRequestContributions
  totalPullRequestReviewContributions totalIssueContributions restrictedContributionsCount } } }"""


def _contributions() -> list[str]:
    token = _secret("github")
    if not token:
        raise NotConnected("GitHub is not connected. Connect it in Settings.")
    start = datetime(datetime.now().year, 1, 1).astimezone()
    r = httpx.post(GITHUB + "/graphql", timeout=20, headers={"Authorization": f"Bearer {token}"},
                   json={"query": CONTRIBUTIONS, "variables": {"from": start.isoformat()}})
    r.raise_for_status()
    c = r.json()["data"]["viewer"]["contributionsCollection"]
    return [f"Contributions since {start:%Y-%m-%d}: {c['contributionCalendar']['totalContributions']} in total "
            f"(the number on the GitHub profile graph)",
            f"Commits: {c['totalCommitContributions']}, pull requests opened: {c['totalPullRequestContributions']}, "
            f"reviews: {c['totalPullRequestReviewContributions']}, issues opened: {c['totalIssueContributions']}, "
            f"in private repositories not shown above: {c['restrictedContributionsCount']}"]


def github(kind: str) -> list[str]:
    """One line per item. kind: notifications, prs, issues, repos, contributions."""
    if kind == "notifications":
        return [f"{n['repository']['full_name']}: {n['subject']['title']} ({n['subject']['type']}, {n['reason']}, "
                f"{_when(n['updated_at'])})" for n in _gh("/notifications", per_page=20)]
    if kind in ("prs", "issues"):
        q = f"is:open is:{'pr' if kind == 'prs' else 'issue'} involves:{github_login()} archived:false"
        return [f"{i['repository_url'].split('/repos/')[1]}#{i['number']}: {i['title']} | by {i['user']['login']} | "
                f"opened {_when(i['created_at'])} | updated {_when(i['updated_at'])} | {i['comments']} comments"
                f"{' | draft' if i.get('draft') else ''} | {i['html_url']}"
                for i in _gh("/search/issues", q=q, sort="updated", per_page=20)["items"]]
    if kind == "repos":
        return [f"{r['full_name']}{' (private)' if r['private'] else ''}: {r['description'] or 'no description'} | "
                f"{r['language'] or '-'} | {r['stargazers_count']} stars | last push {_when(r['pushed_at'])} | "
                f"{r['open_issues_count']} open issues/PRs | {r['html_url']}"
                for r in _gh("/user/repos", sort="pushed", per_page=15)]
    if kind == "contributions":
        return _contributions()
    raise ValueError(f"Unknown kind {kind!r}")
