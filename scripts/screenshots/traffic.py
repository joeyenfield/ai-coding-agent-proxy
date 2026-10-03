"""Drive realistic agent traffic through the proxy for screenshots and smoke testing."""
import json
import sys
import threading

import httpx

PROXY = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8199"
TOOLS = [{"type": "function", "function": {"name": "read_file", "description": "Read a file from the project", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}]
client = httpx.Client(timeout=600)


def session(agent, model, backend, project):
    return client.post(f"{PROXY}/api/sessions", json={"client": agent, "model": model, "backend": backend, "trace": True, "project": project}).json()["session_id"]


def openai(sid, model, prompt, tools=False, max_tokens=300):
    body = {"model": model, "stream": True, "stream_options": {"include_usage": True}, "max_tokens": max_tokens,
            "messages": [{"role": "system", "content": "You are a coding agent working in a Python repository."}, {"role": "user", "content": prompt}]}
    if tools:
        body["tools"] = TOOLS
    with client.stream("POST", f"{PROXY}/session/{sid}/v1/chat/completions", json=body) as response:
        for _ in response.iter_lines():
            pass
    print("openai", model, response.status_code, flush=True)


def anthropic(sid, model, prompt):
    body = {"model": model, "max_tokens": 300, "stream": True, "system": "You are Claude Code running against a local model.",
            "messages": [{"role": "user", "content": prompt}],
            "tools": [{"name": "Read", "description": "Read a file", "input_schema": TOOLS[0]["function"]["parameters"]}]}
    with client.stream("POST", f"{PROXY}/session/{sid}/anthropic/v1/messages", json=body) as response:
        for _ in response.iter_lines():
            pass
    print("anthropic", model, response.status_code, flush=True)


def native(sid, model, prompt):
    response = client.post(f"{PROXY}/session/{sid}/api/chat", json={"model": model, "stream": False, "messages": [{"role": "user", "content": prompt}], "options": {"num_predict": 200}})
    print("ollama", model, response.status_code, flush=True)


mode = sys.argv[2] if len(sys.argv) > 2 else "seed"
if mode == "seed":
    qwen = session("qwen", "qwen3.6:27b-coding", "desktop", "bmo-ai")
    opencode = session("opencode", "qwen3.8:latest", "desktop", "pygame-tetris")
    claude = session("claude", "qwen3.5:4b", "laptop", "ai-coding-agent-proxy")
    aider = session("aider", "qwen3.5:2b", "laptop", "dotfiles")
    jobs = [
        lambda: [openai(qwen, "qwen3.6:27b-coding", p, tools=True) for p in (
            "Open src/main.py and explain the entry point.",
            "Read tests/test_game.py and tell me which test is flaky.",
            "Write a function that parses a .env file into a dict. Reply with code only.",
        )],
        lambda: [anthropic(claude, "qwen3.5:4b", p) for p in (
            "Read README.md and summarise it in two sentences.",
            "What does a 409 from DELETE /api/sessions mean?",
            "Suggest a commit message for splitting a monolith into api and ui folders.",
        )],
        lambda: [native(aider, "qwen3.5:2b", p) for p in ("Write a bash alias for git status.", "Explain what tmux does in one line.")],
    ]
    threads = [threading.Thread(target=job) for job in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    for prompt in ("Add a hold-piece feature to the Tetris game. Outline the steps.", "How should the game loop handle a paused state?"):
        openai(opencode, "qwen3.8:latest", prompt)
elif mode == "think":
    # Two reasoning-heavy requests at once, for the Live page.
    sid = session("qwen", "qwen3.5:4b", "laptop", "bmo-ai")
    sid2 = session("opencode", "qwen3.6:27b-coding", "desktop", "pygame-tetris")
    jobs = [
        threading.Thread(target=openai, args=(sid, "qwen3.5:4b", "A train leaves at 9:40 and arrives at 13:05 after two 12 minute stops. How long was it moving? Think it through, then give the answer and a Python function that does the calculation.", True, 2500)),
        threading.Thread(target=openai, args=(sid2, "qwen3.6:27b-coding", "Read src/board.py and then refactor the line-clear logic into its own function.", True, 1500)),
    ]
    for job in jobs:
        job.start()
    for job in jobs:
        job.join()
else:
    # A long generation that stays in flight while screenshots are taken.
    sid = [s for s in client.get(f"{PROXY}/api/sessions").json() if s["client"] == "opencode"][0]["session_id"]
    openai(sid, "qwen3.8:latest", "Write a detailed, 1500 word design document for a plugin system in a terminal Tetris game.", max_tokens=3000)
