# agent-proxy (Python service)

FastAPI service that sits between coding agents and Ollama. It exposes the
OpenAI, Anthropic and native Ollama APIs to agents, a management API for the
web UI, and the `agent` launcher and `agent-benchmark` commands.

See the [repository README](../README.md) for setup and usage.

```bash
pip install -e ".[dev]"
agent-proxy --reload   # development server on the configured port
pytest
```
