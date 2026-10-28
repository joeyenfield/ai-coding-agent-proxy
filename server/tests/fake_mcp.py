"""A stand-in stdio MCP server, which the real agent-machine runs in test_client.py: one JSON-RPC message per line.

It answers initialize, tools/list and tools/call (echoing its arguments and FAKE_MCP_GREETING from
its environment, which only agent-machine's mcp.json gives it).
"""

from __future__ import annotations

import json
import os
import sys


def main() -> None:
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if "id" not in message or "method" not in message:
            continue
        reply: dict = {"jsonrpc": "2.0", "id": message["id"]}
        method = message["method"]
        if method == "initialize":
            reply["result"] = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake-mcp"}}
        elif method == "tools/list":
            reply["result"] = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            arguments = (message.get("params") or {}).get("arguments") or {}
            text = f"{os.environ.get('FAKE_MCP_GREETING', '')} {json.dumps(arguments)}"
            reply["result"] = {"content": [{"type": "text", "text": text}]}
        else:
            reply["error"] = {"code": -32601, "message": f"no method {method}"}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
