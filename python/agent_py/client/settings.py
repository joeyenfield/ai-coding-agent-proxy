"""The agent machine's ports and `agent login`'s credentials file, the same as the Go client's."""

from __future__ import annotations

import os
from pathlib import Path

from .. import common

PROGRAM = "agent"
DEFAULT_PROXY_PORT = 8184
DEFAULT_INTERCEPT_PORT = 8183


def _port(name: str, fallback: int) -> int:
    try:
        value = int(os.environ.get(name, ""))
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def proxy_port() -> int:
    return _port("AI_PROXY_LOCAL_PORT", DEFAULT_PROXY_PORT)


def intercept_port() -> int:
    return _port("AI_PROXY_LOCAL_INTERCEPT_PORT", DEFAULT_INTERCEPT_PORT)


def proxy_url() -> str:
    return f"http://127.0.0.1:{proxy_port()}"


def credentials_path() -> Path:
    """credentials.json in the user's config folder, or AI_PROXY_CREDENTIALS: shared with the Go client."""
    if os.environ.get("AI_PROXY_CREDENTIALS"):
        return common.expand_home(os.environ["AI_PROXY_CREDENTIALS"])
    return common.config_dir() / "agent-proxy" / "credentials.json"


def upstream(fixed: str = "") -> common.Upstream:
    return common.Upstream(credentials_path, fixed=fixed, use_env=True)


def log_path() -> Path:
    return credentials_path().parent / "agent-proxy.log"
