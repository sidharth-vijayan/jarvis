"""Send a WhatsApp message through WhatsApp Web, in a real Edge window driven by Playwright.
The window uses its own browser profile (data/whatsapp-profile), so the phone only has to be linked once.
WhatsApp does not offer this for personal accounts, so it depends on their page layout and can break when
they change it; the selectors are all in this file."""
import asyncio
from pathlib import Path

PROFILE = Path(__file__).parent.parent / "data" / "whatsapp-profile"
SEARCH = ('div[contenteditable="true"][data-tab="3"], div[role="textbox"][aria-label*="Search" i], '
          'input[aria-label*="Search" i]')
CHAT_TITLES = "#pane-side span[title]"
OPEN_CHAT_TITLE = "#main header span[title]"
MESSAGE_BOX = '#main footer div[contenteditable="true"]'

_lock = asyncio.Lock()
_playwright = _context = _page = None


async def _open():
    global _playwright, _context, _page
    if _page is None or _page.is_closed():
        from playwright.async_api import async_playwright
        if _playwright is None:
            _playwright = await async_playwright().start()
        PROFILE.mkdir(parents=True, exist_ok=True)
        _context = await _playwright.chromium.launch_persistent_context(
            str(PROFILE), channel="msedge", headless=False, no_viewport=True, args=["--window-size=1100,820"])
        _page = _context.pages[0] if _context.pages else await _context.new_page()
        await _page.goto("https://web.whatsapp.com", wait_until="domcontentloaded")
    return _page


async def close():
    global _context, _page
    if _context:
        await _context.close()
    _context = _page = None


async def send(contact: str, message: str, link_timeout_ms: int = 45000) -> str:
    """Send `message` to the chat named exactly `contact`. Returns the chat's name. Raises ValueError with a
    plain explanation, having sent nothing, if WhatsApp is not linked or no chat has exactly that name."""
    from playwright.async_api import TimeoutError as PlaywrightTimeout
    if '"' in contact or "\\" in contact:
        raise ValueError("That contact name has characters JARVIS cannot search for")
    async with _lock:
        page = await _open()
        await page.bring_to_front()
        search = page.locator(SEARCH).first
        try:
            await search.wait_for(state="visible", timeout=link_timeout_ms)
        except PlaywrightTimeout:
            raise ValueError("WhatsApp is not linked on this PC yet. A WhatsApp window is open: on your phone go to "
                             "WhatsApp > Linked devices > Link a device, scan the code, then ask again. Nothing was sent")
        await search.click()
        await page.keyboard.press("Control+A")
        await page.keyboard.type(contact, delay=25)
        await page.wait_for_timeout(1800)
        exact = page.locator(f'#pane-side span[title="{contact}" i]')
        if await exact.count() == 0:
            names = list(dict.fromkeys(await page.locator(CHAT_TITLES).evaluate_all("els => els.map(e => e.title)")))[:5]
            hint = f" The closest chats are: {', '.join(names)}." if names else ""
            raise ValueError(f'There is no WhatsApp chat named exactly "{contact}".{hint} Nothing was sent')
        await exact.first.click()
        title = await page.locator(OPEN_CHAT_TITLE).first.get_attribute("title", timeout=10000)
        if (title or "").lower() != contact.lower():  # never type into a chat we did not mean to open
            raise ValueError(f'WhatsApp opened "{title}" instead of "{contact}". Nothing was sent')
        box = page.locator(MESSAGE_BOX).first
        await box.click(timeout=10000)
        await page.keyboard.insert_text(message)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(1000)
        return title
