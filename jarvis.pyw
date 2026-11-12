"""Start JARVIS as an app: run the local server and show it in its own window.
The server stops when the window is closed. Run with .venv\\Scripts\\pythonw.exe (no console)."""
import ctypes
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

import psutil

ROOT = Path(__file__).parent
URL = f"http://127.0.0.1:{os.environ.get('JARVIS_PORT', '8765')}"
BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
]


def up() -> bool:
    try:
        urllib.request.urlopen(URL + "/api/settings", timeout=1)
        return True
    except OSError:
        return False


server = None
if not up():  # already running (second launch): just open another window
    (ROOT / "data").mkdir(exist_ok=True)
    log = open(ROOT / "data" / "server.log", "w")
    server = subprocess.Popen([str(Path(sys.executable).with_name("python.exe")), "-m", "backend"], cwd=ROOT,
                              stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
    while not up() and server.poll() is None:
        time.sleep(0.1)
    if server.poll() is not None:
        ctypes.windll.user32.MessageBoxW(0, f"JARVIS could not start. See {ROOT / 'data' / 'server.log'}", "JARVIS", 0x10)
        sys.exit(1)

PROFILE = ROOT / "data" / "app-window"


def window_open() -> bool:
    """True while any browser process is using JARVIS's own profile folder, i.e. a JARVIS window exists."""
    for p in psutil.process_iter(["cmdline"]):
        try:
            if str(PROFILE).lower() in " ".join(p.info["cmdline"] or []).lower():
                return True
        except (psutil.Error, TypeError):
            pass
    return False


browser = next((b for b in BROWSERS if os.path.exists(b)), None)
try:
    if browser:
        subprocess.run([browser, f"--app={URL}", f"--user-data-dir={PROFILE}",
                        "--no-first-run", "--window-size=1440,900",
                        # lets the wake-word listener and spoken replies work before any click in the window
                        "--autoplay-policy=no-user-gesture-required"])
        # The call above returns at once if a JARVIS window was already open (the browser hands the request
        # to it), so wait for the windows themselves rather than for that one process.
        time.sleep(2)
        while window_open():
            time.sleep(1)
    else:
        webbrowser.open(URL)
        if server:
            server.wait()
finally:
    if server:
        server.terminate()
