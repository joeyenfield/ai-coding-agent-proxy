from __future__ import annotations

from typing import Any

from ..config import ModelProfile


def apply_profile(payload: dict[str, Any], profile: ModelProfile) -> dict[str, Any]:
    """Apply a model profile to a native Ollama /api/chat or /api/generate payload.

    Values the client sent explicitly always win over profile defaults.
    """
    payload["model"] = profile.ollama_model
    if profile.options:
        payload["options"] = {**profile.options, **(payload.get("options") or {})}
    if profile.keep_alive is not None and "keep_alive" not in payload:
        payload["keep_alive"] = profile.keep_alive
    if profile.think is not None and "think" not in payload:
        payload["think"] = profile.think
    return payload
