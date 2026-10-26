from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from agent_server.app import create_app
from agent_server.chat import Chats, request_turns, response_turn
from agent_server.config import Backend, Settings
from agent_server.sessions import SessionStore


def make_app(tmp_path: Path, handler, **settings_args):
    settings = Settings(log_dir=tmp_path, default_backend="test", backends={"test": Backend("test", "http://ollama.test")}, **settings_args)
    app = create_app(settings)
    client = TestClient(app)
    client.__enter__()
    old_client = app.state.runtime.client
    app.state.runtime.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return app, client, old_client


def close_app(app, client, old_client):
    import asyncio

    asyncio.run(app.state.runtime.client.aclose())
    app.state.runtime.client = old_client
    client.__exit__(None, None, None)


SYSTEM = {"role": "system", "content": "You are a coding agent."}
ASK = {"role": "user", "content": "Read the readme"}
CALL = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "read", "arguments": '{"path": "README.md"}'}}]}
RESULT = {"role": "tool", "tool_call_id": "call_1", "content": "# Hello"}


def sse(*chunks: dict) -> bytes:
    return b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks) + b"data: [DONE]\n\n"


def test_session_chat_follows_the_main_conversation_and_links_replies_to_calls(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        last = body["messages"][-1]
        if last.get("content") == "Title this chat":
            chunks = [{"choices": [{"delta": {"content": "Readme"}}]}]
        elif last["role"] == "tool":
            chunks = [{"choices": [{"delta": {"content": "It says hello."}}]}]
        else:
            chunks = [
                {"choices": [{"delta": {"reasoning_content": "Look at it first."}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", "function": {"name": "read", "arguments": '{"path": '}}]}}]},
                {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"README.md"}'}}]}}]},
            ]
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(*chunks))

    app, client, old = make_app(tmp_path, handler, model_defaults={"translate_openai": False})
    try:
        session = client.post("/api/sessions", json={"client": "test", "backend": "test", "trace": True}).json()
        url = f"/session/{session['session_id']}/v1/chat/completions"
        for messages in ([SYSTEM, ASK], [SYSTEM, {"role": "user", "content": "Title this chat"}], [SYSTEM, ASK, CALL, RESULT]):
            with client.stream("POST", url, json={"model": "model", "stream": True, "messages": messages}) as response:
                response.read()

        chat = client.get(f"/api/sessions/{session['session_id']}/chat").json()
        assert chat["model_calls"] == chat["traced_calls"] == 3
        main = next(thread for thread in chat["threads"] if thread["main"])
        assert main["requests"] == ["000001", "000003"] and main["title"] == "Read the readme"
        assert chat["thread"] == main["id"]
        roles = [turn["role"] for turn in chat["turns"]]
        assert roles == ["system", "user", "assistant", "tool", "assistant"]
        call, result, reply = chat["turns"][2], chat["turns"][3], chat["turns"][4]
        assert call["request_id"] == "000001"
        assert call["blocks"] == [{"type": "tool_use", "id": "call_1", "name": "read", "input": {"path": "README.md"}}]
        assert result["blocks"][0] == {"type": "tool_result", "tool_use_id": "call_1", "is_error": False, "text": "# Hello"}
        assert reply == {"role": "assistant", "blocks": [{"type": "text", "text": "It says hello."}], "request_id": "000003", "index": 4}
        assert chat["total"] == 5 and chat["start"] == 0

        # Pages come newest first; "before" walks back through the older ones.
        base = f"/api/sessions/{session['session_id']}/chat"
        newest = client.get(base, params={"limit": 2}).json()
        assert (newest["start"], [turn["index"] for turn in newest["turns"]]) == (3, [3, 4])
        older = client.get(base, params={"limit": 2, "before": newest["start"]}).json()
        assert [turn["index"] for turn in older["turns"]] == [1, 2]
        assert client.get(f"{base}/turns/3", params={"thread": main["id"]}).json()["blocks"][0]["text"] == "# Hello"
        assert client.get(f"{base}/turns/9", params={"thread": main["id"]}).status_code == 404

        title = next(thread for thread in chat["threads"] if not thread["main"])
        other = client.get(f"/api/sessions/{session['session_id']}/chat", params={"thread": title["id"]}).json()
        assert other["turns"][-1]["blocks"] == [{"type": "text", "text": "Readme"}]
        # The index is kept beside the traces and goes when the session's data is cleared.
        assert (tmp_path / "sessions" / session["session_id"] / "chat-index.json").exists()
        assert client.delete(f"/api/sessions/{session['session_id']}/data").status_code == 200
        assert client.get(f"/api/sessions/{session['session_id']}/chat").json()["threads"] == []
        assert client.get("/api/sessions/missing/chat").status_code == 404
    finally:
        close_app(app, client, old)


def test_calls_sent_with_only_new_messages_join_the_chat_their_replies_appear_in(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create({"client": "claude", "backend": "direct", "trace": True})
    tool_use = {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "ls"}}
    calls = [
        # Claude Code sometimes sends a call with just the latest exchange.
        ({"model": "m", "system": [{"type": "text", "text": "header 1"}], "messages": [{"role": "user", "content": "ls please"}]},
         [{"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {}}},
          {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"command": "ls"}'}}]),
        ({"model": "m", "system": [{"type": "text", "text": "header 2"}], "messages": [
            {"role": "user", "content": [{"type": "text", "text": "<system-reminder>be nice</system-reminder>", "cache_control": {"type": "ephemeral"}}, {"type": "text", "text": "start"}]},
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "ls please"},
            {"role": "assistant", "content": [tool_use]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "a.txt"}]}]},
        ]}, [{"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
             {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "One file."}}]),
    ]
    records = []
    for number, (request, response) in enumerate(calls, 1):
        request_id = f"{number:06d}"
        records.append({"request_id": request_id, "endpoint": "/v1/messages?beta=true", "timestamp": f"2026-01-01T00:00:0{number}Z"})
        store.write_trace(session, request_id, {"metadata": {}, "request": request, "response": response})
    records.append({"request_id": "000003", "endpoint": "/api/event_logging/v2/batch"})

    chat = Chats(store).view(session, records)
    assert [thread["requests"] for thread in chat["threads"]] == [["000001", "000002"]]
    assert chat["threads"][0]["title"] == "start"
    turns = chat["turns"]
    assert [turn["role"] for turn in turns] == ["system", "user", "assistant", "user", "assistant", "tool", "assistant"]
    assert turns[1]["blocks"][0]["reminder"] is True
    assert turns[4]["blocks"] == [tool_use] and turns[4]["request_id"] == "000001"
    assert "request_id" not in turns[2]
    assert turns[5]["blocks"][0]["text"] == "a.txt"
    assert turns[6]["blocks"] == [{"type": "text", "text": "One file."}] and turns[6]["request_id"] == "000002"


def test_pages_carry_previews_of_long_texts_and_the_full_turn_has_them(tmp_path):
    store = SessionStore(tmp_path)
    session = store.create({"client": "claude", "backend": "direct", "trace": True})
    long = "x" * 5000
    request = {"model": "m", "messages": [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "Write", "input": {"file_path": "a.txt", "content": long}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": long}]},
    ]}
    store.write_trace(session, "000001", {"metadata": {}, "request": request, "response": None})
    records = [{"request_id": "000001", "endpoint": "/v1/messages"}]
    chats = Chats(store)
    page = chats.view(session, records)
    call, result = page["turns"][1]["blocks"][0], page["turns"][2]["blocks"][0]
    assert call["preview"] and call["input"]["file_path"] == "a.txt" and len(call["input"]["content"]) == 300
    assert result["preview"] and len(result["text"]) == 2000
    full = chats.turn(session, records, page["thread"], 2)
    assert full["blocks"][0]["text"] == long and "preview" not in full["blocks"][0]


def test_responses_and_ollama_formats_read_as_turns():
    turns = request_turns({
        "instructions": "Be brief.",
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]},
            {"type": "function_call", "call_id": "c1", "name": "shell", "arguments": '{"cmd": "ls"}'},
            {"type": "function_call_output", "call_id": "c1", "output": "a.txt"},
        ],
    })
    assert [turn["role"] for turn in turns] == ["system", "user", "assistant", "tool"]
    assert [block["type"] for block in turns[2]["blocks"]] == ["thinking", "tool_use"]
    completed = [{"type": "response.output_text.delta", "delta": "x"}, {"type": "response.completed", "response": {"output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "Done."}]}]}}]
    assert response_turn(completed)["blocks"] == [{"type": "text", "text": "Done."}]
    ollama = [{"message": {"role": "assistant", "thinking": "hm", "content": ""}}, {"message": {"role": "assistant", "content": "Hi"}}]
    assert response_turn(ollama)["blocks"] == [{"type": "thinking", "text": "hm"}, {"type": "text", "text": "Hi"}]
    assert response_turn({"error": "backend down"})["blocks"][0]["type"] == "error"
