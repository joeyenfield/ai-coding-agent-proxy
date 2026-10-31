# agent-server (the server, Python)

The central part: the API that every coding agent's traffic goes through, and the one
the web console (`../ui`) calls. It holds the accounts and sharing, records every
request, routes to the LLM providers (Ollama, Strata, Docker Model Runner, Anthropic),
decrypts account agents' HTTPS, relays shared terminals and runs managed containers.
It serves the console's built files too, but the console's code lives in `../ui`.

Agent machines don't run this: they run the Go client (`../client`), whose `agent-proxy`
carries their traffic here. The client only knows the server's URL; it gets the agent
definitions, backends and launch environments from this API.

See the [repository README](../README.md) for setup and usage.

```bash
pip install -e ".[dev]"     # agent-server, agent-benchmark
agent-server --reload       # development server on the configured port
python -m pytest -q         # tests; test_client.py also builds and runs ../client when Go is installed
```

In Docker, from the repository root: `docker compose up -d`, which runs the published image
`ghcr.io/joeyenfield/agent-server` (built by `.github/workflows/images.yml`). To build it
yourself: `docker build -f server/Dockerfile -t agent-server .`. See `Dockerfile` and `docker/`.
