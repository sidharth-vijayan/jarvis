"""JARVIS launcher. Starts the local server, opens the app window, and stays in the system tray.
Closing the window leaves JARVIS running in the background (reminders still arrive as notifications);
"Quit" in the tray menu stops everything. Run with .venv\\Scripts\\pythonw.exe (no console).
Pass --background to start in the tray without opening a window (used by "start with Windows")."""
import ctypes
import ctypes.wintypes
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

import psutil
import pystray
from PIL import Image, ImageDraw

ROOT = Path(__file__).parent
URL = f"http://127.0.0.1:{os.environ.get('JARVIS_PORT', '8765')}"
PROFILE = ROOT / "data" / "app-window"
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]
# Global shortcut that opens the window: modifiers then a letter, for example "ctrl+alt+j".
HOTKEY = os.environ.get("JARVIS_HOTKEY", "ctrl+alt+j").lower().split("+")
MODIFIERS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}


def get(path: str):
    with urllib.request.urlopen(URL + path, timeout=3) as r:
        return json.load(r)


def up() -> bool:
    try:
        get("/api/settings")
        return True
    except (OSError, ValueError):
        return False


def window_processes():
    for p in psutil.process_iter(["cmdline"]):
        try:
            if str(PROFILE).lower() in " ".join(p.info["cmdline"] or []).lower():
                yield p
        except (psutil.Error, TypeError):
            pass


def open_window(*_):
    browser = next((b for b in BROWSERS if os.path.exists(b)), None)
    if not browser:
        webbrowser.open(URL)
        return
    subprocess.Popen([browser, f"--app={URL}", f"--user-data-dir={PROFILE}", "--no-first-run", "--window-size=1440,900",
                      # lets the wake-word listener and spoken replies work before any click in the window
                      "--autoplay-policy=no-user-gesture-required"])


def start_server():
    (ROOT / "data").mkdir(exist_ok=True)
    log = open(ROOT / "data" / "server.log", "w")
    server = subprocess.Popen([str(Path(sys.executable).with_name("python.exe")), "-m", "backend"], cwd=ROOT,
                              stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    while not up() and server.poll() is None:
        time.sleep(0.1)
    if server.poll() is not None:
        ctypes.windll.user32.MessageBoxW(0, f"JARVIS could not start. See {ROOT / 'data' / 'server.log'}", "JARVIS", 0x10)
        sys.exit(1)
    return server


def stop_server(server):
    """The venv's python.exe is a launcher that starts the real interpreter as a child, so stop the whole tree."""
    try:
        procs = psutil.Process(server.pid).children(recursive=True) + [psutil.Process(server.pid)]
    except psutil.Error:
        return
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass


def tray_image():
    image = Image.new("RGBA", (64, 64), (5, 10, 20, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((12, 12, 52, 52), outline=(60, 220, 255, 255), width=5)
    draw.ellipse((26, 26, 38, 38), fill=(60, 220, 255, 255))
    return image


def hotkey_loop():
    """Registers the global shortcut with Windows and opens the window each time it is pressed."""
    user32 = ctypes.windll.user32
    mods = sum(MODIFIERS.get(k, 0) for k in HOTKEY[:-1]) | 0x4000  # MOD_NOREPEAT
    if not HOTKEY[-1] or not user32.RegisterHotKey(None, 1, mods, ord(HOTKEY[-1][0].upper())):
        return  # another program already owns that shortcut
    msg = ctypes.wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == 0x0312:  # WM_HOTKEY
            open_window()


def reminder_loop(icon):
    """While no window is open, deliver due reminders as Windows notifications (the window speaks them itself)."""
    while True:
        time.sleep(20)
        try:
            if not any(window_processes()):
                for r in get("/api/reminders/due"):
                    icon.notify(r["text"], "JARVIS reminder")
        except (OSError, ValueError):
            pass


def main():
    if up():  # already running: just bring up a window
        open_window()
        return
    server = start_server()

    def quit_all(icon, _item):
        for p in window_processes():
            try:
                p.terminate()
            except psutil.Error:
                pass
        stop_server(server)
        icon.stop()

    icon = pystray.Icon("JARVIS", tray_image(), "JARVIS", pystray.Menu(
        pystray.MenuItem(f"Open JARVIS  ({'+'.join(HOTKEY).title()})", open_window, default=True),
        pystray.MenuItem("Quit", quit_all)))
    threading.Thread(target=hotkey_loop, daemon=True).start()
    threading.Thread(target=reminder_loop, args=(icon,), daemon=True).start()
    if "--background" not in sys.argv:
        open_window()
    try:
        icon.run()
    finally:
        stop_server(server)


main()
