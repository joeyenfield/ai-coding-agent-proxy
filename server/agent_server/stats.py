"""The server computer's own load, network and disk use, and what the server is doing right now,
for the console's Server stats page (GET /api/server/stats).

Machines report their load over the fleet socket (agent-machine's metrics); this is the same for
the computer agent-server itself runs on, read here with psutil. `live` adds the server's own
counts: requests, connections, sessions, the fleet, the other server processes. Nothing is saved:
the console samples it every few seconds while the page is open, and works out rates itself from
the running totals.
"""

from __future__ import annotations

import os
import platform
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil


# Walking a folder can take seconds, so folder sizes (and the database's) are measured in the
# background and kept this long.
FOLDER_TTL = 300


@dataclass
class Folder:
    name: str
    path: Path


class ServerStats:
    def __init__(self, settings: Any):
        self.folders = [
            Folder("Logs", Path(settings.log_dir)),
            Folder("Builds handed out", Path(settings.dist_dir)),
        ]
        # Sessions, their records and traces, accounts and settings: all in the database.
        self.db = getattr(settings, "db", None)
        self.process = psutil.Process()
        # psutil's CPU figures are since the previous call; the first one only starts the clock.
        psutil.cpu_percent(interval=None)
        self.process.cpu_percent(interval=None)
        self._lock = threading.Lock()
        self._sizes: dict[str, int | None] = {}
        self._measured_at: float | None = None
        self._measuring = False

    def sample(self) -> dict[str, Any]:
        self._measure_if_stale()
        memory = psutil.virtual_memory()
        swap = psutil.swap_memory()
        with self.process.oneshot():
            process = {
                "pid": self.process.pid,
                "rss": self.process.memory_info().rss,
                "cpu_percent": self.process.cpu_percent(interval=None),
                "threads": self.process.num_threads(),
                "started_at": self.process.create_time(),
                "open_files": _open_files(self.process),
                "connections": _connections(self.process),
            }
        try:
            load = list(os.getloadavg())
        except (AttributeError, OSError):  # Windows has none
            load = None
        with self._lock:
            sizes, measured_at, measuring = dict(self._sizes), self._measured_at, self._measuring
        return {
            "timestamp": time.time(),
            "hostname": socket.gethostname(),
            "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
            "boot_time": psutil.boot_time(),
            "cpu": {"percent": psutil.cpu_percent(interval=None), "count": psutil.cpu_count() or 0, "load_average": load},
            "memory": {"total": memory.total, "used": memory.total - memory.available, "available": memory.available, "percent": memory.percent},
            "swap": {"total": swap.total, "used": swap.used, "percent": swap.percent},
            "process": process,
            "network": _network(),
            "disks": self._disks(),
            "folders": [{"name": folder.name, "path": str(folder.path), "bytes": sizes.get(str(folder.path))} for folder in self.folders],
            "database": {
                "kind": ("sqlite" if self.db.sqlite else "postgresql") if self.db else None,
                "bytes": sizes.get("database"),
            },
            "folders_measured_at": measured_at,
            "folders_measuring": measuring,
        }

    def _disks(self) -> list[dict[str, Any]]:
        """Each filesystem holding one of the server's folders, once, with the folders on it."""
        mounts = sorted((part.mountpoint for part in psutil.disk_partitions(all=False)), key=len, reverse=True)
        found: dict[str, dict[str, Any]] = {}
        for folder in self.folders:
            path = _existing(folder.path)
            if path is None:
                continue
            mount = next((point for point in mounts if _within(path, point)), path.anchor or str(path))
            if mount not in found:
                try:
                    usage = psutil.disk_usage(mount)
                except OSError:
                    continue
                found[mount] = {"mountpoint": mount, "total": usage.total, "used": usage.used, "free": usage.free, "percent": usage.percent, "folders": []}
            found[mount]["folders"].append(folder.name)
        return list(found.values())

    def _measure_if_stale(self) -> None:
        with self._lock:
            fresh = self._measured_at is not None and time.time() - self._measured_at < FOLDER_TTL
            if fresh or self._measuring:
                return
            self._measuring = True
        threading.Thread(target=self._measure, name="server-stats-folders", daemon=True).start()

    def _measure(self) -> None:
        sizes = {str(folder.path): _size(folder.path) for folder in self.folders}
        sizes["database"] = self.db.size() if self.db else None
        with self._lock:
            self._sizes, self._measured_at, self._measuring = sizes, time.time(), False


def _open_files(process: psutil.Process) -> int | None:
    """File descriptors (handles on Windows) the server holds: sockets, files and pipes together."""
    try:
        return process.num_handles() if hasattr(process, "num_handles") else process.num_fds()
    except (psutil.Error, OSError, AttributeError):
        return None


def _connections(process: psutil.Process) -> int | None:
    """The server's own sockets, listening ones included. Some systems refuse it without privileges."""
    try:
        return len(process.net_connections(kind="inet"))
    except (psutil.Error, OSError, AttributeError):
        return None


def _network() -> dict[str, int] | None:
    """Bytes and packets through the computer's network interfaces since it started, loopback left out."""
    try:
        counters = psutil.net_io_counters(pernic=True)
    except (psutil.Error, OSError):
        return None
    used = [value for name, value in counters.items() if not name.lower().startswith(("lo", "loopback"))]
    if not used:
        return None
    return {
        "bytes_sent": sum(item.bytes_sent for item in used),
        "bytes_recv": sum(item.bytes_recv for item in used),
        "packets_sent": sum(item.packets_sent for item in used),
        "packets_recv": sum(item.packets_recv for item in used),
        "errors": sum(item.errin + item.errout for item in used),
        "interfaces": len(used),
    }


# What an open WebSocket is, by its path.
SOCKET_KINDS = (
    ("machines", "/api/machines/connect"),
    ("model_tunnels", "/api/machines/tunnel/"),
    ("agent_tunnels", "/api/proxy/tunnel"),
    ("links", "/api/links/"),
)


def _socket_kind(path: str) -> str:
    for kind, prefix in SOCKET_KINDS:
        if path.startswith(prefix):
            return kind
    if path.startswith("/api/terminals/"):
        return "terminal_hosts" if path.endswith("/host") else "terminal_viewers"
    return "other"


def live(runtime: Any) -> dict[str, Any]:
    """What this server process is doing now. Read on the event loop, which owns everything counted here.

    With several server processes these are this process's own; `nodes` lists every process alive."""
    log = runtime.access_log
    now = int(time.time() // 60)
    sockets: dict[str, int] = {}
    for entry in list(log.open.values()):
        kind = _socket_kind(entry.get("path", ""))
        sockets[kind] = sockets.get(kind, 0) + 1
    sessions = runtime.sessions.list()
    machines = list(runtime.fleet.machines.values())
    terminals = [terminal for terminal in runtime.terminals.terminals.values() if terminal.connected]
    links = list(runtime.links.links.values())
    return {
        "started_at": runtime.started_at,
        "requests": {
            "in_flight": len(runtime.active),
            "totals": {kind: log.totals[kind] for kind in ("ok", "failed", "dropped")},
            # The last hour, a minute each, oldest first: everything the server answered, not only model calls.
            "per_minute": [
                {"minute": minute * 60, **{kind: log.minutes.get(minute, {}).get(kind, 0) for kind in ("ok", "failed", "dropped")}}
                for minute in range(now - 59, now + 1)
            ],
        },
        "connections": {
            "websockets": len(log.open),
            "by_kind": sockets,
            "live_streams": len(runtime.live.subscribers),
        },
        "sessions": {
            "open": sum(item.ended_at is None for item in sessions),
            "total": len(sessions),
            "streaming": len({record.get("session_id") for record in runtime.active.values()}),
        },
        "fleet": {
            "machines": len(machines),
            "connected": sum(runtime.fleet.connected(machine.id) for machine in machines),
            "tunnels": len(runtime.tunnels._open),
            "tunnel_listeners": len(runtime.tunnels._listeners),
        },
        "terminals": {"shared": len(terminals), "viewers": sum(len(terminal.viewers) for terminal in terminals)},
        "intercept_tunnels": len(runtime.intercept.tunnels) if runtime.intercept else 0,
        "links": {"total": len(links), "connected": sum(bool(runtime.links.connected(link.id)) for link in links)},
        # Records waiting to be written to the database: it grows when the database can't keep up.
        "write_queue": runtime.db.writer.queue.qsize(),
        "nodes": [
            {"id": pod, "this": pod == runtime.cluster.pod_id, "seen_at": info.get("at"), "live_streams": info.get("live")}
            for pod, info in sorted(runtime.cluster.pods().items())
        ],
    }


# Prometheus's text exposition format.
PROMETHEUS = "text/plain; version=0.0.4; charset=utf-8"

# One metric: its name, type, help text and samples (labels, value). A value of None is left out.
Metric = tuple[str, str, str, list[tuple[dict[str, Any], Any]]]


def _labels(labels: dict[str, Any]) -> str:
    def escape(value: Any) -> str:
        return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    return "{" + ",".join(f'{name}="{escape(value)}"' for name, value in labels.items()) + "}" if labels else ""


def exposition(metrics: list[Metric], common: dict[str, Any] | None = None) -> str:
    """The metrics as Prometheus text, each sample with the common labels first."""
    lines: list[str] = []
    for name, kind, text, samples in metrics:
        found = [(labels, value) for labels, value in samples if value is not None]
        if not found:
            continue
        lines += [f"# HELP {name} {text}", f"# TYPE {name} {kind}"]
        lines += [f"{name}{_labels({**(common or {}), **labels})} {float(value) if isinstance(value, bool) else value}" for labels, value in found]
    return "\n".join(lines) + "\n" if lines else ""


def server_metrics(runtime: Any) -> list[Metric]:
    """The dashboard's numbers for Prometheus: what `live` counts, the computer's network and memory, the process's memory."""
    now = live(runtime)
    network = _network() or {}
    memory = psutil.virtual_memory()
    try:
        rss = psutil.Process().memory_info().rss
    except (psutil.Error, OSError):
        rss = None
    one = lambda value: [({}, value)]  # noqa: E731
    return [
        ("agent_server_requests_total", "counter", "Requests and WebSockets this process answered, by how they ended",
         [({"outcome": outcome}, count) for outcome, count in now["requests"]["totals"].items()]),
        ("agent_server_websockets_open", "gauge", "WebSockets open here, by kind",
         [({"kind": kind}, now["connections"]["by_kind"].get(kind, 0)) for kind in (*(name for name, _ in SOCKET_KINDS), "terminal_hosts", "terminal_viewers", "other")]),
        ("agent_server_sessions_open", "gauge", "Sessions that haven't ended", one(now["sessions"]["open"])),
        ("agent_server_sessions_streaming", "gauge", "Sessions with a request in flight here", one(now["sessions"]["streaming"])),
        ("agent_server_fleet_connected", "gauge", "Machines connected to the server", one(now["fleet"]["connected"])),
        ("agent_server_intercept_tunnels", "gauge", "Account agents' HTTPS tunnels open here", one(now["intercept_tunnels"])),
        ("agent_server_links_connected", "gauge", "Linked servers connected", one(now["links"]["connected"])),
        ("agent_server_process_resident_memory_bytes", "gauge", "The server process's memory", one(rss)),
        ("agent_server_host_memory_used_bytes", "gauge", "Memory in use on the server's computer", one(memory.total - memory.available)),
        ("agent_server_host_memory_total_bytes", "gauge", "Memory of the server's computer", one(memory.total)),
        ("agent_server_host_network_receive_bytes_total", "counter", "Bytes the server's computer received, loopback left out", one(network.get("bytes_recv"))),
        ("agent_server_host_network_transmit_bytes_total", "counter", "Bytes the server's computer sent, loopback left out", one(network.get("bytes_sent"))),
    ]


def machine_metrics(sample: dict[str, Any]) -> list[Metric]:
    """A machine's load sample (agent-machine's metrics) for Prometheus. Parts an older agent-machine
    doesn't send, or that its computer can't report, are left out."""
    def part(name: str) -> dict[str, Any]:
        value = sample.get(name)
        return value if isinstance(value, dict) else {}

    cpu, memory, swap, network, process, agent = (part(name) for name in ("cpu", "memory", "swap", "network", "process", "agent"))
    gpus = [gpu for gpu in sample.get("gpus") or [] if isinstance(gpu, dict)]
    disks = [disk for disk in sample.get("disks") or [] if isinstance(disk, dict)]
    load = sample.get("load_average") if isinstance(sample.get("load_average"), list) else []
    one = lambda value: [({}, value)]  # noqa: E731
    gpu = lambda field: [({"gpu": item.get("index"), "name": item.get("name")}, item.get(field)) for item in gpus]  # noqa: E731
    disk = lambda field: [({"mountpoint": item.get("mountpoint")}, item.get(field)) for item in disks]  # noqa: E731
    since = agent.get("connected_since")
    return [
        ("agent_machine_cpu_percent", "gauge", "CPU in use on the machine", one(cpu.get("percent"))),
        ("agent_machine_cpu_count", "gauge", "The machine's CPU cores", one(cpu.get("count"))),
        ("agent_machine_load_average", "gauge", "Load average", [({"minutes": minutes}, value) for minutes, value in zip((1, 5, 15), load)]),
        ("agent_machine_memory_used_bytes", "gauge", "Memory in use", one(memory.get("used"))),
        ("agent_machine_memory_total_bytes", "gauge", "Memory", one(memory.get("total"))),
        ("agent_machine_swap_used_bytes", "gauge", "Swap in use", one(swap.get("used"))),
        ("agent_machine_swap_total_bytes", "gauge", "Swap", one(swap.get("total"))),
        ("agent_machine_uptime_seconds", "gauge", "Seconds since the computer started", one(sample.get("uptime_seconds"))),
        ("agent_machine_gpu_utilization_percent", "gauge", "GPU load", gpu("utilization")),
        ("agent_machine_gpu_memory_used_bytes", "gauge", "GPU memory in use", gpu("memory_used")),
        ("agent_machine_gpu_memory_total_bytes", "gauge", "GPU memory", gpu("memory_total")),
        ("agent_machine_gpu_temperature_celsius", "gauge", "GPU temperature", gpu("temperature")),
        ("agent_machine_gpu_power_watts", "gauge", "GPU power draw", gpu("power_watts")),
        ("agent_machine_network_receive_bytes_total", "counter", "Bytes received, loopback left out", one(network.get("bytes_recv"))),
        ("agent_machine_network_transmit_bytes_total", "counter", "Bytes sent, loopback left out", one(network.get("bytes_sent"))),
        ("agent_machine_network_receive_packets_total", "counter", "Packets received", one(network.get("packets_recv"))),
        ("agent_machine_network_transmit_packets_total", "counter", "Packets sent", one(network.get("packets_sent"))),
        ("agent_machine_network_errors_total", "counter", "Network errors, in and out", one(network.get("errors"))),
        ("agent_machine_disk_used_bytes", "gauge", "Disk space in use", disk("used")),
        ("agent_machine_disk_total_bytes", "gauge", "Disk space", disk("total")),
        ("agent_machine_process_resident_memory_bytes", "gauge", "agent-machine's own memory", one(process.get("rss"))),
        ("agent_machine_process_start_time_seconds", "gauge", "When agent-machine started", one(process.get("started_at"))),
        ("agent_machine_connected", "gauge", "1 while agent-machine holds its connection to the server", one(None if not agent else int(since is not None))),
        ("agent_machine_connects_total", "counter", "Connections to the server since agent-machine started", one(agent.get("connects"))),
        ("agent_machine_requests_total", "counter", "Requests from the server it handled", one(agent.get("requests"))),
        ("agent_machine_tunnels_open", "gauge", "Tunnels to its model servers open", one(agent.get("tunnels_open"))),
        ("agent_machine_tunnels_total", "counter", "Tunnels opened", one(agent.get("tunnels_total"))),
        ("agent_machine_tunnel_receive_bytes_total", "counter", "Bytes from the server into its model servers", one(agent.get("tunnel_bytes_in"))),
        ("agent_machine_tunnel_transmit_bytes_total", "counter", "Bytes from its model servers back to the server", one(agent.get("tunnel_bytes_out"))),
    ]


def _existing(path: Path) -> Path | None:
    """The folder, or the nearest parent that exists (a fresh install has no dist/ yet)."""
    path = path.resolve()
    for candidate in (path, *path.parents):
        if candidate.exists():
            return candidate
    return None


def _within(path: Path, mount: str) -> bool:
    try:
        return path.is_relative_to(Path(mount))
    except ValueError:
        return False


def _size(path: Path) -> int | None:
    """Bytes in a folder tree, not following links; None when it doesn't exist."""
    if not path.is_dir():
        return None
    total, pending = 0, [path]
    while pending:
        try:
            entries = os.scandir(pending.pop())
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        pending.append(Path(entry.path))
                    elif entry.is_file(follow_symlinks=False):
                        total += entry.stat(follow_symlinks=False).st_size
                except OSError:
                    continue
    return total
