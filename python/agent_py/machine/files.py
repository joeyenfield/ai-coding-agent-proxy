"""The machine's file viewer: the server's {"type": "files"} requests, op "list" (the folders and
files inside one folder) and op "read" (text or an image), as the Go agent-machine's presence/files.go.

Only while files are on (`agent-machine files on`) and only inside the roots (checked here after
resolving links, whatever the server allowed). Hidden names (.env, .git) are shown like any other:
who may browse a machine's files is already limited to its owner and the teams it grants files to.
Supported raster images are sent as base64; other binary files are reported, never sent.
"""

from __future__ import annotations

import base64
import os
from typing import Any, Dict, List, Optional

from . import settings

# The most entries one listing returns; a folder with more says it's truncated.
FILES_LIMIT = 1000
# The most of a file one read sends; a longer file says it's truncated.
FILE_READ_LIMIT = 1 << 20
# Leaves room for base64 and JSON inside the fleet's 16 MiB message limit.
IMAGE_READ_LIMIT = 10 << 20


class Refused(Exception):
    pass


def answer(request: Dict[str, Any], machine: settings.Machine) -> Dict[str, Any]:
    """A files request answered, as the files_result to send back."""
    result: Dict[str, Any] = {"type": "files_result", "request": request.get("request")}
    op, path = request.get("op"), str(request.get("path") or "")
    try:
        if op == "list":
            result.update(list_files(path, machine))
        elif op == "read":
            result.update(read_file(path, machine))
        else:
            raise Refused(f'unknown files op "{op}"')
    except Refused as error:
        return {"type": "files_result", "request": request.get("request"), "ok": False, "error": str(error)}
    result["ok"] = True
    return result


def target(path: str, machine: settings.Machine) -> str:
    """Checks that path may be shown: files on, absolute, and inside a root once links are resolved.
    Returns the resolved path."""
    if not machine.files:
        raise Refused("this machine doesn't share its files (agent-machine files on)")
    if not path or not os.path.isabs(path):
        raise Refused("name a path inside one of this machine's launch folders")
    resolved, ok = machine.within(path)
    if not ok:
        raise Refused(f"{path} isn't inside one of this machine's launch folders")
    return resolved


def is_root(folder: str, machine: settings.Machine) -> bool:
    return any(settings.fold(os.path.normpath(root)) == settings.fold(os.path.normpath(folder)) for root in machine.roots)


def list_files(folder: str, machine: settings.Machine) -> Dict[str, Any]:
    target(folder, machine)
    try:
        # In name order, as Go's ReadDir gives them, so the same entries are kept when truncated.
        entries = sorted(os.scandir(folder), key=lambda entry: entry.name)
    except OSError as error:
        raise Refused(f"couldn't read {folder}: {error}")
    listed: List[Dict[str, Any]] = []
    truncated = False
    for entry in entries:
        path = os.path.join(folder, entry.name)
        try:
            info = os.stat(path)
            # A link is shown only when it leads somewhere inside the roots.
            if entry.is_symlink() and not machine.within(path)[1]:
                continue
        except OSError:
            continue
        if os.path.isdir(path):
            kind = "folder"
        elif os.path.isfile(path):
            kind = "file"
        else:
            continue
        if len(listed) == FILES_LIMIT:
            truncated = True
            break
        item: Dict[str, Any] = {"name": entry.name, "path": path, "kind": kind, "modified": int(info.st_mtime)}
        if kind == "file":
            item["size"] = info.st_size
        listed.append(item)
    # Folders first, then files, each by name.
    listed.sort(key=lambda item: (item["kind"] != "folder", item["name"].lower()))
    # Up goes no further than a root.
    parent: Optional[str] = None
    up = os.path.dirname(folder)
    if not is_root(folder, machine) and up != folder and machine.within(up)[1]:
        parent = up
    return {"path": folder, "entries": listed, "parent": parent, "truncated": truncated}


def read_file(path: str, machine: settings.Machine) -> Dict[str, Any]:
    resolved = target(path, machine)
    if not os.path.isfile(resolved):
        raise Refused(f"{path} isn't a file")
    try:
        info = os.stat(resolved)
        with open(resolved, "rb") as file:
            media_type = image_media_type(file.read(512))
            file.seek(0)
            limit = IMAGE_READ_LIMIT if media_type else FILE_READ_LIMIT
            data = file.read(limit + 1)
    except OSError as error:
        raise Refused(f"couldn't read {path}: {error}")
    truncated = len(data) > limit
    data = data[:limit]
    result: Dict[str, Any] = {
        "path": path, "name": os.path.basename(path), "size": info.st_size, "modified": int(info.st_mtime), "truncated": truncated,
    }
    if media_type:
        result["binary"] = True
        # An incomplete image cannot be previewed; don't send a large unusable prefix.
        if not truncated:
            result["image"] = {"media_type": media_type, "data": base64.b64encode(data).decode("ascii")}
        return result
    text = text_of(data, truncated)
    if text is None:
        # Other binary formats stay on the machine.
        result["binary"] = True
    else:
        result.update(binary=False, content=text)
    return result


def image_media_type(data: bytes) -> str:
    """Inspect bytes rather than extensions. SVG stays text; only these raster formats are sent."""
    for signature, media_type in (
        (b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"), (b"GIF89a", "image/gif"),
        (b"BM", "image/bmp"), (b"\x00\x00\x01\x00", "image/x-icon"),
    ):
        if data.startswith(signature):
            return media_type
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return ""


def text_of(data: bytes, truncated: bool) -> Optional[str]:
    """data as text, or None when it isn't: a NUL byte, or bytes that aren't UTF-8. A character cut
    off at the end of a truncated read is dropped rather than counted against it."""
    if b"\0" in data:
        return None
    for cut in range(4 if truncated else 1):
        try:
            return data[: len(data) - cut].decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None
