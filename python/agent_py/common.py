"""What the Python agent and agent-machine share: the protocol they speak, the headers that
say so, sign-in files, and HTTP clients that never take a proxy from the environment.

The Go programs keep these apart (client/ and machine/ share no code). Here the two live in
one package, so the small part that's the same in both is written once; each still keeps
its own credentials file in its own folder, in the same format as the Go programs, so a
sign-in made by either works with the other.
"""

from __future__ import annotations

import json
import os
import platform as _platform
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import httpx

# The server API level these programs speak. The server accepts a range and refuses others
# with 426 (WebSockets: close 4426); keep it equal to settings.Protocol in client/ and machine/.
PROTOCOL = 1

DEFAULT_SERVER = "http://127.0.0.1:9999"
TOKEN_HEADER = "X-AI-Proxy-Token"
VERSION_HEADER = "X-AI-Proxy-Version"
PROTOCOL_HEADER = "X-AI-Proxy-Protocol"

LOOPBACK = {"localhost", "127.0.0.1", "::1"}


def _version() -> str:
    if os.environ.get("AI_PROXY_VERSION"):
        return os.environ["AI_PROXY_VERSION"]
    # The repository's VERSION, when run from a checkout; "dev" otherwise, as an unstamped Go build says.
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "VERSION"
        if candidate.is_file() and (parent / "server").is_dir():
            text = candidate.read_text(encoding="utf-8").strip()
            return f"{text}+python" if text else "dev"
    try:
        from importlib.metadata import version

        return version("ai-proxy-agent-py")
    except Exception:
        return "dev"


VERSION = _version()


def identify(headers: Dict[str, str], program: Optional[str] = None) -> Dict[str, str]:
    """Adds the version and protocol to headers bound for the server, and a User-Agent when program is named."""
    headers[VERSION_HEADER] = VERSION
    headers[PROTOCOL_HEADER] = str(PROTOCOL)
    if program:
        headers["User-Agent"] = f"{program}/{VERSION}"
    return headers


def client(timeout: Optional[float] = 30.0, program: Optional[str] = None) -> httpx.Client:
    """An HTTP client that ignores HTTPS_PROXY: started from inside an account agent, it would loop back to itself."""
    headers = identify({}, program) if program else {}
    return httpx.Client(trust_env=False, timeout=timeout, headers=headers)


def async_client(timeout: Optional[float] = 30.0) -> httpx.AsyncClient:
    return httpx.AsyncClient(trust_env=False, timeout=timeout)


def health(base: str, timeout: float = 0.75) -> Optional[Dict[str, Any]]:
    """GET /health of any of the programs, or None when it doesn't answer."""
    try:
        with client(timeout) as http:
            response = http.get(base.rstrip("/") + "/health")
        if response.status_code != 200:
            return None
        body = response.json()
        return body if isinstance(body, dict) else None
    except (httpx.HTTPError, ValueError):
        return None


def is_loopback_url(url: str) -> bool:
    try:
        return (urlparse(url).hostname or "").lower() in LOOPBACK
    except ValueError:
        return False


def loopback_host(host: str) -> bool:
    """Whether a Host header names this computer, with or without a port."""
    host = (host or "").strip().lower()
    if host.startswith("["):
        host = host[1:].split("]", 1)[0]
    elif host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host in LOOPBACK


def websocket_url(server: str, path: str) -> str:
    parsed = urlparse(server)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return f"{scheme}://{parsed.netloc}{parsed.path.rstrip('/')}{path}"


def detail(body: bytes | str) -> str:
    """The server's error message from a JSON body ({"detail": ...}), or the body itself."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and parsed.get("detail") is not None:
            value = parsed["detail"]
            return value if isinstance(value, str) else json.dumps(value)
    except ValueError:
        pass
    return text.strip() or "no details"


def normalize(server: str) -> str:
    """The key a server is stored under."""
    return server.strip().rstrip("/").lower()


def config_dir() -> Path:
    """The user's config folder: %APPDATA% on Windows, XDG_CONFIG_HOME or ~/.config elsewhere."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        return Path(base) if base else Path.home() / "AppData" / "Roaming"
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) if base else Path.home() / ".config"


def expand_home(path: str) -> Path:
    return Path(os.path.expanduser(path))


def write_json(path: Path, value: Any) -> None:
    """Replaces path in one step, readable by this user only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


@dataclass
class ServerEntry:
    url: str
    user: str = ""
    token: str = ""
    token_id: str = ""

    def to_json(self) -> Dict[str, str]:
        value = {"url": self.url}
        for key in ("user", "token", "token_id"):
            if getattr(self, key):
                value[key] = getattr(self, key)
        return value


@dataclass
class Credentials:
    """A login's file: each server's URL, user and token, and the default server. Passwords are never stored."""

    path: Path
    servers: Dict[str, ServerEntry] = field(default_factory=dict)
    default: str = ""

    @classmethod
    def load(cls, path: Path) -> "Credentials":
        credentials = cls(path)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return credentials
        for key, entry in (data.get("servers") or {}).items():
            if isinstance(entry, dict):
                credentials.servers[key] = ServerEntry(str(entry.get("url") or ""), str(entry.get("user") or ""), str(entry.get("token") or ""), str(entry.get("token_id") or ""))
        credentials.default = str(data.get("default") or "")
        return credentials

    def save(self) -> None:
        value: Dict[str, Any] = {"servers": {key: entry.to_json() for key, entry in self.servers.items()}}
        if self.default:
            value["default"] = self.default
        write_json(self.path, value)

    def default_entry(self) -> Optional[ServerEntry]:
        return self.servers.get(self.default) if self.default else None


@dataclass
class Upstream:
    """The agent-server in use and its sign-in, read from the credentials file on every call,
    so login and logout apply at once."""

    credentials_path: Callable[[], Path]
    fixed: str = ""
    # The client honours AI_PROXY_URL and AI_PROXY_TOKEN; agent-machine reads only its own file.
    use_env: bool = True

    def credentials(self) -> Credentials:
        return Credentials.load(self.credentials_path())

    def server(self) -> str:
        if self.fixed:
            return self.fixed.rstrip("/")
        if self.use_env and os.environ.get("AI_PROXY_URL"):
            return os.environ["AI_PROXY_URL"].rstrip("/")
        entry = self.credentials().default_entry()
        if entry and entry.url:
            return entry.url.rstrip("/")
        return DEFAULT_SERVER

    def token(self, server: str) -> str:
        if self.use_env and os.environ.get("AI_PROXY_TOKEN"):
            return os.environ["AI_PROXY_TOKEN"]
        entry = self.credentials().servers.get(normalize(server))
        return entry.token if entry else ""

    def user(self, server: str) -> str:
        entry = self.credentials().servers.get(normalize(server))
        return entry.user if entry else ""


def goos() -> str:
    """This computer's OS as Go names it, which is what the server expects."""
    return {"win32": "windows", "darwin": "darwin", "cygwin": "windows"}.get(sys.platform, "freebsd" if sys.platform.startswith("freebsd") else "linux")


def goarch() -> str:
    machine = _platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64", "i386": "386", "i686": "386", "x86": "386"}.get(machine, "armv7" if machine.startswith("arm") else machine)


WSL_DRIVES = ("/mnt/", "/c/", "/d/")


def agent_dirs() -> List[Path]:
    """The folders agents' installers use besides PATH: OpenCode's, Bun's, Cargo's, npm with a user prefix, ~/.local/bin and ~/bin."""
    home = Path.home()
    return [home / part for part in (".opencode/bin", ".local/bin", "bin", ".bun/bin", ".cargo/bin", ".npm-global/bin")]


def look_path(name: str) -> Optional[str]:
    """shutil.which, except that in WSL a program on the Windows drives doesn't count: run through
    interop, it can't use Linux paths and its terminal shows nothing."""
    if not name:
        return None
    in_wsl = sys.platform == "linux" and "microsoft" in os.uname().release.lower()
    if in_wsl and "/" not in name:
        dirs = [entry for entry in os.environ.get("PATH", "").split(os.pathsep) if not entry.startswith(WSL_DRIVES)]
        found = shutil.which(name, path=os.pathsep.join(dirs))
    else:
        found = shutil.which(name)
    if found or "/" in name or "\\" in name:
        return found
    for folder in agent_dirs():
        candidate = shutil.which(name, path=str(folder))
        if candidate:
            return candidate
    return None
