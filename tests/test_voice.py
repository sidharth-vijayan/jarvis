"""Real speech round trip: Piper speaks a sentence, Whisper must hear it back.
Needs the installed voices and Whisper model. Run: .venv\\Scripts\\python -m tests.test_voice"""
import time

from fastapi.testclient import TestClient

from backend import main, voice


def demo():
    with TestClient(main.app) as c:
        names = c.get("/api/voice").json()["voices"]
        assert names, "no Piper voices in data/voices"
        t = time.time()
        r = c.post("/api/tts", json={"text": "Remind me to call the bank tomorrow at nine.", "voice": names[0]})
        assert r.status_code == 200 and r.content[:4] == b"RIFF"
        t_tts = time.time() - t
        t = time.time()
        heard = c.post("/api/stt", content=r.content).json()["text"]
        t_stt = time.time() - t
        assert "call the bank tomorrow" in heard.lower(), heard
        t = time.time()
        c.post("/api/stt", content=r.content)
        print(f"heard: {heard!r}  tts {t_tts:.1f}s  stt first {t_stt:.1f}s, warm {time.time() - t:.1f}s")

        assert c.post("/api/tts", json={"text": "hi", "voice": "..\\..\\secrets"}).status_code == 404
        assert c.post("/api/tts", json={"text": "hi", "voice": names[0], "speed": 9}).status_code == 422
        assert c.post("/api/stt", content=b"").status_code == 400
        assert c.post("/api/stt", content=b"not audio").status_code == 400
    print("voice OK")


if __name__ == "__main__":
    demo()
