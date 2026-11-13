"""Web search checks. The first half needs no network; the second half does a real search.
Run: .venv\\Scripts\\python -m tests.test_web"""
import asyncio

from backend import tools, web


def demo():
    # Text extraction drops scripts, navigation and markup.
    title, text = web.extract("<html><head><title> A  page </title><script>evil()</script></head><body><nav>Menu</nav>"
                              "<p>Hello <b>world</b>.</p><footer>Legal</footer></body></html>")
    assert (title, text) == ("A page", "Hello world ."), (title, text)

    # Only public web addresses may be fetched.
    refused = ["http://127.0.0.1:8765/api/settings", "http://localhost/", "http://192.168.1.1/", "http://10.0.0.5/",
               "http://169.254.169.254/latest", "file:///C:/Windows/win.ini", "ftp://example.com/x", "http:///nohost"]
    for url in refused:
        assert not asyncio.run(web.is_public(url)), url
    assert asyncio.run(tools.run("read_webpage", {"url": "http://127.0.0.1:8765/api/settings"})).ok is False

    # Bad calls come back as a readable failure instead of raising.
    assert asyncio.run(tools.run("web_search", {})).ok is False
    assert asyncio.run(tools.run("web_search", {"query": 5})).ok is False

    # Live: a real search opens real pages and reports exactly those as sources.
    assert asyncio.run(web.is_public("https://example.com/")), "no internet connection?"
    result = asyncio.run(tools.run("web_search", {"query": "Python programming language"}))
    assert result.ok and result.sources, result.text[:300]
    assert result.text.startswith(tools.UNTRUSTED)
    for s in result.sources:
        assert s["url"].startswith("http") and f"URL: {s['url']}" in result.text
    print(f"read {len(result.sources)} pages, {len(result.text)} chars:", *[s["url"] for s in result.sources], sep="\n  ")
    print("web OK")


if __name__ == "__main__":
    demo()
