"""Model servers in containers on this machine, for the server (machine/internal/containers in Go).

The server asks over the machine's fleet connection (start, stop, restart, remove, status, logs);
the machine runs the `docker` CLI (or `podman`) itself. `docker run` is root on this machine, so
containers are off until `agent-machine containers on`, and every request is checked here whatever
the server allowed: only the engines below, each engine's own settings, plain image names, GPU
lists and a volume name or folder. The engine table lives here, and the machine tells the server
about it in its hello.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import settings

# Names every container the machine runs for the server.
PREFIX = "ai-proxy-"
LABEL = "ai-proxy.backend"
# A JSON list of arguments run as the CLI instead of docker or podman (tests use a fake one).
COMMAND_ENV = "AI_PROXY_CONTAINER_COMMAND"

JOB_LINES = 200
SHOWN_LINES = 30
# How long a detection stands: requests come every few seconds while the console's Containers
# page is open, and each probe runs the CLI.
DETECT_FOR = 30.0


@dataclass
class Option:
    """One setting an engine's image reads from the environment."""

    env: str
    label: str
    choices: List[str]
    hint: str

    def public(self) -> Dict[str, Any]:
        return {"env": self.env, "label": self.label, "choices": list(self.choices), "hint": self.hint}


@dataclass
class Engine:
    """A kind of model server this machine can run in a container."""

    id: str
    label: str
    backend_type: str
    image: str
    images: List[str]
    port: int
    data_path: str
    volume: str
    health_path: str
    stop_timeout: int
    flags: List[str]
    options: List[Option]
    summary: str

    def public(self) -> Dict[str, Any]:
        """The engine for the hello, as Go's JSON writes it (without the run flags)."""
        return {"id": self.id, "label": self.label, "backend_type": self.backend_type, "image": self.image, "images": list(self.images),
                "port": self.port, "data_path": self.data_path, "volume": self.volume, "health_path": self.health_path,
                "stop_timeout": self.stop_timeout, "options": [option.public() for option in self.options], "summary": self.summary}


STRATA_IMAGE = "ghcr.io/niko1221/strata"

# Engines in the order the console offers them.
ENGINES: List[Engine] = [
    Engine(
        id="strata", label="Strata", backend_type="strata",
        image=STRATA_IMAGE + ":latest-cuda13",
        images=[STRATA_IMAGE + ":latest-" + flavor for flavor in ("cuda13", "cuda12", "rocm-gfx120x", "rocm-gfx110x", "rocm-gfx1151", "sycl")],
        port=8080, data_path="/data", volume="strata-data", health_path="/health",
        # Strata's Docker guide: a 2-minute stop lets it unload cleanly, memlock pins the model's RAM.
        stop_timeout=120, flags=["--ulimit", "memlock=-1:-1", "--shm-size", "2g"],
        options=[
            Option("MODEL", "Model size", ["IQ2_XS", "IQ3_XXS", "IQ3_S", "Q2_0", "IQ1_M", "UD-IQ4_XS", "UD-Q4_K_XL", "UD-Q5_K_XL", "UD-Q6_K_XL", "Q8_0"], "Strata picks one for the card"),
            Option("FAMILY", "Family", ["qwen", "unsloth", "gyro", "coder", "swift"], "Strata's recommendation"),
            Option("CONTEXT", "Context", ["32k", "64k", "128k", "256k"], "The largest that fits"),
            Option("KV", "KV cache", ["int8", "q4_0", "k8v4", "fp16"], "int8"),
            Option("VISION", "Vision", ["off", "on", "gpu", "cpu"], "off"),
            Option("PARALLEL", "Parallel requests", ["1", "2", "3", "4", "5", "6", "7", "8"], "Fitted to the card"),
        ],
        summary="Strata's own image. The first start downloads the model into the shared volume (tens of GB), later starts take a minute or two to load it.",
    ),
    Engine(
        id="ollama", label="Ollama", backend_type="ollama",
        image="ollama/ollama:latest", images=["ollama/ollama:latest", "ollama/ollama:rocm"],
        port=11434, data_path="/root/.ollama", volume="ollama-models", health_path="/api/version",
        stop_timeout=30, flags=[],
        options=[
            Option("OLLAMA_CONTEXT_LENGTH", "Context", ["8192", "16384", "32768", "65536", "131072"], "Ollama's default"),
            Option("OLLAMA_KV_CACHE_TYPE", "KV cache", ["f16", "q8_0", "q4_0"], "f16"),
            Option("OLLAMA_FLASH_ATTENTION", "Flash attention", ["1", "0"], "Off"),
            Option("OLLAMA_NUM_PARALLEL", "Parallel requests", ["1", "2", "4"], "Ollama's default"),
            Option("OLLAMA_KEEP_ALIVE", "Keep a model loaded", ["5m", "30m", "-1"], "5m"),
        ],
        summary="The official Ollama image. Models pulled into the shared volume are there for every Ollama container.",
    ),
]


def engine_by_id(engine_id: str) -> Optional[Engine]:
    for engine in ENGINES:
        if engine.id == engine_id:
            return engine
    return None


def stop_timeout(engine: str) -> int:
    """How long a stop waits for the engine to unload."""
    found = engine_by_id(engine)
    return found.stop_timeout if found else 30


# Patterns are ASCII-only and matched whole (fullmatch), as Go's regexp does with ^...$.
# A plain image reference (registry/name:tag or @digest); never something the CLI reads as a flag.
IMAGE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@-]{0,254}")
VOLUME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
GPUS_PATTERN = re.compile(r"(all|none|[0-9]+(,[0-9]+)*)")
ENV_PATTERN = re.compile(r"[^\x00-\x1f]{0,200}")
NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,39}")
KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{0,100}")
DRIVE_PATTERN = re.compile(r"[A-Za-z]:[\\/]")
# A Windows share (\\host\share), which Go's filepath.IsAbs counts as absolute there.
UNC_PATTERN = re.compile(r"[\\/]{2}[^\\/]+[\\/]+[^\\/]+")


class SpecError(ValueError):
    pass


@dataclass
class Spec:
    """A container as the server describes it, checked by validate."""

    engine: str
    image: str
    port: int
    gpus: str
    env: Dict[str, str] = field(default_factory=dict)
    volume: str = ""


def text(value: Any) -> str:
    """A JSON value as Python's str() writes it (what the server's checks did, and Go's copy of them),
    except that whole numbers are written without ".0", as Go reads every JSON number as a float."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        return str(int(value))
    return str(value)


def is_absolute(volume: str) -> bool:
    """Go's filepath.IsAbs: a drive or a share on Windows, a leading slash elsewhere."""
    if sys.platform == "win32":
        return bool(DRIVE_PATTERN.match(volume) or UNC_PATTERN.match(volume))
    return volume.startswith("/")


def validate(raw: Any) -> Spec:
    """Checks a spec the way the server does, filling in the engine's defaults. Raises SpecError."""
    if not isinstance(raw, dict):
        raise SpecError("container must be an object")
    engine = engine_by_id(text(raw.get("engine")))
    if engine is None:
        raise SpecError("Unknown engine; use one of: " + ", ".join(item.id for item in ENGINES))
    image = text(raw.get("image")).strip() or engine.image
    if not IMAGE_PATTERN.fullmatch(image):
        raise SpecError(f"Image must be an image name such as {engine.image}")
    port = engine.port
    value = raw.get("port")
    if value is not None and text(value) not in ("", "0"):
        number = text(value)
        # Strict like Go's ParseFloat: no surrounding space, no digit separators.
        if number != number.strip() or "_" in number:
            raise SpecError("Port must be a number")
        try:
            parsed = float(number)
        except ValueError:
            raise SpecError("Port must be a number")
        port = int(parsed) if math.isfinite(parsed) else 0
    if port < 1024 or port > 65535:
        raise SpecError("Port must be between 1024 and 65535")
    gpus = "all"
    if raw.get("gpus") is not None:
        gpus = text(raw["gpus"]).replace(" ", "").lower() or "none"
    if not GPUS_PATTERN.fullmatch(gpus):
        raise SpecError("GPUs must be all, none, or card numbers such as 0 or 0,1")
    allowed = {option.env for option in engine.options}
    names = sorted(allowed)
    env: Dict[str, str] = {}
    values = raw.get("env")
    if isinstance(values, dict):
        for key, setting in values.items():
            if key not in allowed:
                raise SpecError(f"{key} isn't a {engine.label} setting; use one of: {', '.join(names)}")
            setting = text(setting).strip() if setting is not None else ""
            if not ENV_PATTERN.fullmatch(setting):
                raise SpecError(f"{key} has an invalid value")
            if setting:
                env[key] = setting
    elif values is not None:
        raise SpecError("env must be an object")
    spec = Spec(engine=engine.id, image=image, port=port, gpus=gpus, env=env)
    volume = text(raw.get("volume")).strip()
    if volume:
        if not VOLUME_PATTERN.fullmatch(volume) and not volume.startswith("/") and not is_absolute(volume) and not DRIVE_PATTERN.match(volume):
            raise SpecError("Volume must be a volume name such as strata-data or an absolute folder")
        if "," in volume or "\n" in volume:
            raise SpecError("Volume can't contain commas or line breaks")
        spec.volume = volume
    return spec


def check_name(name: str) -> None:
    """Refuses names that aren't the server's backend names."""
    if not isinstance(name, str) or not NAME_PATTERN.fullmatch(name):
        raise SpecError(f"{json.dumps(name)} isn't a container name (letters, numbers, - and _, up to 40)")


def check_key(key: str) -> None:
    if not KEY_PATTERN.fullmatch(key):
        raise SpecError("the API key has characters an API key can't have")


def check_bind(bind: str) -> str:
    if bind in ("", "127.0.0.1"):
        return "127.0.0.1"
    if bind == "0.0.0.0":
        return bind
    raise SpecError(f"{json.dumps(bind)} isn't an address containers listen on (127.0.0.1 or 0.0.0.0)")


def run_args(name: str, spec: Spec, key: str, bind: str) -> Tuple[List[str], str]:
    """The `docker run` for a container, and the digest of it that marks the container, so a changed
    spec shows as outdated. The digest is the one the server computed before containers moved to
    machines, so existing containers aren't replaced for no reason."""
    return run_args_for("docker", name, spec, key, bind)


def run_args_for(cli: str, name: str, spec: Spec, key: str, bind: str) -> Tuple[List[str], str]:
    """The run command for a CLI. Podman takes the same arguments as Docker but GPUs: it hands them
    over as CDI devices (the NVIDIA Container Toolkit's nvidia.com/gpu), where Docker has --gpus."""
    engine = engine_by_id(spec.engine)
    assert engine is not None
    env = dict(spec.env)
    if engine.id == "strata":
        # Its own preset inside the shared /data keeps settings, tuning and key per container.
        env["PRESET"] = name
        env["API_KEY"] = key
    volume = spec.volume or engine.volume
    args = [
        "run", "-d", "--name", PREFIX + name,
        "--label", f"{LABEL}={name}", "--label", f"ai-proxy.engine={engine.id}",
        "-p", f"{bind}:{spec.port}:{engine.port}", "-v", f"{volume}:{engine.data_path}",
        "--stop-timeout", str(engine.stop_timeout),
    ]
    args += engine.flags
    if cli == "podman" and spec.gpus != "none":
        for card in spec.gpus.split(","):
            args += ["--device", "nvidia.com/gpu=" + card]
    elif spec.gpus == "all":
        args += ["--gpus", "all"]
    elif spec.gpus != "none" and "," in spec.gpus:
        # Docker reads --gpus as CSV, so a list of cards needs the quotes.
        args += ["--gpus", f'"device={spec.gpus}"']
    elif spec.gpus != "none":
        args += ["--gpus", "device=" + spec.gpus]
    for setting in sorted(env):
        args += ["-e", f"{setting}={env[setting]}"]
    digest = hashlib.sha256(json.dumps(args + [spec.image]).encode()).hexdigest()[:16]
    return args + ["--label", "ai-proxy.spec=" + digest, spec.image], digest


def command_line(cli: str, args: List[str], key: str) -> str:
    """The run command to show, quoted for a shell, with the API key hidden."""
    return " ".join(shlex.quote(part) for part in [cli] + [arg.replace(key, "<api key>") if key else arg for arg in args])


def quiet() -> Dict[str, Any]:
    """No console window for the CLI on Windows (Go's process.Quiet)."""
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}


def lookup(source: Dict[str, Any], name: str) -> Any:
    """A JSON object's field the way Go's decoder finds it: the exact key, else any case of it."""
    if not isinstance(source, dict):
        return None
    if name in source:
        return source[name]
    for key, value in source.items():
        if isinstance(key, str) and key.lower() == name.lower():
            return value
    return None


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


class CLI:
    """Runs the container CLI with this machine's environment, so DOCKER_HOST and docker contexts
    work as they do in a terminal here."""

    def __init__(self, name: str, program: Optional[List[str]] = None) -> None:
        self.name = name
        # Runs instead of name from PATH (tests).
        self.program = program

    def command(self) -> Optional[List[str]]:
        """The program and leading arguments to run, or None when it isn't installed."""
        if self.program is not None:
            return self.program
        value = os.environ.get(COMMAND_ENV)
        if value:
            try:
                command = json.loads(value)
            except ValueError:
                return None
            if isinstance(command, list) and command and all(isinstance(part, str) for part in command):
                return command
            return None
        found = shutil.which(self.name)
        return [found] if found else None

    def missing(self) -> str:
        return f"{self.name} isn't installed or isn't on PATH"

    def run(self, timeout: float, *args: str) -> Tuple[int, str, str]:
        """Runs one command and returns its exit code and output. 127 means the CLI is missing, 124
        that it took longer than timeout."""
        command = self.command()
        if command is None:
            return 127, "", self.missing()
        try:
            done = subprocess.run([*command, *args], stdin=subprocess.DEVNULL, capture_output=True, timeout=timeout, **quiet())
        except subprocess.TimeoutExpired as expired:
            return 124, decode(expired.stdout), f"{self.name} {args[0]} took longer than {int(timeout)} s"
        except OSError as error:
            return 127, "", str(error)
        return done.returncode, decode(done.stdout), decode(done.stderr)

    def combined(self, timeout: float, *args: str) -> str:
        """Runs one command with stdout and stderr together, as `docker logs` shows both."""
        command = self.command()
        if command is None:
            return ""
        try:
            done = subprocess.run([*command, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout, **quiet())
        except subprocess.TimeoutExpired as expired:
            return decode(expired.stdout) or f"{self.name} {args[0]} took longer than {int(timeout)} s"
        except OSError as error:
            return str(error)
        output = decode(done.stdout)
        if done.returncode != 0 and not output:
            return f"exit status {done.returncode}"
        return output

    def stream(self, timeout: float, on_line: Callable[[str], None], *args: str) -> int:
        """Runs a long command (an image pull), passing each line on as it arrives."""
        command = self.command()
        if command is None:
            on_line(self.missing())
            return 127
        try:
            process = subprocess.Popen([*command, *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **quiet())
        except OSError as error:
            on_line(str(error))
            return 127
        timed_out = threading.Event()

        def expire() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            assert process.stdout is not None
            for raw in process.stdout:
                line = decode(raw).rstrip("\r\n ")
                if line.strip():
                    on_line(line)
            code = process.wait()
        finally:
            timer.cancel()
        if timed_out.is_set():
            on_line("Timed out")
            return 124
        return code if code >= 0 else 1

    def probe(self) -> Dict[str, Any]:
        """Whether the CLI is installed and its engine answers: asks for its version, which needs the engine up."""
        runtime: Dict[str, Any] = {"cli": self.name, "found": False, "online": False, "version": "", "error": ""}
        if self.command() is None:
            runtime["error"] = self.missing()
            return runtime
        runtime["found"] = True
        code, out, error_text = self.run(15, "version", "--format", "{{json .}}")
        # The first line: podman and the fake print one JSON object, a failing docker adds its error after.
        first = out.strip().split("\n", 1)[0]
        try:
            version = json.loads(first)
        except ValueError:
            version = {}
        server = lookup(version, "Server")
        server = server if isinstance(server, dict) else None
        # Docker always answers with its engine's part (Server). Podman on Linux runs without a
        # service and has none; where it needs one (a podman machine), it fails without it.
        runtime["online"] = code == 0 and (server is not None or self.name == "podman")
        runtime["version"] = as_text(lookup(lookup(version, "Client"), "Version"))
        if server is not None and as_text(lookup(server, "Version")):
            runtime["version"] = as_text(lookup(server, "Version"))
        if not runtime["online"]:
            message = error_text.strip() or out.strip() or f"{self.name} version failed"
            runtime["error"] = message[-500:]
        return runtime

    def inspect(self, names: List[str]) -> Dict[str, Dict[str, Any]]:
        """The state of the named containers (server names, without the prefix); missing ones are left out."""
        found: Dict[str, Dict[str, Any]] = {}
        if not names or self.command() is None:
            return found
        # inspect prints the containers it found even when some are missing (and exits 1).
        _, out, _ = self.run(20, "inspect", *[PREFIX + name for name in names])
        try:
            entries = json.loads(out.strip())
        except ValueError:
            return found
        if not isinstance(entries, list):
            return found
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            state = lookup(entry, "State") if isinstance(lookup(entry, "State"), dict) else {}
            config = lookup(entry, "Config") if isinstance(lookup(entry, "Config"), dict) else {}
            labels = lookup(config, "Labels") if isinstance(lookup(config, "Labels"), dict) else {}
            name = as_text(labels.get(LABEL))
            if not name:
                name = as_text(lookup(entry, "Name"))
                name = name[1:] if name.startswith("/") else name
                name = name[len(PREFIX):] if name.startswith(PREFIX) else name
            exit_code = lookup(state, "ExitCode")
            item: Dict[str, Any] = {
                "status": as_text(lookup(state, "Status")).lower() or "unknown", "running": lookup(state, "Running") is True,
                "health": None, "started_at": as_text(lookup(state, "StartedAt")), "finished_at": as_text(lookup(state, "FinishedAt")),
                "exit_code": exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else 0,
                "error": None, "spec": None, "image": as_text(lookup(config, "Image")),
            }
            health = lookup(state, "Health")
            if not isinstance(health, dict):
                health = lookup(state, "Healthcheck")
            if isinstance(health, dict) and as_text(lookup(health, "Status")):
                item["health"] = as_text(lookup(health, "Status"))
            if as_text(lookup(state, "Error")):
                item["error"] = as_text(lookup(state, "Error"))
            if "ai-proxy.spec" in labels:
                item["spec"] = as_text(labels["ai-proxy.spec"])
            found[name] = item
        return found


def decode(data: Any) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", "replace")


def new_cli(name: str) -> CLI:
    """The CLI the machine's settings name: docker unless they say podman."""
    return CLI("podman" if name == "podman" else "docker")


_detected_lock = threading.Lock()
_detected: Dict[str, Any] = {"at": 0.0, "cli": None}


def detect() -> CLI:
    """The container CLI installed here when the machine doesn't name one: Docker when it's installed
    and its engine answers, else Podman when it does, else whichever is installed (Docker first), so
    the console can say why it isn't answering."""
    with _detected_lock:
        if _detected["cli"] is not None and time.monotonic() - _detected["at"] < DETECT_FOR:
            return _detected["cli"]
        _detected["cli"], _detected["at"] = pick(new_cli("docker"), new_cli("podman")), time.monotonic()
        return _detected["cli"]


def pick(*candidates: CLI) -> CLI:
    installed = []
    for cli in candidates:
        if cli.command() is None:
            continue
        if cli.probe()["online"]:
            return cli
        installed.append(cli)
    return installed[0] if installed else candidates[0]


def on_path(machine: settings.Machine) -> CLI:
    """The CLI the machine names, or the first of docker and podman on PATH, without running either:
    for a machine that hasn't turned containers on."""
    if machine.container_cli in ("docker", "podman"):
        return new_cli(machine.container_cli)
    for name in ("docker", "podman"):
        cli = new_cli(name)
        if cli.command() is not None:
            return cli
    return new_cli("docker")


def forget_detection() -> None:
    """Makes the next detect look again (tests, and `containers cli` changes)."""
    with _detected_lock:
        _detected["cli"], _detected["at"] = None, 0.0


def cli_for(machine: settings.Machine) -> CLI:
    """The CLI the machine's settings name, or the one installed here when they don't name one (see detect)."""
    if machine.container_cli in ("docker", "podman"):
        return new_cli(machine.container_cli)
    return detect()


class Job:
    """The latest start, stop or switch of one container, shown while it runs and after."""

    def __init__(self, action: str) -> None:
        self.lock = threading.Lock()
        self.action = action
        self.state = "running"
        self.started_at = time.time()
        self.finished_at = 0.0
        self.error = ""
        self.lines: List[str] = []

    def say(self, line: str) -> None:
        with self.lock:
            self.lines.append(line)
            del self.lines[:-JOB_LINES]

    def public(self) -> Dict[str, Any]:
        """The job for the server: its last lines only."""
        with self.lock:
            return {"action": self.action, "state": self.state, "started_at": self.started_at,
                    "finished_at": self.finished_at or None, "error": self.error or None, "lines": self.lines[-SHOWN_LINES:]}

    def running(self) -> bool:
        with self.lock:
            return self.state == "running"


def target(value: Any) -> Dict[str, Any]:
    """One container in a request: its spec, Strata key and listen address."""
    value = value if isinstance(value, dict) else {}
    return {"spec": value.get("spec"), "key": as_text(value.get("key")), "bind": as_text(value.get("bind"))}


class Manager:
    """Answers the server's container requests and keeps the jobs they start."""

    def __init__(self, machine: Optional[Callable[[], settings.Machine]] = None) -> None:
        # Reads this machine's settings, fresh for every request so `agent-machine containers off`
        # applies at once; tests replace it.
        self.machine = machine or settings.Machine.load
        self.lock = threading.Lock()
        self.jobs: Dict[str, Job] = {}
        # The ports of the containers the server has named, which its tunnels may reach.
        self._ports: set = set()

    def ports(self) -> set:
        """The ports of the containers the server has asked about: where they serve on this machine."""
        with self.lock:
            return set(self._ports)

    def remember(self, spec: Any) -> None:
        """Keeps a container spec's port, from any request that carries one."""
        port = spec.get("port") if isinstance(spec, dict) else None
        if isinstance(port, bool) or not isinstance(port, (int, float)) or not 1 <= port <= 65535:
            return
        with self.lock:
            self._ports.add(int(port))

    def describe(self) -> Dict[str, Any]:
        """What the hello says about containers: whether they're on, the CLI, and the engines."""
        machine = self.machine()
        if not machine.containers:
            # Nothing runs until the owner opts in; finding the CLI only looks at PATH.
            cli = on_path(machine)
            return {"enabled": False, "cli": cli.name, "found": cli.command() is not None, "online": False, "version": "", "error": "", "engines": []}
        cli = cli_for(machine)
        runtime = cli.probe()
        return {"enabled": True, "cli": cli.name, "found": runtime["found"], "online": runtime["online"],
                "version": runtime["version"], "error": runtime["error"], "engines": [engine.public() for engine in ENGINES]}

    def handle(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Answers one {"type": "container"} request, as the container_result to send back."""
        machine = self.machine()
        targets = request.get("containers") if isinstance(request.get("containers"), dict) else {}
        if machine.containers:
            self.remember(request.get("spec"))
            for value in targets.values():
                self.remember(target(value)["spec"])
        try:
            result = self._handle(machine, request, targets)
            result["ok"] = True
        except Exception as error:
            result = {"ok": False, "error": str(error) or type(error).__name__}
        result["type"] = "container_result"
        result["request"] = request.get("request")
        return result

    def _handle(self, machine: settings.Machine, request: Dict[str, Any], targets: Dict[str, Any]) -> Dict[str, Any]:
        if not machine.containers:
            raise SpecError("Containers are off on this machine (agent-machine containers on)")
        cli = cli_for(machine)
        op = as_text(request.get("op"))
        if op == "status":
            return self.status(cli, targets)
        name = as_text(request.get("name"))
        check_name(name)
        if op in ("start", "restart"):
            spec, args = prepare(cli, name, target(request))
            others: List[Dict[str, str]] = []
            if op == "start":
                listed = request.get("stop_first")
                for other in listed if isinstance(listed, list) else []:
                    other = other if isinstance(other, dict) else {}
                    check_name(as_text(other.get("name")))
                    others.append({"name": as_text(other.get("name")), "engine": as_text(other.get("engine"))})

            def work(job: Job) -> None:
                if op == "restart":
                    stop(cli, name, spec.engine, job)
                start(cli, name, spec, args, others, job)

            return {"job": self.run(name, "switch" if others else op, work).public()}
        if op == "stop":
            engine = as_text(request.get("engine"))
            return {"job": self.run(name, "stop", lambda job: stop(cli, name, engine, job)).public()}
        if op == "remove":
            code, _, error_text = cli.run(stop_timeout(as_text(request.get("engine"))) + 60, "rm", "-f", PREFIX + name)
            if code != 0 and "no such" not in error_text.lower():
                raise SpecError(error_text.strip() or f"{cli.name} rm failed")
            with self.lock:
                self.jobs.pop(name, None)
            return {}
        if op == "logs":
            key = as_text(request.get("key"))
            check_key(key)
            requested = request.get("tail")
            requested = int(requested) if isinstance(requested, (int, float)) and not isinstance(requested, bool) else 0
            tail = 400 if requested == 0 else min(max(requested, 10), 5000)
            output = cli.combined(20, "logs", "--tail", str(tail), PREFIX + name)
            if key:
                output = output.replace(key, "<api key>")
            return {"text": output}
        raise SpecError(f"unknown container request {json.dumps(op)}")

    def status(self, cli: CLI, targets: Dict[str, Any]) -> Dict[str, Any]:
        names = [name for name in targets if NAME_PATTERN.fullmatch(name)]
        states = cli.inspect(names)
        items: Dict[str, Any] = {}
        for name, value in targets.items():
            item: Dict[str, Any] = {"state": None, "job": None, "outdated": False, "command": None, "problem": None}
            items[name] = item
            try:
                check_name(name)
            except SpecError as error:
                item["problem"] = str(error)
                continue
            state = states.get(name)
            item["state"] = state
            job = self.job(name)
            if job is not None:
                item["job"] = job.public()
            wanted = target(value)
            try:
                _, args = prepare(cli, name, wanted)
            except SpecError as error:
                item["problem"] = str(error)
                continue
            digest = args[-2][len("ai-proxy.spec="):]
            item["outdated"] = state is not None and state["spec"] != digest
            item["command"] = command_line(cli.name, args, wanted["key"])
        return {"runtime": cli.probe(), "containers": items}

    def job(self, name: str) -> Optional[Job]:
        with self.lock:
            return self.jobs.get(name)

    def run(self, name: str, action: str, work: Callable[[Job], None]) -> Job:
        """Starts work as the container's job, unless one is already running for it."""
        with self.lock:
            current = self.jobs.get(name)
            if current is not None and current.running():
                raise SpecError(f"{name} is busy with {current.action}; wait for it to finish")
            job = Job(action)
            self.jobs[name] = job

        def go() -> None:
            error = ""
            try:
                work(job)
            except Exception as failure:
                error = str(failure) or type(failure).__name__
                job.say("Failed: " + error)
            with job.lock:
                job.finished_at = time.time()
                job.state, job.error = ("failed", error) if error else ("done", "")

        threading.Thread(target=go, name=f"container {name}", daemon=True).start()
        return job


def prepare(cli: CLI, name: str, wanted: Dict[str, Any]) -> Tuple[Spec, List[str]]:
    """Checks a container's spec, key and address, and builds its run arguments."""
    spec = validate(wanted["spec"])
    check_key(wanted["key"])
    bind = check_bind(wanted["bind"])
    args, _ = run_args_for(cli.name, name, spec, wanted["key"], bind)
    return spec, args


def start(cli: CLI, name: str, spec: Spec, args: List[str], others: List[Dict[str, str]], job: Job) -> None:
    for other in others:
        job.say(f"Stopping {other['name']} to free the GPU")
        stop(cli, other["name"], other["engine"], job)
    digest = args[-2][len("ai-proxy.spec="):]
    image = args[-1]
    current = cli.inspect([name]).get(name)
    if current is not None and current["spec"] == digest:
        if current["running"]:
            job.say("Already running")
            return
        job.say("Starting the container")
        code, _, error_text = cli.run(120, "start", PREFIX + name)
        if code != 0:
            raise SpecError(error_text.strip() or "start failed")
        job.say("Started")
        return
    if current is not None:
        job.say("Its settings changed: replacing the old container")
        cli.run(stop_timeout(spec.engine) + 60, "rm", "-f", PREFIX + name)
    if cli.run(30, "image", "inspect", image)[0] != 0:
        job.say("Pulling " + image)
        if cli.stream(3600, job.say, "pull", image) != 0:
            raise SpecError(f"Couldn't pull {image}")
    job.say("Creating the container")
    code, _, error_text = cli.run(180, *args)
    if code != 0:
        raise SpecError(error_text.strip() or "run failed")
    job.say("Started. Strata downloads its model on the first start, then loads it." if spec.engine == "strata" else "Started")


def stop(cli: CLI, name: str, engine: str, job: Job) -> None:
    timeout = stop_timeout(engine)
    job.say(f"Stopping {name} (up to {timeout} s while it unloads)")
    code, _, error_text = cli.run(timeout + 60, "stop", "-t", str(timeout), PREFIX + name)
    if code != 0 and "no such" not in error_text.lower():
        raise SpecError(error_text.strip() or "stop failed")
