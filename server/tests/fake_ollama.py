"""A stand-in for `ollama serve` in tests: answers /api/version where OLLAMA_HOST says, until it's stopped.

Run as `python fake_ollama.py serve`. FAKE_OLLAMA_FAIL=1 makes it exit at once, like an
Ollama that can't start.
"""

from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        body = json.dumps({"version": "0.0.0-fake"} if self.path == "/api/version" else {"models": []}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        pass


def main() -> int:
    if sys.argv[1:] != ["serve"]:
        print(f"fake ollama: unsupported {sys.argv[1:]}", file=sys.stderr)
        return 2
    if os.environ.get("FAKE_OLLAMA_FAIL"):
        print("Error: listen tcp: bind: address already in use", file=sys.stderr)
        return 1
    host, _, port = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").rpartition(":")
    print(f"fake ollama listening on {host}:{port}", flush=True)
    ThreadingHTTPServer((host, int(port)), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
