"""Browser tools against a local test site, in a hidden Edge window. Run: python -m tests.test_browser"""
import asyncio
import http.server
import os
import tempfile
import threading
from pathlib import Path

os.environ["JARVIS_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from backend import browser, desktop, tools, web  # noqa: E402

SITE = Path(tempfile.mkdtemp())
(SITE / "index.html").write_text("""<html><title>Test shop</title><body>
<h1>Sign up</h1><a href="/about.html">About us</a> <a href="/report.zip">Annual report</a>
<form action="/thanks.html"><label for=n>Your name</label><input id=n name=name>
<label for=p>Password</label><input id=p type=password name=pw>
<select name=size aria-label="Size"><option>Small</option><option>Large</option></select>
<button type=submit>Create account</button></form></body></html>""")
(SITE / "about.html").write_text("<html><title>About</title><body>We sell things.</body></html>")
(SITE / "thanks.html").write_text("<html><title>Thanks</title><body>Account created.</body></html>")
(SITE / "check.html").write_text("<html><body>Please confirm you are not a robot.</body></html>")
(SITE / "report.zip").write_bytes(b"PK test")


class Quiet(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=str(SITE), **k)

    def log_message(self, *a):
        pass


async def main():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Quiet)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_port}"

    assert not (await tools.run("browser_open", {"url": base})).ok  # localhost is refused for real
    real_is_public = web.is_public
    web.is_public = lambda url: asyncio.sleep(0, url.startswith(base))  # this test site only
    browser.HEADLESS, browser.DOWNLOADS, browser.PROFILE = True, Path(tempfile.mkdtemp()), Path(tempfile.mkdtemp())
    try:
        page = (await tools.run("browser_open", {"url": base})).text
        assert "Sign up" in page and "untrusted" in page, page
        num = {e["label"]: str(n) for n, e in browser._elements.items()}
        assert await desktop._click_preview(num["About us"]) is None  # plain link: no approval
        assert "Download" in await desktop._click_preview(num["Annual report"])
        assert await desktop._type_preview(num["Your name"], "Sid", "no") is None
        assert "press Enter" in await desktop._type_preview(num["Your name"], "Sid", "yes")
        assert not (await tools.run("browser_type", {"number": num["Password"], "text": "x", "press_enter": "no"})).ok
        assert (await tools.run("browser_type", {"number": num["Size"], "text": "Large", "press_enter": "no"})).ok
        assert (await tools.run("browser_type", {"number": num["Your name"], "text": "Sid", "press_enter": "no"})).ok
        preview = await desktop._click_preview(num["Create account"])
        assert "Create account" in preview and "Sid" in preview, preview
        assert "Account created" in (await tools.run("browser_click", {"number": num["Create account"]})).text
        await tools.run("browser_open", {"url": base})
        num = {e["label"]: str(n) for n, e in browser._elements.items()}
        await tools.run("browser_click", {"number": num["Annual report"]})
        await asyncio.sleep(2)
        assert list(browser.DOWNLOADS.iterdir()), "nothing downloaded"
        assert "Do not try to solve it" in (await tools.run("browser_open", {"url": base + "/check.html"})).text
        web.is_public = real_is_public
        assert "Browser action failed" in (await tools.run("browser_open", {"url": "http://192.168.1.1"})).text
    finally:
        web.is_public = real_is_public
        if browser._context:
            await browser._context.close()
            await browser._playwright.stop()
        server.shutdown()
    print("browser: all checks passed")


if __name__ == "__main__":
    asyncio.run(main())
