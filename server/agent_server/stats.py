"""The server computer's own load and disk use, for the console's Server stats page (GET /api/server/stats).

Machines report their load over the fleet socket (agent-machine's metrics); this is the same for
the computer agent-server itself runs on, read here with psutil. Nothing is saved: the console
samples it every few seconds while the page is open.
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


# Walking logs/ can take seconds once sessions pile up, so folder sizes are measured in the
# background and kept this long.
FOLDER_TTL = 300


@dataclass
class Folder:
    name: str
    path: Path


class ServerStats:
    def __init__(self, settings: Any):
        self.folders = [
            Folder("Recorded traffic and logs", Path(settings.log_dir)),
            Folder("Builds handed out", Path(settings.dist_dir)),
        ]
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
            "disks": self._disks(),
            "folders": [{"name": folder.name, "path": str(folder.path), "bytes": sizes.get(str(folder.path))} for folder in self.folders],
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
        with self._lock:
            self._sizes, self._measured_at, self._measuring = sizes, time.time(), False


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
