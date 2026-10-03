"""CPU, memory and GPU usage of the machine this process runs on.

Ollama's API reports loaded models but not machine load, so the proxy samples
its own host directly. For a remote Ollama machine, run ``agent-metrics`` there
and set ``metrics_url`` on that backend. Samples are taken on request and are
never stored.
"""

from __future__ import annotations

import argparse
import asyncio
import platform
import shutil
import socket
import subprocess
import time
from typing import Any

import psutil


CACHE_SECONDS = 1.0
NVIDIA_QUERY = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw"

_cache: tuple[float, dict[str, Any]] | None = None
_lock = asyncio.Lock()
_cpu_primed = False
_cpu_last: tuple[float, float] = (0.0, 0.0)
MIN_CPU_INTERVAL = 0.5


async def sample() -> dict[str, Any]:
    """Return a fresh sample, shared between callers within CACHE_SECONDS."""
    global _cache
    async with _lock:
        if _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        value = await asyncio.to_thread(collect)
        _cache = (time.monotonic(), value)
        return value


def collect() -> dict[str, Any]:
    memory = psutil.virtual_memory()
    gpus, gpu_error = _nvidia_gpus()
    return {
        "hostname": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
        "timestamp": time.time(),
        "cpu": {"percent": _cpu_percent(), "count": psutil.cpu_count()},
        "memory": {"used": memory.total - memory.available, "total": memory.total, "percent": memory.percent},
        "gpus": gpus,
        "gpu_source": "nvidia-smi" if gpus else None,
        "gpu_error": gpu_error,
    }


def _cpu_percent() -> float:
    # psutil measures since the previous call: the first call needs a short blocking
    # interval, and calls very close together read near zero, so reuse the last value.
    global _cpu_primed, _cpu_last
    now = time.monotonic()
    if not _cpu_primed:
        _cpu_primed = True
        _cpu_last = (now, psutil.cpu_percent(interval=0.3))
    elif now - _cpu_last[0] >= MIN_CPU_INTERVAL:
        _cpu_last = (now, psutil.cpu_percent(interval=None))
    return _cpu_last[1]


def _nvidia_gpus() -> tuple[list[dict[str, Any]], str | None]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return [], "nvidia-smi not found, so GPU usage is unavailable on this host"
    try:
        output = subprocess.run(
            [executable, f"--query-gpu={NVIDIA_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        return [], f"nvidia-smi failed: {exc}"
    gpus = []
    for line in output.strip().splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        gpus.append({
            "index": int(parts[0]),
            "name": parts[1],
            "utilization": _number(parts[2]),
            # nvidia-smi reports MiB.
            "memory_used": _mib(parts[3]),
            "memory_total": _mib(parts[4]),
            "temperature": _number(parts[5]),
            "power_watts": _number(parts[6]) if len(parts) > 6 else None,
        })
    return gpus, None


def _number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None  # "[N/A]" on GPUs that don't report a field.


def _mib(value: str) -> int | None:
    number = _number(value)
    return int(number * 1024 * 1024) if number is not None else None


def main() -> None:
    """Serve this machine's metrics so a proxy elsewhere can chart them."""
    import uvicorn
    from fastapi import FastAPI

    parser = argparse.ArgumentParser(description="Expose CPU, memory and GPU usage for the AI proxy")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8182)
    args = parser.parse_args()
    app = FastAPI(title="AI proxy host metrics")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/host/metrics")
    async def metrics() -> dict[str, Any]:
        return await sample()

    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
