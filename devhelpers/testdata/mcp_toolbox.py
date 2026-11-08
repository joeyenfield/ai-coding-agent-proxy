"""A small stdio MCP server for the test-data stack: a few harmless tools to try shared MCP with.

agent-machine runs it (mcp.json, from QUICKSTART_MCP) and bridges it to agents as Streamable HTTP.
One JSON-RPC message per line, standard library only, so it runs in the machine image as it is.
Its tools: the time, an echo, dice, word counts and a note pad kept in memory for this process.
"""
from __future__ import annotations

import json
import random
import socket
import sys
from datetime import datetime, timezone

NOTES: list[str] = []

TOOLS = [
    {"name": "now", "description": "The current date and time, in UTC and on this machine.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "echo", "description": "Answers with the text it was given.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "roll_dice", "description": "Rolls dice, such as 2 dice of 6 sides.",
     "inputSchema": {"type": "object", "properties": {"count": {"type": "integer", "minimum": 1, "maximum": 20},
                                                      "sides": {"type": "integer", "minimum": 2, "maximum": 1000}}}},
    {"name": "word_count", "description": "Counts the lines, words and characters of a text.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add_note", "description": "Keeps a note until the server restarts.",
     "inputSchema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
    {"name": "list_notes", "description": "The notes kept so far, oldest first.",
     "inputSchema": {"type": "object", "properties": {}}},
]


def call(name: str, arguments: dict) -> str:
    if name == "now":
        return f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} (UTC) on {socket.gethostname()}"
    if name == "echo":
        return str(arguments.get("text", ""))
    if name == "roll_dice":
        count = max(1, min(20, int(arguments.get("count") or 1)))
        sides = max(2, min(1000, int(arguments.get("sides") or 6)))
        rolls = [random.randint(1, sides) for _ in range(count)]
        return f"{count}d{sides}: {', '.join(map(str, rolls))} (total {sum(rolls)})"
    if name == "word_count":
        text = str(arguments.get("text", ""))
        return f"{len(text.splitlines())} lines, {len(text.split())} words, {len(text)} characters"
    if name == "add_note":
        NOTES.append(str(arguments.get("note", ""))[:2000])
        return f"Kept. {len(NOTES)} notes so far."
    if name == "list_notes":
        return "\n".join(f"{index}. {note}" for index, note in enumerate(NOTES, 1)) or "No notes yet."
    raise KeyError(name)


def answer(message: dict) -> dict | None:
    if "id" not in message:
        return None  # A notification, such as notifications/initialized: nothing to say.
    reply: dict = {"jsonrpc": "2.0", "id": message["id"]}
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        reply["result"] = {"protocolVersion": params.get("protocolVersion") or "2025-06-18",
                           "capabilities": {"tools": {}}, "serverInfo": {"name": "toolbox", "version": "1"}}
    elif method == "ping":
        reply["result"] = {}
    elif method == "tools/list":
        reply["result"] = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            text = call(params.get("name", ""), params.get("arguments") or {})
            reply["result"] = {"content": [{"type": "text", "text": text}]}
        except KeyError:
            reply["error"] = {"code": -32602, "message": f"no tool {params.get('name')}"}
        except (TypeError, ValueError) as error:
            reply["result"] = {"content": [{"type": "text", "text": f"Bad arguments: {error}"}], "isError": True}
    else:
        reply["error"] = {"code": -32601, "message": f"no method {method}"}
    return reply


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        reply = answer(message) if isinstance(message, dict) else None
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
