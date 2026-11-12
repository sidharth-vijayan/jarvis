"""Wake word check: spoken "Hey Jarvis" must trigger, other speech must not.
Run: .venv\\Scripts\\python -m tests.test_wake"""
import io

import numpy as np
from faster_whisper.audio import decode_audio
from fastapi.testclient import TestClient

from backend import main, voice


def frames(text, name):
    """Piper speaks `text`; returns it as the 80 ms 16 kHz frames the app streams, plus a second of silence."""
    audio = decode_audio(io.BytesIO(voice.speak(text, name)), sampling_rate=16000)
    pcm = (np.concatenate([audio, np.zeros(16000, np.float32)]) * 32767).astype(np.int16)
    return [pcm[i:i + 1280].tobytes() for i in range(0, len(pcm) - 1279, 1280)]


def demo():
    for name in voice.voices():
        detector = voice.WakeDetector()
        hit = max(detector.score(f) for f in frames("Hey Jarvis, what time is it?", name))
        detector.reset()
        miss = max(detector.score(f) for f in frames("The weather is quite nice today, is it not?", name))
        print(f"{name:<22} hey jarvis={hit:.2f}  other speech={miss:.2f}")
        assert hit >= voice.WAKE_THRESHOLD > miss, (name, hit, miss)

    # Once through the real websocket endpoint. The direct check above passed for this same audio,
    # so "wake" is certain to arrive; near-miss "score:" messages may come first.
    with TestClient(main.app) as c, c.websocket_connect("/api/wake") as ws:
        for frame in frames("Hey Jarvis.", voice.voices()[0]):
            ws.send_bytes(frame)
        while ws.receive_text() != "wake":
            pass
    print("wake OK")


if __name__ == "__main__":
    demo()
