"""Web search and page reading, with no API key: DuckDuckGo's HTML results page plus plain page fetches.
Everything returned from here is untrusted text from the internet."""
import asyncio
import html
import ipaddress
import re
import urllib.parse
from html.parser import HTMLParser

import httpx

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/130.0 Safari/537.36", "Accept-Language": "en"}
MAX_PAGE_BYTES = 1_500_000
PAGE_CHARS = 2500  # per page handed to the model; three pages fit comfortably in its 8k-token context
SKIP_TAGS = {"script", "style", "noscript", "nav", "footer", "header", "aside", "form", "svg", "template", "iframe"}


class _Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.title, self._skip, self._in_title = [], "", 0, False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_TAGS:
            self._skip += 1
        self._in_title = tag == "title" and not self._skip and not self.title  # the page's title, not an SVG's

    def handle_endtag(self, tag):
        if tag in SKIP_TAGS and self._skip:
            self._skip -= 1
        self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip and data.strip():
            self.parts.append(data.strip())


def extract(page: str) -> tuple[str, str]:
    """HTML -> (title, readable text)."""
    parser = _Text()
    parser.feed(page)
    return " ".join(parser.title.split()), re.sub(r"\s+", " ", " ".join(parser.parts))


async def is_public(url: str) -> bool:
    """Only plain web addresses on the public internet. Keeps a page (or a confused model) from making
    JARVIS fetch this laptop's own services or devices on the home network."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, None)
    except OSError:
        return False
    # ponytail: checked at lookup time only; a host that changes its DNS answer between this check and the
    # fetch could slip through. Pin the resolved address if JARVIS is ever exposed beyond this laptop.
    return all(ipaddress.ip_address(info[4][0]).is_global for info in infos)


async def _check(request: httpx.Request):  # runs for every hop, so redirects are checked too
    if not await is_public(str(request.url)):
        raise httpx.RequestError("Refused: not a public web address", request=request)


def _client() -> httpx.AsyncClient:
    # A slow page is skipped rather than waited for: the answer uses whichever pages came back in time.
    return httpx.AsyncClient(headers=HEADERS, timeout=6, follow_redirects=True, max_redirects=5,
                             event_hooks={"request": [_check]})


async def fetch(client: httpx.AsyncClient, url: str, min_chars: int = 200) -> dict | None:
    """Download one page. Returns {url, title, text}, or None if it could not be read or has under min_chars
    of text (for search, a near-empty page is useless)."""
    try:
        async with client.stream("GET", url) as r:
            if r.status_code != 200 or not r.headers.get("content-type", "").startswith(("text/html", "text/plain")):
                return None
            body = b""
            async for chunk in r.aiter_bytes():
                body += chunk
                if len(body) > MAX_PAGE_BYTES:
                    break
            title, text = extract(body.decode(r.encoding or "utf-8", errors="replace"))
            return {"url": str(r.url), "title": title or str(r.url), "text": text} if len(text) >= min_chars else None
    except (httpx.HTTPError, ValueError):
        return None


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<.*?>", "", s, flags=re.S)).strip()


def _parse_html(page: str) -> list[dict]:
    results = []
    for block in page.split('class="result results_links')[1:]:
        link = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        target = re.search(r"uddg=([^&\"]+)", link.group(1))
        url = urllib.parse.unquote(target.group(1)) if target else html.unescape(link.group(1))
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        results.append({"title": _clean(link.group(2)), "url": url, "snippet": _clean(snippet.group(1)) if snippet else ""})
    return results


def _parse_lite(page: str) -> list[dict]:
    links = re.findall(r"<a[^>]*href=\"([^\"]+)\"[^>]*class='result-link'[^>]*>(.*?)</a>", page, re.S)
    snippets = re.findall(r"<td class='result-snippet'>(.*?)</td>", page, re.S)
    return [{"title": _clean(t), "url": html.unescape(u), "snippet": _clean(snippets[i]) if i < len(snippets) else ""}
            for i, (u, t) in enumerate(links)]


def _parse_yahoo(page: str) -> list[dict]:
    results = []
    for block in page.split("algo-sr")[1:]:
        link = re.search(r'href="([^"]*/RU=([^/"]+)/[^"]*)"', block)
        title = re.search(r"<h3[^>]*>(.*?)</h3>", block, re.S)
        snippet = re.search(r'class="compText[^"]*"[^>]*>(.*?)</div>', block, re.S)
        if link and title:
            results.append({"title": re.sub(r"^.*?› [^ ]+ ?", "", _clean(title.group(1))) or _clean(title.group(1)),
                            "url": urllib.parse.unquote(link.group(2)), "snippet": _clean(snippet.group(1)) if snippet else ""})
    return results


# Tried in order. Each sometimes answers automated requests with a "prove you're human" page instead of
# results, especially after many searches in a row; the next one is tried when a page gives no results.
ENGINES = (("POST", "https://html.duckduckgo.com/html/", "q", _parse_html),
           ("POST", "https://lite.duckduckgo.com/lite/", "q", _parse_lite),
           ("GET", "https://search.yahoo.com/search", "p", _parse_yahoo))


async def search(client: httpx.AsyncClient, query: str, limit: int = 6) -> list[dict]:
    """Web results as [{title, url, snippet}]. Raises httpx.HTTPError only if every engine fails."""
    results, error = [], None
    for method, endpoint, field, parse in ENGINES:
        try:
            for attempt in range(2):  # one retry: these pages give the odd server error
                r = await (client.post(endpoint, data={field: query}) if method == "POST" else client.get(endpoint, params={field: query}))
                if r.status_code < 500:
                    break
            r.raise_for_status()
        except httpx.HTTPError as e:
            error = e
            continue
        results = [x for x in parse(r.text) if x["url"].startswith("http") and "duckduckgo.com/y.js" not in x["url"]]  # no ads
        if results:
            return results[:limit]
    if error:
        raise error
    return []


# Google News RSS: free, keyless, and filters by country and time, which plain web search does badly.
NEWS_PERIODS = {"hour": "1h", "day": "1d", "week": "7d", "month": "30d"}


async def news(topic: str, country: str, period: str, limit: int = 12) -> list[dict]:
    """Newest headlines as [{title, source, published, url}]. topic '' = top stories; country is a two-letter
    code such as IN, US, GB, AE ('' = US English edition); period is hour, day, week or month ('' = any)."""
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    cc = (country or "US").upper()
    params = {"hl": f"en-{cc}", "gl": cc, "ceid": f"{cc}:en"}
    when = NEWS_PERIODS.get(period.lower(), "")
    if topic:
        url, params["q"] = "https://news.google.com/rss/search", f"{topic} when:{when}" if when else topic
    else:  # top stories are current by definition; a time filter needs a search term
        url = "https://news.google.com/rss"
    async with _client() as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
    found = []
    for item in ET.fromstring(r.content).iter("item"):
        title, source = item.findtext("title", ""), item.findtext("source", "")
        if source and title.endswith(" - " + source):
            title = title[: -len(source) - 3]
        try:
            published = parsedate_to_datetime(item.findtext("pubDate", "")).astimezone().strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            published = ""
        found.append({"title": title, "source": source, "published": published, "url": item.findtext("link", "")})
    # top stories come ranked by importance; searches are put newest first
    return (sorted(found, key=lambda n: n["published"], reverse=True) if topic else found)[:limit]


async def search_and_read(query: str, pages: int = 3) -> tuple[list[dict], list[dict]]:
    """Search, then open the top results. Returns (all results, the pages that were actually read)."""
    async with _client() as client:
        results = await search(client, query)
        fetched = await asyncio.gather(*(fetch(client, r["url"]) for r in results[:pages + 1]))
        return results, [p for p in fetched if p][:pages]


async def read(url: str, min_chars: int = 200) -> dict | None:
    async with _client() as client:
        return await fetch(client, url, min_chars)
