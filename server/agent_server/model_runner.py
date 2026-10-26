"""Where agents' traffic goes on Docker Model Runner, Docker's own model server.

Model Runner serves its models over the OpenAI (``/engines/v1``), Anthropic
(``/anthropic``) and Ollama (``/api``) APIs and needs no API key. The server
passes agent traffic through unchanged, apart from the path prefix.

Everything else about Model Runner (its models, pulling, loading, Docker Hub) is
agent-machine's, on the machine that runs it (machine/internal/providers).
"""

from __future__ import annotations


def upstream_path(path: str) -> str:
    """Model Runner's path for an agent-facing one: Anthropic under /anthropic, OpenAI under /engines, Ollama's /api as is."""
    if path.startswith("/v1/messages"):
        return "/anthropic" + path
    if path.startswith("/v1/"):
        return "/engines" + path
    return path
