#!/usr/bin/env sh
# Restart Ollama with a server-wide default context. The proxy also sets num_ctx
# per request (config/models.yaml); this covers clients that bypass it, such as
# the Responses API used by Codex.
set -eu
sudo systemctl stop ollama
OLLAMA_CONTEXT_LENGTH=65536 OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 ollama serve
