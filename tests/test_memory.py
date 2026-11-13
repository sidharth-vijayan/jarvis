"""Memory checks. Run: .venv\\Scripts\\python -m tests.test_memory"""
import asyncio
import json
import os
import tempfile

os.environ["JARVIS_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from backend import desktop, llm, main, tools  # noqa: E402

seen = {}


async def fake_stream(model, messages, think, tools=None):
    seen.update(system=messages[0]["content"], offered=[t["function"]["name"] for t in tools or []])
    if messages[-1]["role"] != "tool" and messages[-1]["content"] == "forget it anyway":
        yield {"message": {"tool_calls": [{"function": {"name": "remember", "arguments": {"fact": "x y z", "category": "fact"}}}]}, "done": True}
    else:
        yield {"message": {"content": "Noted."}, "done": True}


def demo():
    llm.chat_stream = fake_stream
    run = lambda name, **args: asyncio.run(tools.run(name, args))  # noqa: E731
    with TestClient(main.app) as c:
        # Saving: stored once, bad categories fall back, secrets are refused.
        assert run("remember", fact="The user's manager is Priya.", category="person").text == "Saved to memory."
        assert run("remember", fact="The user's manager is Priya.", category="person").text == "That was already in memory."
        assert run("remember", fact="The user prefers short replies.", category="nonsense").ok
        assert run("remember", fact="The user's bank password is hunter2.", category="fact").ok is False
        mems = c.get("/api/memories").json()
        assert [(m["text"], m["category"]) for m in mems] == [("The user prefers short replies.", "fact"), ("The user's manager is Priya.", "person")]

        # Memories reach the model, and the memory tools are offered.
        c.post("/api/chat", json={"message": "hello"})
        assert "- The user's manager is Priya." in seen["system"] and "remember" in seen["offered"]

        # Past conversations can be searched.
        r = run("recall_conversations", query="hello")
        assert r.ok and "hello" in r.text and run("recall_conversations", query="zebra unicorn").text.startswith("No earlier")

        # Editing and deleting from the interface.
        mid = mems[1]["id"]
        assert c.put(f"/api/memories/{mid}", json={"text": "The user's manager is Anil."}).status_code == 200
        assert c.put("/api/memories/9999", json={"text": "x"}).status_code == 404

        # Forgetting shows exactly what will go, and removes only that.
        try:
            asyncio.run(desktop._forget_preview("unicorn"))
            raise AssertionError("preview for nothing")
        except ValueError:
            pass
        assert asyncio.run(desktop._forget_preview("manager")).endswith("  - The user's manager is Anil.")
        assert run("forget", about="manager").ok
        assert [m["text"] for m in c.get("/api/memories").json()] == ["The user prefers short replies."]

        # Switched off: nothing is injected, the tools are hidden, and a call to one is refused and not run.
        c.put("/api/settings", json={"memory_enabled": "0"})
        ev = [json.loads(x) for x in c.post("/api/chat", json={"message": "forget it anyway"}).text.splitlines()]
        assert "prefers short replies" not in seen["system"] and not set(desktop.MEMORY_TOOLS) & set(seen["offered"])
        result = next(e for e in ev if e["type"] == "tool_result")
        assert result["decision"] == "blocked" and len(c.get("/api/memories").json()) == 1

        # The graph shows memories.
        assert any(n["type"] == "memory" for n in c.get("/api/graph").json()["nodes"])
    print("memory OK")


if __name__ == "__main__":
    demo()
