"""Ollama client. The only LLM provider: the project is fully local by decision.
Add a second provider here (same two functions) if that ever changes."""
import json
import os

import httpx

OLLAMA_URL = os.environ.get("JARVIS_OLLAMA_URL", "http://127.0.0.1:11434")
# How long Ollama keeps a model in memory after its last use. Loading takes 10-20 s, so keep it warm.
KEEP_ALIVE = os.environ.get("JARVIS_KEEP_ALIVE", "30m")
NUM_CTX = 8192


async def preload(model: str):
    """Ask Ollama to load a model now, so the first request does not wait for it. Failures are ignored."""
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            # Same context size as chat_stream: a different one would make Ollama reload the model on first use.
            await client.post(f"{OLLAMA_URL}/api/generate",
                              json={"model": model, "keep_alive": KEEP_ALIVE, "options": {"num_ctx": NUM_CTX}})
    except httpx.HTTPError:
        pass


async def list_models() -> list[str]:
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{OLLAMA_URL}/api/tags")
        r.raise_for_status()
        return sorted(m["name"] for m in r.json()["models"])


async def loaded_models() -> list[str]:
    """Models Ollama currently holds in memory."""
    async with httpx.AsyncClient(timeout=5) as client:
        r = await client.get(f"{OLLAMA_URL}/api/ps")
        r.raise_for_status()
        return [m["name"] for m in r.json()["models"]]


async def chat_stream(model: str, messages: list[dict], think: bool):
    """Yield Ollama's streamed chat chunks as dicts."""
    body = {"model": model, "messages": messages, "stream": True, "think": think,
            "keep_alive": KEEP_ALIVE, "options": {"num_ctx": NUM_CTX}}
    async with httpx.AsyncClient(timeout=httpx.Timeout(10, read=600)) as client:
        async with client.stream("POST", f"{OLLAMA_URL}/api/chat", json=body) as r:
            if r.status_code != 200:
                text = (await r.aread()).decode(errors="replace")
                try:
                    text = json.loads(text)["error"]
                except (ValueError, KeyError, TypeError):
                    pass
                raise RuntimeError(text[:300])
            async for line in r.aiter_lines():
                if line:
                    yield json.loads(line)
