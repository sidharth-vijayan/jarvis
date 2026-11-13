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


async def fetch(client: httpx.AsyncClient, url: str) -> dict | None:
    """Download one page. Returns {url, title, text}, or None if it could not be read."""
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
            return {"url": str(r.url), "title": title or str(r.url), "text": text} if len(text) > 200 else None
    except (httpx.HTTPError, ValueError):
        return None


async def search(client: httpx.AsyncClient, query: str, limit: int = 6) -> list[dict]:
    """DuckDuckGo results as [{title, url, snippet}]. Raises httpx.HTTPError if the search itself fails."""
    r = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
    r.raise_for_status()
    clean = lambda s: html.unescape(re.sub(r"<.*?>", "", s)).strip()  # noqa: E731
    results = []
    for block in r.text.split('class="result results_links')[1:]:
        link = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.S)
        if not link:
            continue
        target = re.search(r"uddg=([^&\"]+)", link.group(1))
        url = urllib.parse.unquote(target.group(1)) if target else html.unescape(link.group(1))
        snippet = re.search(r'class="result__snippet"[^>]*>(.*?)</a>', block, re.S)
        if url.startswith("http") and "duckduckgo.com/y.js" not in url:  # skip ads
            results.append({"title": clean(link.group(2)), "url": url, "snippet": clean(snippet.group(1)) if snippet else ""})
    return results[:limit]


async def search_and_read(query: str, pages: int = 3) -> tuple[list[dict], list[dict]]:
    """Search, then open the top results. Returns (all results, the pages that were actually read)."""
    async with _client() as client:
        results = await search(client, query)
        fetched = await asyncio.gather(*(fetch(client, r["url"]) for r in results[:pages + 1]))
        return results, [p for p in fetched if p][:pages]


async def read(url: str) -> dict | None:
    async with _client() as client:
        return await fetch(client, url)
