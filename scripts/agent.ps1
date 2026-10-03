# Launch a coding agent through the proxy: .\scripts\agent.ps1 qwen . --model qwen3.6:35b
$ErrorActionPreference = "Stop"
python -m agent_proxy.launcher @args
exit $LASTEXITCODE
