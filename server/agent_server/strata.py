"""The API key the server sends with agents' traffic to a Strata backend.

Strata serves one local model over the OpenAI Chat Completions, Anthropic
Messages and OpenAI Responses APIs, and asks for an API key on /v1. The server
forwards agent traffic to it unchanged apart from that key, so no secret is
copied into config/:

  - a Strata container the server had a machine run has the key the server chose
    for it (containers.KeyStore fills managed_keys);
  - STRATA_API_KEY on the server wins otherwise, as it does for Strata's own CLI;
  - else the key the machine reported for the Strata it runs on that port, read
    from Strata's files there (the fleet keeps it in memory only).

Everything else about Strata (its status, loading and unloading, its logs, its
profiles) is agent-machine's, on the machine that runs it (machine/internal/providers).
"""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

from .config import Backend


CREDENTIAL_HEADERS = {"authorization", "x-api-key"}

# Keys of the Strata containers the server runs, by backend name; filled in by containers.KeyStore.
managed_keys: dict[str, str] = {}
# The fleet, set by the runtime, for the keys machines report.
fleet: Any = None


def first_key(value: Any) -> str | None:
    """The first key a Strata server accepts, in its forms: "k1,k2", a list, or "none" for no key."""
    if isinstance(value, (list, tuple)):
        keys = [str(item).strip() for item in value]
    elif isinstance(value, str) and value.strip().lower() != "none":
        keys = [item.strip() for item in value.split(",")]
    else:
        return None
    return next((key for key in keys if key), None)


def key_for(backend: Backend) -> tuple[str | None, str | None]:
    """The API key for a Strata backend and where it came from: "container", "environment" or "machine"."""
    if backend.container and backend.name in managed_keys:
        # The server chose this key when it created the container.
        return managed_keys[backend.name], "container"
    environment = first_key(os.environ.get("STRATA_API_KEY", ""))
    if environment:
        return environment, "environment"
    if fleet is not None and backend.machine:
        try:
            port = urlsplit(backend.url).port
        except ValueError:
            port = None
        key = fleet.key(backend.machine, port)
        if key:
            return key, "machine"
    return None, None


def with_key(backend: Backend, headers: dict[str, str]) -> dict[str, str]:
    """Headers for a request to Strata: the agent's placeholder credential swapped for Strata's key.

    Without a known key the agent's own headers go through, so a key set in the
    agent still works.
    """
    key, _ = key_for(backend)
    if not key:
        return headers
    kept = {name: value for name, value in headers.items() if name.lower() not in CREDENTIAL_HEADERS}
    return {**kept, "authorization": f"Bearer {key}"}
