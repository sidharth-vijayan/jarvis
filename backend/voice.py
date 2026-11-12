"""Local speech: Whisper (speech-to-text) and Piper (text-to-speech).
Both run on the CPU so the GPU's memory stays free for the language model."""
import io
import os
import threading
import wave
from pathlib import Path

VOICE_DIR = Path(os.environ.get("JARVIS_VOICES", Path(__file__).parent.parent / "data" / "voices"))
# Measured on this laptop for a 3 s clip: base.en 0.65 s, small.en 2.0 s. small.en is more accurate.
WHISPER_MODEL = os.environ.get("JARVIS_WHISPER_MODEL", "small.en")
# Wake-word score (0..1) needed to trigger. Lower = triggers more easily, with more false alarms.
WAKE_THRESHOLD = float(os.environ.get("JARVIS_WAKE_THRESHOLD", "0.5"))
WAKE_FRAME_BYTES = 2560  # 80 ms of 16 kHz 16-bit mono audio, the frame size the detector expects


class WakeDetector:
    """Streaming "hey jarvis" detector (openWakeWord). One per connection: it keeps audio state."""

    def __init__(self):
        from openwakeword.model import Model
        self.model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")

    def score(self, frame: bytes) -> float:
        import numpy as np
        return float(max(self.model.predict(np.frombuffer(frame, dtype=np.int16)).values()))

    def reset(self):
        self.model.reset()

_stt_lock, _tts_lock = threading.Lock(), threading.Lock()
_whisper = None
_voices = {}
_warming = False


def voices() -> list[str]:
    return sorted(p.name.removesuffix(".onnx") for p in VOICE_DIR.glob("*.onnx"))


def _load_whisper():
    global _whisper
    with _stt_lock:
        if _whisper is None:
            from faster_whisper import WhisperModel
            _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8", cpu_threads=8)
        return _whisper


def warm():
    """Load Whisper in the background so the first spoken request is not slow."""
    global _warming
    if not _warming:
        _warming = True
        threading.Thread(target=_load_whisper, daemon=True).start()


def transcribe(audio: bytes) -> str:
    """Audio file bytes (any format ffmpeg reads, e.g. the browser's webm) -> text. Nothing is stored."""
    model = _load_whisper()
    with _stt_lock:
        segments, _ = model.transcribe(io.BytesIO(audio), beam_size=1, vad_filter=True,
                                       condition_on_previous_text=False)
        return " ".join(s.text.strip() for s in segments).strip()


def speak(text: str, voice: str, speed: float = 1.0) -> bytes:
    """Text -> WAV bytes. `voice` must be one of voices()."""
    if voice not in voices():  # also keeps `voice` from being used as an arbitrary path
        raise ValueError(f"Voice not installed: {voice}")
    from piper import PiperVoice, SynthesisConfig
    with _tts_lock:
        if voice not in _voices:
            _voices[voice] = PiperVoice.load(VOICE_DIR / f"{voice}.onnx")
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            _voices[voice].synthesize_wav(text, wav, syn_config=SynthesisConfig(length_scale=1 / speed))
        return buffer.getvalue()
