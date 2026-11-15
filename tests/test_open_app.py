"""'Open <app>' commands, with a fake list of installed apps so nothing is launched.
Run: python -m tests.test_open_app"""
import asyncio

from backend import desktop, main

APPS = [{"Name": n, "AppID": n} for n in ("WhatsApp", "Brave", "Spotify", "Google Chrome", "Visual Studio Code",
                                         "Notepad", "Settings", "Snipping Tool")]


def check():
    desktop.installed_apps = lambda: APPS
    opens = lambda text: asyncio.run(main.app_to_open(text))  # noqa: E731
    for said, app in [("Open WhatsApp.", "WhatsApp"), ("open whats app", "WhatsApp"), ("open what's up", "WhatsApp"),
                      ("Hey Jarvis, open Brave", "Brave"), ("Can you launch spotify?", "Spotify"),
                      ("Open the Spotify app please", "Spotify"), ("open chrome", "Google Chrome"),
                      ("start VS Code", "Visual Studio Code"), ("open snipping", "Snipping Tool")]:
        assert opens(said) == app, (said, opens(said))
    # not a clear app name: left to the model, which can open websites, folders and so on
    for said in ("open google.com", "open my documents folder", "start a timer for 5 minutes", "open s",
                 "open the pod bay doors", "what's up", "send a message to Sam"):
        assert opens(said) is None, (said, opens(said))
    print("open app OK")


if __name__ == "__main__":
    check()
