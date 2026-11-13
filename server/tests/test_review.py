"""Session reviews (review.py): the checks on a session's records and payloads, and the AI review's own session."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from oidc_helpers import access_token, authenticated_client as TestClient

from agent_server import review, review_prompts
from agent_server.app import create_app
from agent_server.config import Backend, Settings


def make_app(tmp_path: Path, handler):
    settings = Settings(
        log_dir=tmp_path, default_backend="test",
        backends={"test": Backend("test", "http://ollama.test"), "claude": Backend("claude", "https://api.anthropic.com", "anthropic")},
        model_defaults={"translate_openai": False},
    )
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


def sse(text: str, usage: dict | None = None) -> bytes:
    chunks = [{"choices": [{"delta": {"content": text}}]}, {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage or {"prompt_tokens": 12, "completion_tokens": 3}}]
    return b"".join(b"data: " + json.dumps(chunk).encode() + b"\n\n" for chunk in chunks) + b"data: [DONE]\n\n"


def answer(text: str = "Done.", **usage):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=sse(text, usage or None))

    return handler


def call(number: int, name: str, arguments: dict, result: str) -> list[dict]:
    """A tool call and what came back, as an agent sends them again in its history."""
    return [
        {"role": "assistant", "content": None, "tool_calls": [{"id": f"call_{number}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]},
        {"role": "tool", "tool_call_id": f"call_{number}", "content": result},
    ]


def ask(client, session_id: str, messages: list[dict], tools: list[dict] | None = None) -> None:
    body = {"model": "model", "stream": True, "messages": messages, **({"tools": tools} if tools else {})}
    with client.stream("POST", f"/session/{session_id}/v1/chat/completions", json=body) as response:
        response.read()
        assert response.status_code == 200


def test_review_reads_the_prompt_and_tools_from_captured_payloads(tmp_path):
    app, client, old = make_app(tmp_path, answer(prompt_tokens=120_000, completion_tokens=40))
    try:
        session = client.post("/api/sessions", json={"client": "test", "backend": "test", "trace": True}).json()
        history = [{"role": "system", "content": "# Rules\n" + "Rules. " * 5_000 + "\n## Style\n" + "Style. " * 995}, {"role": "user", "content": "Fix it"}]
        for number in range(4):
            history += call(number, "read_file", {"path": "src/app.py"}, "print('hello')")
        history += call(10, "bash", {"command": "rg TODO src"}, "x" * 50_000)
        history += call(11, "bash", {"command": "pytest -q"}, "2 passed")
        history += call(12, "mcp__jira__search", {"query": "bug"}, "none")
        history += call(13, "edit_file", {"path": "src/app.py", "old": "a", "new": "b"}, "no match for old")
        history[-1]["content"] = [{"type": "tool_result", "tool_use_id": "call_13", "content": "no match for old", "is_error": True}]
        history[-1]["role"] = "user"
        history[-2] = {"role": "assistant", "content": [{"type": "tool_use", "id": "call_13", "name": "edit_file", "input": {"path": "src/app.py", "old": "a", "new": "b"}}]}
        tools = [{"type": "function", "function": {"name": name, "description": "A tool", "parameters": {"type": "object"}}} for name in ("read_file", "bash", "mcp__jira__search", "mcp__wiki__page")]
        ask(client, session["session_id"], history, tools)

        found = client.get(f"/api/sessions/{session['session_id']}/review").json()
        assert found["traced_calls"] == found["traffic"]["model_calls"] == 1
        assert found["traffic"]["peak_context_tokens"] == 120_000 and found["traffic"]["input_tokens"] == 120_000
        assert 10_400 < found["prompt"]["system_tokens"] < 10_600
        assert found["prompt"]["tools_defined"] == 4 and found["prompt"]["tools_unused"] == ["mcp__wiki__page"]
        assert [(item["name"], item["tools"], item["calls"]) for item in found["prompt"]["mcp_servers"]] == [("jira", 1, 1), ("wiki", 1, 0)]
        assert found["prompt"]["unused_tool_tokens"] > 0 and len(found["prompt"]["largest_tools"]) == 4
        # The system prompt's outline: one section per heading, largest first.
        assert [item["title"] for item in found["prompt"]["sections"]] == ["Rules", "Style"]
        # What the context holds, by kind, shares adding up.
        parts = {item["part"]: item for item in found["context"]}
        assert {"System prompt", "Tool results", "Tool calls", "Messages typed", "Tool definitions"} <= set(parts)
        assert abs(sum(item["share"] for item in found["context"]) - 1) < 0.01 and parts["Tool results"]["tokens"] > parts["Messages typed"]["tokens"]
        assert found["timeline"]["points"] == [{"call": 1, "request_id": "000001", "tokens": 120_000, "output_tokens": 40}]
        tools_used = found["tools"]
        assert tools_used["calls"] == 8 and tools_used["errors"] == 1
        # A shell command counts as what it does: rg is a search, pytest a shell command.
        assert tools_used["by_category"] == {"read": 4, "search": 1, "shell": 1, "mcp": 1, "edit": 1}
        assert tools_used["reread_files"] == [{"file": "src/app.py", "reads": 4}]
        # Reading a file again is one finding, not also a repeated call.
        assert tools_used["repeated_calls"] == []
        assert tools_used["large_results"] == [{"name": "bash", "target": "rg TODO src", "tokens": 12_500}]
        assert tools_used["edited_files"] == [{"file": "src/app.py", "edits": 1}] and tools_used["searches"] == ["rg TODO src"]
        assert tools_used["error_samples"] == [{"name": "edit_file", "target": "src/app.py", "error": "no match for old", "times": 1}]
        assert [(item["command"], item["calls"], item["result_tokens"]) for item in tools_used["commands"]] == [("rg TODO", 1, 12_500), ("pytest -q", 1, 2)]
        assert found["conversation"]["prompts"] == 1 and found["conversation"]["prompt_excerpts"] == ["Fix it"]
        # The main chat step by step: what was typed, then what the agent did.
        assert found["conversation"]["steps"] == [{"typed": "Fix it", "tool_calls": 8, "by_kind": {"read": 4, "search": 1, "shell": 1, "mcp": 1, "edit": 1}, "result_tokens": 12_519}]
        findings = {item["id"]: item for item in found["findings"]}
        assert {"system-prompt", "peak-context", "rereads", "large-results"} <= set(findings) and "untraced" not in findings
        assert findings["system-prompt"]["severity"] == "medium" and findings["rereads"]["severity"] == "low"
        # Worst first.
        order = [review.SEVERITIES.index(item["severity"]) for item in found["findings"]]
        assert order == sorted(order)
        assert all(item["detail"] and item["suggestion"] and item["area"] for item in found["findings"])
        # A finding says what it cost where that can be worked out, and the findings leave a score.
        assert findings["large-results"]["impact_tokens"] == 12_500 and findings["rereads"]["impact_tokens"] is None
        assert findings["system-prompt"]["impact_tokens"] == found["prompt"]["system_tokens"] - 8_000
        assert found["score"] == 100 - sum(review.PENALTIES[item["severity"]] for item in found["findings"]) and found["grade"]
    finally:
        close_app(app, client, old)


def test_review_without_payloads_checks_only_the_numbers(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "out of memory"})

    app, client, old = make_app(tmp_path, handler)
    try:
        session = client.post("/api/sessions", json={"client": "test", "backend": "test"}).json()
        for _ in range(3):
            client.post(f"/session/{session['session_id']}/v1/chat/completions", json={"model": "model", "messages": [{"role": "user", "content": "hi"}]})
        found = client.get(f"/api/sessions/{session['session_id']}/review").json()
        assert found["traced_calls"] == 0 and found["prompt"] is None and found["tools"] is None and found["conversation"] is None
        assert found["traffic"]["model_calls"] == found["traffic"]["failed_calls"] == 3
        assert [item["id"] for item in found["findings"]] == ["failed-calls", "untraced"]
        assert found["score"] == 91 and found["grade"] == "Efficient" and found["context"] is None

        empty = client.post("/api/sessions", json={"client": "test", "backend": "test"}).json()
        nothing = client.get(f"/api/sessions/{empty['session_id']}/review").json()
        assert nothing["findings"] == [] and nothing["score"] is None and nothing["timeline"]["points"] == []
    finally:
        close_app(app, client, old)


def test_checks_fire_at_their_thresholds():
    traffic = {
        "requests": 500, "model_calls": 420, "input_tokens": 900_000, "output_tokens": 1, "cache_read_tokens": 90_000, "cache_write_tokens": 0,
        "cached_share": 0.1, "idle_rewrites": 6, "idle_rewrite_tokens": 600_000, "peak_context_tokens": 95_000, "context_window": 100_000, "failed_calls": 0, "rate_limited_calls": 2,
        "incomplete_calls": 2, "median_first_token_ms": 20_000, "duration_seconds": 60, "usd": 0, "subscription_usd": 0, "models": [],
    }
    tools = {
        "calls": 100, "errors": 40, "result_tokens": 0, "by_category": {"search": 70, "read": 30},
        "by_name": [{"name": "grep", "category": "search", "calls": 70, "errors": 40, "result_chars": 0}],
        "reread_files": [], "large_result_tokens": 0, "repeated_calls": [{"name": "bash", "target": "make", "times": 7}], "large_results": [], "large_result_count": 0,
    }
    chat = {"conversations": 1, "subagents": 16, "background_calls": 0, "compactions": 3, "images": 5, "prompts": 25, "first_prompt_chars": 10, "prompt_excerpts": []}
    prompt = {"system_chars": 0, "system_tokens": 500, "system_start": "", "tools_defined": 40, "tool_definition_tokens": 30_000,
              "tools_unused": [f"mcp__idle__{n}" for n in range(39)], "unused_tool_tokens": 29_000, "mcp_servers": [{"name": "idle", "tools": 39, "calls": 0}]}
    measured = {"traced_calls": 420, "traffic": traffic, "tools": tools, "conversation": chat, "prompt": prompt,
                "timeline": {"calls": 400, "points": [], "growth_per_call": 6_000}}
    found = {item["id"]: item["severity"] for check in review.CHECKS for item in [check(measured)] if item}
    assert found == {
        "tool-definitions": "high", "unused-tools": "low", "peak-context": "high", "context-growth": "medium", "images": "low", "compactions": "high", "long-session": "high",
        "searching": "high", "tool-errors": "high", "repeated-calls": "medium", "subagents": "medium", "short-prompt": "low",
        "steering": "low", "rate-limited": "medium", "incomplete": "low", "caching": "medium", "idle-cache": "medium", "slow": "low",
    }
    quiet = {
        "traced_calls": 5, "tools": {**tools, "calls": 10, "errors": 0, "by_category": {"edit": 10}, "repeated_calls": []},
        "traffic": {**traffic, "model_calls": 5, "cached_share": 0.95, "peak_context_tokens": 20_000, "rate_limited_calls": 0, "incomplete_calls": 0, "median_first_token_ms": 900, "idle_rewrites": 0},
        "conversation": {**chat, "subagents": 0, "compactions": 0, "images": 0, "prompts": 2, "first_prompt_chars": 400},
        "prompt": {**prompt, "tools_defined": 12, "tool_definition_tokens": 3_000, "tools_unused": []},
        "timeline": {"calls": 5, "points": [], "growth_per_call": 900},
    }
    assert [check(quiet) for check in review.CHECKS if check(quiet)] == []
    # Every check has a name the prompts can list.
    assert set(found) | {"system-prompt", "rereads", "large-results", "failed-calls", "untraced"} == set(review_prompts.METRICS)


def test_each_prompt_sends_its_own_data_and_findings():
    from agent_server.sessions import Session

    session = Session("s", "claude-account", None, "claude-opus-5-5", "direct")
    traffic = {
        "requests": 12, "model_calls": 12, "input_tokens": 900_000, "output_tokens": 4_000, "cache_read_tokens": 90_000, "cache_write_tokens": 5_000,
        "cached_share": 0.1, "idle_rewrites": 2, "idle_rewrite_tokens": 200_000, "peak_context_tokens": 150_000, "context_window": None,
        "failed_calls": 0, "rate_limited_calls": 1, "incomplete_calls": 0, "median_first_token_ms": 900, "duration_seconds": 600,
        "usd": 1.5, "subscription_usd": 0, "models": [{"model": "claude-opus-5-5", "calls": 12, "input_tokens": 900_000, "output_tokens": 4_000, "usd": 1.5}],
    }
    found = {"traffic": traffic, "prompt": None, "conversation": None, "tools": None, "context": None, "score": 85, "grade": "Efficient",
             "timeline": {"calls": 2, "points": [{"call": 1, "tokens": 50_000}, {"call": 2, "tokens": 150_000}], "growth_per_call": 100_000},
             "findings": [
                 {"id": "rate-limited", "severity": "medium", "title": "Calls were rate limited", "detail": "One."},
                 {"id": "idle-cache", "severity": "low", "title": "The cache ran out", "detail": "Twice.", "impact_tokens": 200_000},
                 {"id": "untraced", "severity": "info", "title": "Payloads weren't captured", "detail": "None."},
             ]}
    shipped = review_prompts.prompts(review_prompts.DEFAULT_PROMPTS)
    assert [prompt.id for prompt in shipped] == ["overall", "context", "instructions", "tools", "prompting", "growth", "cost", "reliability", "project"]
    # Every name a prompt can list has lines behind it, and every check a name.
    assert set(review.SECTIONS) == set(review_prompts.DATA)
    for prompt in shipped:
        system, user = review.brief(session, found, prompt)
        assert prompt.focus in system["content"] and "## What to change" in system["content"] and f"Up to {prompt.suggestions} numbered" in system["content"]
        # The note that payloads are missing goes with every prompt.
        assert "Payloads weren't captured" in user["content"]
        assert ("Calls were rate limited" in user["content"]) == ("rate-limited" in prompt.metrics)
        assert ("Estimated cost" in user["content"]) == ("cost" in prompt.data)
        assert ("call number: tokens read" in user["content"]) == ("timeline" in prompt.data)
        if "idle-cache" in prompt.metrics:
            assert "About 200,000 tokens over the session." in user["content"]
        public = prompt.public()
        assert len(public["data"]) == len(prompt.data) and len(public["metrics"]) == len(prompt.metrics) and public["instructions"] == system["content"]
    asked = review.brief(session, found, shipped[0], "Was  the model\ntoo big? " + "x" * 600)[1]["content"]
    assert "The engineer also asks: Was the model too big? xxx" in asked and asked.endswith("x") and len(asked.split("also asks: ")[1]) == 500
    assert review_prompts.named(review_prompts.DEFAULT_PROMPTS, None).id == "overall"
    assert review_prompts.named(review_prompts.DEFAULT_PROMPTS, "cost").title == "Cost and caching"


def test_review_prompts_are_a_setting_site_admins_change(tmp_path):
    from agent_server import config_io

    app, client, old = make_app(tmp_path, answer("Fine."))
    try:
        settings = app.state.runtime.settings
        shipped = client.get("/api/config/review-prompts").json()
        assert shipped["custom"] is False and len(shipped["prompts"]) == 9 and "timeline" in shipped["data"] and "caching" in shipped["metrics"]
        assert "id: overall" in client.get("/api/config/review-prompts?format=yaml").text

        mine = """
prompts:
  - id: security
    title: Secrets and risky commands
    about: What the agent ran that deserves a second look.
    focus: Look at the shell commands and say which could have changed something outside the project.
    data: [session, commands, errors]
    metrics: [tool-errors, repeated-calls]
    suggestions: 3
  - id: everything
    title: Everything
    about: All of it.
    focus: Look at everything.
    data: all
    metrics: all
"""
        saved = client.put("/api/config/review-prompts", content=mine).json()
        assert saved["custom"] is True and [item["id"] for item in saved["prompts"]] == ["security", "everything"]
        assert saved["prompts"][1]["data"] == list(review_prompts.DATA) and saved["prompts"][0]["summary"] == review_prompts.DEFAULT_SUMMARY
        assert config_io.export(settings)["review_prompts"] == saved["prompts"]

        # A session's Review tab offers them, and a review is asked with one.
        session = client.post("/api/sessions", json={"client": "test", "backend": "test", "trace": True}).json()
        ask(client, session["session_id"], [{"role": "user", "content": "hello"}, *call(1, "bash", {"command": "rm -rf build"}, "done")])
        base = f"/api/sessions/{session['session_id']}"
        offered = client.get(f"{base}/review").json()["prompts"]
        assert [item["title"] for item in offered] == ["Secrets and risky commands", "Everything"]
        assert offered[0]["data"] == ["The agent, model and length", "Shell commands run", "Failed tool calls and their errors"]
        made = client.post(f"{base}/reviews", json={"backend": "test", "model": "m", "question": "Anything destructive?"}).json()
        assert made["prompt"] == "security" and "which could have changed something outside the project" in made["messages"][0]["content"]
        assert "rm -rf build" in made["messages"][1]["content"] and "Up to 3 numbered" in made["messages"][0]["content"]
        assert made["messages"][1]["content"].endswith("The engineer also asks: Anything destructive?")
        assert client.post(f"{base}/reviews", json={"backend": "test", "model": "m", "prompt": "overall"}).status_code == 400

        # What's wrong is said; nothing is saved.
        for bad, why in (
            ("prompts: [{id: a, title: A, about: A, focus: Look, data: [nonsense]}]", "no such data: nonsense"),
            ("prompts: [{id: a, title: A, about: A, focus: Look, metrics: [nope]}]", "no such metrics: nope"),
            ("prompts: [{id: 'Bad Id', title: A, about: A, focus: Look}]", "lowercase letters"),
            ("prompts: [{id: a, title: A, about: A}]", "focus is text"),
            ("prompts: []", "1 to 40"),
            ("other: 1", "prompts"),
        ):
            refused = client.put("/api/config/review-prompts", content=bad)
            assert refused.status_code == 400 and why in refused.json()["detail"], refused.text
        assert client.get("/api/config/review-prompts").json()["prompts"] == saved["prompts"]

        # Only site admins; and null goes back to the shipped set.
        store = app.state.runtime.auth.store
        store.add_org("acme", "Acme")
        store.add_user("dave", "test-only-password", role="admin", tenant="acme")
        dave = {"x-ai-proxy-token": access_token(app, "dave")}
        assert client.get("/api/config/review-prompts", headers=dave).status_code == 403
        assert client.put("/api/config/review-prompts", content=mine, headers=dave).status_code == 403
        assert client.put("/api/config/review-prompts", json={"prompts": None}).json()["custom"] is False
        assert len(client.get(f"{base}/review").json()["prompts"]) == 9
    finally:
        close_app(app, client, old)


def test_ai_review_is_a_session_of_its_own(tmp_path):
    app, client, old = make_app(tmp_path, answer("## Summary\nA tidy session."))
    try:
        session = client.post("/api/sessions", json={"client": "test", "backend": "test", "trace": True}).json()
        ask(client, session["session_id"], [{"role": "user", "content": "Rename the helper in util.py"}])
        base = f"/api/sessions/{session['session_id']}"
        # Only backends that take OpenAI Chat with the server's sign-in: not the Anthropic pass-through.
        assert client.get(f"{base}/review").json()["reviewers"] == ["test"]
        assert client.post(f"{base}/reviews", json={"backend": "claude", "model": "m"}).status_code == 400
        assert client.post(f"{base}/reviews", json={"backend": "test"}).status_code == 400
        assert client.post(f"{base}/reviews", json={"backend": "test", "model": "m", "prompt": "nope"}).status_code == 400
        assert [item["id"] for item in client.get(f"{base}/review").json()["prompts"]][:2] == ["overall", "context"]
        focused = client.post(f"{base}/reviews", json={"backend": "test", "model": "m", "prompt": "tools"}).json()
        assert focused["prompt"] == "tools" and "Look only at how the agent used its tools" in focused["messages"][0]["content"]
        client.delete(f"/api/sessions/{focused['session']['session_id']}")

        started = client.post(f"{base}/reviews", json={"backend": "test", "model": "reviewer"})
        assert started.status_code == 201
        made = started.json()
        assert made["session"]["client"] == review.REVIEW_CLIENT and made["session"]["tags"]["review_of"] == session["session_id"]
        assert made["session"]["tags"]["visibility"] == "private" and made["session"]["trace"] is True
        assert [message["role"] for message in made["messages"]] == ["system", "user"]
        assert "Rename the helper in util.py" in made["messages"][1]["content"] and "## What to change" in made["messages"][0]["content"]

        listed = client.get(f"{base}/review").json()["reviews"]
        assert [item["session_id"] for item in listed] == [made["session"]["session_id"]] and listed[0]["model"] == "reviewer"
        assert listed[0]["prompt"] == "The whole session"
        one = f"{base}/reviews/{made['session']['session_id']}"
        assert client.get(one).json()["text"] is None
        ask(client, made["session"]["session_id"], made["messages"])
        assert client.get(one).json()["text"] == "## Summary\nA tidy session."

        # Only the server says what a session reviews; and a review belongs to the session it names.
        forged = client.post("/api/sessions", json={"client": review.REVIEW_CLIENT, "backend": "test", "tags": {"review_of": session["session_id"]}}).json()
        assert "review_of" not in forged["tags"] and len(client.get(f"{base}/review").json()["reviews"]) == 1
        assert client.get(f"/api/sessions/{forged['session_id']}/reviews/{made['session']['session_id']}").status_code == 404
        assert client.get(f"{base}/reviews/{session['session_id']}").status_code == 404
    finally:
        close_app(app, client, old)


def test_reviews_follow_who_may_see_the_session(tmp_path):
    app, client, old = make_app(tmp_path, answer("Fine."))
    try:
        store = app.state.runtime.auth.store
        store.add_org("acme", "Acme")
        store.add_user("alice", "test-only-password", tenant="acme")
        store.add_user("bob", "test-only-password", tenant="acme")
        # The site's provider, shared with every organization, so Acme's people have one to review with.
        app.state.runtime.settings.set_shared_models({"test": ["*"]})
        alice = {"x-ai-proxy-token": access_token(app, "alice")}
        bob = {"x-ai-proxy-token": access_token(app, "bob")}
        private = client.post("/api/sessions", json={"client": "test", "backend": "test", "trace": True}, headers=alice).json()
        shared = client.post("/api/sessions", json={"client": "test", "backend": "test", "trace": True, "visibility": "org"}, headers=alice).json()
        for path in ("/review", "/reviews/anything"):
            assert client.get(f"/api/sessions/{private['session_id']}{path}", headers=bob).status_code == 404
        assert client.post(f"/api/sessions/{private['session_id']}/reviews", json={"backend": "test", "model": "m"}, headers=bob).status_code == 404

        # Bob may review what Alice shared, and his review is his own: Alice doesn't see it.
        made = client.post(f"/api/sessions/{shared['session_id']}/reviews", json={"backend": "test", "model": "m"}, headers=bob).json()
        assert made["session"]["tags"]["owner"] == "bob"
        assert len(client.get(f"/api/sessions/{shared['session_id']}/review", headers=bob).json()["reviews"]) == 1
        assert client.get(f"/api/sessions/{shared['session_id']}/review", headers=alice).json()["reviews"] == []
        assert client.get(f"/api/sessions/{shared['session_id']}/reviews/{made['session']['session_id']}", headers=alice).status_code == 404
    finally:
        close_app(app, client, old)
