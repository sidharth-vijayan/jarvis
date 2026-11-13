"""Backend check with a fake model (no Ollama needed). Run: .venv\\Scripts\\python -m tests.test_backend"""
import json
import os
import tempfile

os.environ["JARVIS_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from backend import llm, main  # noqa: E402

seen = {}


async def fake_stream(model, messages, think, tools=None):
    seen.update(model=model, messages=messages, think=think)
    yield {"message": {"thinking": "hmm"}}
    yield {"message": {"content": "Hello"}}
    yield {"message": {"content": ", sir."}, "done": True, "eval_count": 4, "eval_duration": 2_000_000_000}


async def broken_stream(model, messages, think, tools=None):
    raise RuntimeError("model 'nope' not found")
    yield


async def tool_stream(model, messages, think, tools=None):
    """A model that searches once, calls a tool that does not exist, then answers from the results."""
    seen.update(messages=messages, offered=[t["function"]["name"] for t in tools or []])
    if messages[-1]["role"] != "tool":
        yield {"message": {"tool_calls": [{"function": {"name": "web_search", "arguments": {"query": "news"}}},
                                          {"function": {"name": "format_disk", "arguments": {}}}]}, "done": True}
    else:
        yield {"message": {"content": "It rained [1]."}, "done": True, "eval_count": 3, "eval_duration": 1_000_000_000}


async def fake_search(query):
    from backend.tools import Result
    return Result(f"[1] Weather\nIt rained. Asked: {query}", [{"title": "Weather", "url": "https://example.com/w"}])


def events(response):
    return [json.loads(line) for line in response.text.splitlines()]


def demo():
    llm.chat_stream = fake_stream
    with TestClient(main.app) as c:
        # New conversation: streams start/thinking/tokens/done and stores both messages.
        ev = events(c.post("/api/chat", json={"message": "hi there", "mode": "balanced"}))
        assert [e["type"] for e in ev] == ["start", "thinking", "token", "token", "done"], ev
        cid = ev[0]["conversation_id"]
        assert ev[-1]["tokens_per_s"] == 2.0
        assert seen["think"] is True and seen["messages"][0]["role"] == "system"
        msgs = c.get(f"/api/conversations/{cid}/messages").json()
        assert [(m["role"], m["content"]) for m in msgs] == [("user", "hi there"), ("assistant", "Hello, sir.")]

        # Follow-up carries history; fast mode turns thinking off; mode picks the model.
        c.put("/api/settings", json={"model_fast": "tiny"})
        c.post("/api/chat", json={"message": "again", "conversation_id": cid, "mode": "fast"})
        assert seen["think"] is False and seen["model"] == "tiny"
        assert [m["content"] for m in seen["messages"][1:]] == ["hi there", "Hello, sir.", "again"]

        # Validation and errors.
        assert c.post("/api/chat", json={"message": ""}).status_code == 422
        assert c.post("/api/chat", json={"message": "x", "conversation_id": 999}).status_code == 404
        assert c.put("/api/settings", json={"mode": "turbo"}).status_code == 422
        llm.chat_stream = broken_stream
        ev = events(c.post("/api/chat", json={"message": "x", "conversation_id": cid}))
        assert ev[-1] == {"type": "error", "message": "model 'nope' not found"}

        # Tools: the call runs, an unknown tool is refused, the answer follows, real sources are appended.
        from backend import tools
        llm.chat_stream = tool_stream
        tools.REGISTRY["web_search"].run = fake_search
        ev = events(c.post("/api/chat", json={"message": "any news?", "conversation_id": cid}))
        assert [e["type"] for e in ev] == ["start", "tool", "tool_result", "tool", "tool_result", "token", "sources", "done"], ev
        assert ev[1]["label"] == 'Searching the web for "news"' and ev[2]["ok"] and not ev[4]["ok"]
        assert "web_search" in seen["offered"] and seen["messages"][-1]["content"] == "There is no tool called format_disk."
        saved = c.get(f"/api/conversations/{cid}/messages").json()[-1]["content"]
        assert saved == "It rained [1].\n\nSources:\n- [Weather](https://example.com/w)", saved

        # A destructive command is refused by the server without asking, and is recorded in the audit log.
        async def rm_stream(model, messages, think, tools=None):
            if messages[-1]["role"] != "tool":
                yield {"message": {"tool_calls": [{"function": {"name": "run_command", "arguments": {"command": "rm -r ~"}}}]}, "done": True}
            else:
                yield {"message": {"content": "I will not do that."}, "done": True}
        llm.chat_stream = rm_stream
        ev = events(c.post("/api/chat", json={"message": "wipe my files", "conversation_id": cid}))
        assert [e["type"] for e in ev] == ["start", "tool", "tool_result", "token", "done"], ev
        assert ev[2]["decision"] == "blocked" and not ev[2]["ok"]
        last = c.get("/api/audit").json()[0]
        assert (last["tool"], last["decision"], last["ok"]) == ("run_command", "blocked", 0), last
        assert c.post("/api/confirm/nope", json={"approve": True}).status_code == 404

        # Delete removes the conversation and its messages.
        assert c.delete(f"/api/conversations/{cid}").status_code == 204
        assert c.get("/api/conversations").json() == []
        assert c.get(f"/api/conversations/{cid}/messages").status_code == 404
        assert c.get("/").status_code == 200  # web UI is served

        # System stats are real percentages; the graph links only to nodes that exist.
        stats = c.get("/api/system").json()
        assert all(0 <= stats[k] <= 100 for k in ("cpu", "ram", "disk")), stats
        cid = events(c.post("/api/chat", json={"message": "graph me"}))[0]["conversation_id"]
        g = c.get("/api/graph").json()
        ids = {n["id"] for n in g["nodes"]}
        assert {"core", "hub:conversations", f"conversation:{cid}", "mode:fast"} <= ids
        assert all(link["source"] in ids and link["target"] in ids for link in g["links"])
    print("backend OK")


if __name__ == "__main__":
    demo()
