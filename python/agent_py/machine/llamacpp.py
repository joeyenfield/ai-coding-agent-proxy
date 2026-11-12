"""llama.cpp's llama-server on this machine, for the server: run in router mode, started and stopped
here, its LLAMA_ARG_* settings saved from the console, and installed from the console. Mirrors
machine/internal/servers/{llamacpp.go, llamacpp_settings.go, llamacpp_install.go} in the Go
agent-machine; servers.Manager dispatches to it.

Router mode: started without a model, llama-server serves every model in its cache (downloaded from
Hugging Face through its own /models API) and folder, loading each on the first request for it and
unloading the least recently used. It always listens on loopback: agents reach it through the
machine's tunnel, so it never needs an API key. Nothing that would let a model run commands here
can be set (llama-server's tools, its MCP client, its agent mode), nor an API key or presets file.

Installing: in Termux, Termux's own packages (llama-cpp and its OpenCL or Vulkan backend) through
apt; anywhere else, the build llama.cpp publishes on GitHub for this platform and backend, unpacked
into agent-machine's folder, and only when its SHA-256 is the one GitHub lists. It runs as a job:
the request answers at once and the machine's llama.cpp state carries its progress.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from . import servers, settings
from .servers import HARDWARE, MEMORY, SCHED, SERVER, Refused, Table, as_int, setting

# A command to run instead of llama-server, as a JSON list (tests use a fake).
LLAMA_ENV = "AI_PROXY_LLAMACPP_COMMAND"
# Another releases API (tests use a fake), and a command to run instead of Termux's apt-get.
RELEASES_ENV = "AI_PROXY_LLAMACPP_RELEASES"
APT_ENV = "AI_PROXY_LLAMACPP_APT"
RELEASES_URL = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=8"
# The most an archive may unpack to: CUDA builds are under 2 GB.
MAX_UNPACKED = 6 << 30

MODELS, REASONING = "Models", "Reasoning"
GROUPS = [MEMORY, SCHED, HARDWARE, REASONING, MODELS, SERVER]
CACHE_TYPES = ["f16", "bf16", "q8_0", "q5_1", "q5_0", "q4_1", "q4_0", "iq4_nl"]

# The table the console shows, in order: the same as the Go agent-machine's (llamacpp_settings.go).
SETTINGS = [
    setting("ctx_size", MEMORY, "Context length (tokens)", "int", 16384,
            "8192 = small tasks, 16384 = recommended, 32768 = larger tasks, 65536 = large agent sessions. 0 = the model's own (with Fit on, the largest that fits).",
            "LLAMA_ARG_CTX_SIZE", minimum=0, maximum=1048576),
    setting("cache_type_k", MEMORY, "KV cache type (K)", "choice", "f16",
            "f16 = full precision, most memory. q8_0 ≈ half. q4_0 ≈ a quarter, with some loss.", "LLAMA_ARG_CACHE_TYPE_K", choices=CACHE_TYPES),
    setting("cache_type_v", MEMORY, "KV cache type (V)", "choice", "f16", "A quantized V cache needs Flash Attention.", "LLAMA_ARG_CACHE_TYPE_V", choices=CACHE_TYPES),
    setting("flash_attn", MEMORY, "Flash Attention", "choice", "auto", "auto = where the backend supports it.", "LLAMA_ARG_FLASH_ATTN", choices=["auto", "on", "off"]),
    setting("fit", MEMORY, "Fit to memory", "bool", True,
            "Pick the context and GPU layers that aren't set so the model fits in this machine's memory.", "LLAMA_ARG_FIT", false="off", true="on"),
    setting("cache_ram", MEMORY, "Prompt cache (MiB)", "int", 8192, "RAM for reusing earlier prompts. 0 = off, -1 = no limit.", "LLAMA_ARG_CACHE_RAM", minimum=-1, maximum=1048576),
    setting("parallel", SCHED, "Parallel requests", "int", 1, "Slots per model. 1 = lowest KV cache memory; -1 = automatic.", "LLAMA_ARG_N_PARALLEL", minimum=-1, maximum=128),
    setting("models_max", SCHED, "Max loaded models", "int", 1,
            "Models loaded at once; the least recently used is unloaded first. 0 = no limit.", "LLAMA_ARG_MODELS_MAX", minimum=0, maximum=128),
    setting("models_autoload", SCHED, "Load models on request", "bool", True,
            "Load a model when an agent asks for it. Off: only models loaded from the console answer.", "LLAMA_ARG_MODELS_AUTOLOAD"),
    setting("gpu_layers", HARDWARE, "GPU layers", "choice", "auto", "Layers to put on the GPU: auto, all, or a number. 0 = CPU only.",
            "LLAMA_ARG_N_GPU_LAYERS", choices=["auto", "all", "0"], editable=True),
    setting("device", HARDWARE, "Devices", "str", "",
            'Which devices to use, e.g. "CUDA0" or "Vulkan0,Vulkan1" ("none" = CPU only). Empty = all. The log lists them when llama-server starts.', "LLAMA_ARG_DEVICE"),
    setting("threads", HARDWARE, "CPU threads", "int", -1, "-1 = automatic.", "LLAMA_ARG_THREADS", minimum=-1, maximum=1024),
    setting("reasoning_budget", REASONING, "Thinking budget (tokens)", "int", -1,
            "How much a thinking model may think before it answers. -1 = no limit, 0 = don't think. Small models on slow machines answer far sooner with a budget.",
            "LLAMA_ARG_THINK_BUDGET", minimum=-1, maximum=1048576),
    setting("jinja", REASONING, "Model's chat template", "bool", True, "Use the chat template in the model (needed for tool calls).", "LLAMA_ARG_JINJA"),
    setting("cache_dir", MODELS, "Download folder", "str", "", "Where models downloaded from Hugging Face go. Empty = llama.cpp's standard cache.", "LLAMA_CACHE"),
    setting("models_dir", MODELS, "GGUF folder", "str", "", "A folder of .gguf files to serve as well (a subfolder for a model in several files). Empty = llamacpp-models in agent-machine's data folder (AI_PROXY_MACHINE_DATA, else its own folder). Models agent-machine downloads itself go here.", "LLAMA_ARG_MODELS_DIR"),
    setting("webui", SERVER, "Web UI", "bool", False, "llama-server's own chat page, on this computer only.", "LLAMA_ARG_UI", false="off", true="on"),
    setting("metrics", SERVER, "Metrics endpoint", "bool", False, "Prometheus metrics at /metrics.", "LLAMA_ARG_ENDPOINT_METRICS"),
]

# What agent-machine sets itself, or the server must never set.
RESERVED = ["LLAMA_ARG_HOST", "LLAMA_ARG_PORT", "LLAMA_API_KEY", "LLAMA_ARG_API_KEY", "LLAMA_ARG_API_KEY_FILE", "LLAMA_ARG_MODELS_PRESET", "LLAMA_ARG_REUSE_PORT", "LLAMA_ARG_CORS_ORIGINS"]
# Parts of names that switch on tools, MCP or agent mode: ways for a model to run things here.
FORBIDDEN = ["TOOL", "MCP", "AGENT", "SSL", "PROPS"]


def _extra(name: str) -> str:
    if not name.startswith("LLAMA_ARG_") and not name.startswith("GGML_"):
        return "only LLAMA_ARG_* and GGML_* variables can be set here"
    # Windows reads environment names without regard to case, so a lower-case name would slip past the lists below.
    if name != name.upper():
        return "write the variable's name in upper case"
    if name in RESERVED:
        return "agent-machine sets where llama-server listens, and it takes no API key or presets file here"
    if any(part in name for part in FORBIDDEN):
        return "llama-server's tools, MCP servers, agent mode and server options can't be turned on from the console"
    return ""


TABLE = Table(SETTINGS, GROUPS, "llamacpp.json", _extra)


def _dropped(name: str) -> bool:
    upper = name.upper()
    return upper in RESERVED or (upper.startswith("LLAMA_ARG_") and any(part in upper for part in FORBIDDEN))


def default_models_dir() -> str:
    """The GGUF folder when none is set: llamacpp-models in agent-machine's data folder (its own
    folder, unless AI_PROXY_MACHINE_DATA moves it)."""
    return str(settings.data() / "llamacpp-models")


def managed_env(config: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """The variables the config sets (None: unset), with always a GGUF folder: where agent-machine
    downloads models when the router can't."""
    env = TABLE.managed(config)
    if env.get("LLAMA_ARG_MODELS_DIR") is None:
        env["LLAMA_ARG_MODELS_DIR"] = default_models_dir()
    return env


def models_dir() -> str:
    """The GGUF folder agent-machine's llama-server serves."""
    return managed_env(TABLE.load())["LLAMA_ARG_MODELS_DIR"] or default_models_dir()


def environment(base: Dict[str, str], config: Dict[str, Any]) -> Dict[str, str]:
    """The environment llama-server starts with: base without what the console mustn't set, with the managed variables applied."""
    env = TABLE.environment(base, config, _dropped)
    if TABLE.managed(config).get("LLAMA_ARG_MODELS_DIR") is None:
        env["LLAMA_ARG_MODELS_DIR"] = default_models_dir()
    return env


def warnings(config: Dict[str, Any]) -> List[str]:
    found = []
    context = as_int(config.get("ctx_size")) or 0
    if context and context < 8192:
        found.append("Context below 8K is likely too small for coding agents.")
    elif context and context < 16384:
        found.append("Some coding agents may exhaust this context rapidly.")
    if (as_int(config.get("parallel")) or 0) > 1:
        found.append("Parallel requests increase KV cache memory requirements.")
    if config.get("cache_type_v") not in ("f16", "bf16") and config.get("flash_attn") == "off":
        found.append("A quantized V cache needs Flash Attention; set it to auto or on.")
    return found


def log_path() -> Path:
    return settings.home() / "llamacpp.log"


def server_name() -> str:
    return "llama-server.exe" if sys.platform == "win32" else "llama-server"


def termux_prefix() -> str:
    """Termux's prefix when agent-machine runs in Termux on Android, else ""."""
    prefix = os.environ.get("PREFIX", "")
    if "com.termux" in prefix:
        return prefix
    if os.environ.get("TERMUX_VERSION"):
        return prefix or "/data/data/com.termux/files/usr"
    return ""


def _find() -> Optional[str]:
    record = load_install()
    if record.get("binary") and os.path.isfile(record["binary"]):
        return record["binary"]
    found = shutil.which(server_name())
    if found:
        return found
    candidates = ["/opt/homebrew/bin/llama-server", "/usr/local/bin/llama-server", "/usr/bin/llama-server"]
    if termux_prefix():
        candidates.insert(0, os.path.join(termux_prefix(), "bin", "llama-server"))
    return next((candidate for candidate in candidates if os.path.isfile(candidate)), None)


def llama_command() -> Optional[List[str]]:
    """The llama-server to run: the one installed from the console, else one on PATH or where Termux and Homebrew put it."""
    return servers.command(LLAMA_ENV, _find)


def running(port: int) -> bool:
    """Whether a llama-server answers on the port: /health is 200 when it's ready and 503 while it loads, and never needs a key."""
    try:
        with httpx.Client(trust_env=False, timeout=1.5) as http:
            return http.get(f"http://127.0.0.1:{port}/health").status_code in (200, 503)
    except httpx.HTTPError:
        return False


# Newer builds print "version: 0.8.0-dev (build 12032, commit dde879bc4)", older ones "version: 12032 (abc1234)".
VERSION_LINE = re.compile(rb"version:\s*(?:\S+\s*\(build (\d+)|(\d+)\s*\([0-9a-f]+\))")
# llama-server --version is asked once per binary, not every minute.
_versions: Dict[Tuple[str, ...], str] = {}
_versions_lock = threading.Lock()


def version(program: Optional[List[str]]) -> str:
    """The build of a llama-server ("b12032"), from its --version."""
    if not program:
        return ""
    key: Tuple[str, ...] = tuple(program)
    try:
        key += (str(os.stat(program[0]).st_mtime),)
    except OSError:
        pass
    with _versions_lock:
        if key in _versions:
            return _versions[key]
        try:
            done = subprocess.run([*program, "--version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=15, check=False, env=with_libraries(dict(os.environ), program[0]), **servers.QUIET)
            output = done.stdout or b""
        except (OSError, subprocess.TimeoutExpired):
            output = b""
        found = parse_version(output)
        prefix = termux_prefix()
        if not found and prefix and program[0].startswith(prefix):
            # Termux builds from a source tarball, whose --version names no build: its package does.
            found = termux_version(prefix)
        _versions[key] = found
        return _versions[key]


def termux_version(prefix: str) -> str:
    """Termux's llama-cpp package version, as llama.cpp names it: "b9590" for the old "0.0.0-b9590" packages, else the release ("0.8.0")."""
    try:
        done = subprocess.run([os.path.join(prefix, "bin", "dpkg-query"), "-W", "-f=${Version}", "llama-cpp"], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15, check=False, **servers.QUIET)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return package_version(done.stdout.decode(errors="replace")) if done.returncode == 0 else ""


def package_version(text: str) -> str:
    text = text.strip()
    if ":" in text:
        text = text.split(":", 1)[1]  # an epoch
    _, found, build = text.partition("-b")
    if found and build:
        return "b" + build.split("-", 1)[0]
    return text.split("-", 1)[0]


def parse_version(output: bytes) -> str:
    """The build in llama-server's --version, as its releases name it ("b12032")."""
    found = VERSION_LINE.search(output)
    if not found:
        return ""
    return "b" + (found.group(1) or found.group(2)).decode()


def pid() -> int:
    """The llama-server this agent-machine started, while it still runs."""
    value = int(servers.load_state().get("llamacpp_pid") or 0)
    return value if value > 0 and settings.alive(value) else 0


def install_root() -> Path:
    """The installed builds, in agent-machine's data folder: they're hundreds of MB."""
    return settings.data() / "llamacpp"


def install_log() -> Path:
    return settings.home() / "llamacpp-install.log"


def install_record_path() -> Path:
    return settings.home() / "llamacpp-install.json"


def load_install() -> Dict[str, Any]:
    """llamacpp-install.json: what the console installed, and where its llama-server is."""
    try:
        value = json.loads(install_record_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def save_install(record: Dict[str, Any]) -> None:
    # The Go agent-machine's layout: its fields in order, empty ones left out.
    ordered = {key: record[key] for key in ("source", "release", "variant", "binary", "dir", "installed_at") if record.get(key) or key in ("source", "variant", "binary", "installed_at")}
    path = install_record_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(ordered, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(temporary, path)


def with_libraries(env: Dict[str, str], binary: str) -> Dict[str, str]:
    """Lets a llama-server unpacked from a release find the libraries beside it (its backends, and
    CUDA's runtime): Linux and macOS don't look there by themselves."""
    name = {"linux": "LD_LIBRARY_PATH", "darwin": "DYLD_LIBRARY_PATH"}.get(sys.platform) or ("LD_LIBRARY_PATH" if sys.platform.startswith("freebsd") else "")
    root = os.path.normpath(str(install_root())) + os.sep
    if not name or not os.path.normpath(binary).startswith(root):
        return env
    folder = os.path.dirname(binary)
    env[name] = folder + os.pathsep + env[name] if env.get(name) else folder
    return env


def now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def state(port: int) -> Dict[str, Any]:
    """The "llamacpp" part of the servers' state."""
    program = llama_command()
    record = load_install()
    result: Dict[str, Any] = {
        "found": program is not None, "running": running(port), "managed": pid() > 0, "port": port,
        "version": version(program), "install": progress(),
        # Where it came from: the console's install (and which build), or found here.
        "source": None, "variant": None, "release": None,
    }
    if program is not None and record.get("binary") and program[0] == record["binary"]:
        result.update(source=record.get("source"), variant=record.get("variant"), release=record.get("release", ""))
    elif program is not None:
        result["source"] = "found"
    result["platform"] = "termux" if termux_prefix() else f"{go_os()}-{go_arch()}"
    return result


def settings_answer(port: int) -> Dict[str, Any]:
    """The "llamacpp_settings" answer: the table, the saved config, and whether the llama-server
    agent-machine runs was started with it."""
    config = TABLE.load()
    env = dict(sorted(managed_env(config).items()))
    matches = None
    saved = servers.load_state()
    if pid() and running(port) and isinstance(saved.get("llamacpp_env"), dict):
        matches = saved["llamacpp_env"] == env
    return {"schema": TABLE.schema(), "config": config, "env": env, "warnings": warnings(config),
            "applied_matches_saved": matches, "config_path": str(TABLE.path()), "log_path": str(log_path())}


def start(port: int) -> None:
    if running(port):
        return
    program = llama_command()
    if program is None:
        raise Refused("llama.cpp isn't installed here: install it from the console, or put llama-server on PATH")
    if installing():
        raise Refused("llama.cpp is being installed: start it when that's done")
    config = TABLE.load()
    env = dict(sorted(managed_env(config).items()))
    # The router refuses to start without its GGUF folder.
    try:
        os.makedirs(env["LLAMA_ARG_MODELS_DIR"] or default_models_dir(), exist_ok=True)
    except OSError as error:
        raise Refused(f"couldn't make the GGUF folder {env['LLAMA_ARG_MODELS_DIR']}: {error}")
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as log:
        log.write(f"# agent-machine: starting {' '.join(program)} (router mode) on 127.0.0.1:{port} at {datetime.datetime.now().astimezone().isoformat(timespec='seconds')}\n")
        for name, value in env.items():
            log.write(f"#   {name}={'(unset)' if value is None else value}\n")
        log.flush()
        flags: Dict[str, Any] = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)}
            if sys.platform == "win32" else {"start_new_session": True})
        try:
            # No model: router mode. Where it listens is agent-machine's to say, on the command line,
            # which wins over any variable.
            process = subprocess.Popen([*program, "--host", "127.0.0.1", "--port", str(port)], stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       env=with_libraries(environment(dict(os.environ), config), program[0]), cwd=str(settings.home()), **flags)
        except OSError as error:
            raise Refused(f"couldn't start llama-server: {error}")
    threading.Thread(target=process.wait, name="llamacpp-wait", daemon=True).start()
    saved = servers.load_state()
    saved.update(llamacpp_pid=process.pid, llamacpp_env=env)
    servers.save_state(saved)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if running(port):
            return
        if process.poll() is not None:
            raise Refused(f"llama-server exited while starting: {servers.tail(path, 600)}")
        time.sleep(0.25)
    raise Refused(f"llama-server started but doesn't answer yet; see {path}")


def stop(port: int) -> None:
    started = pid()
    if not started:
        if running(port):
            raise Refused("a llama-server agent-machine didn't start is running on this port, so it can't stop it; "
                          "stop it on this computer, then start llama.cpp from the console")
        return
    # The router's own instances are in its process group, and go with it.
    servers.stop_tree(started)
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline and (settings.alive(started) or running(port)):
        time.sleep(0.2)
    if settings.alive(started):
        # Keep the PID: clearing it would make a later stop say llama-server was
        # started outside agent-machine, for a process this machine itself started.
        raise Refused("llama-server is still running after being asked to stop; try again")
    saved = servers.load_state()
    saved.update(llamacpp_pid=0, llamacpp_env=None)
    servers.save_state(saved)
    if running(port):
        raise Refused("llama-server is still answering after being stopped")


def action(request: Dict[str, Any], port: int) -> None:
    op = request.get("op")
    if op in ("settings", "install_options"):
        return
    if op == "configure":
        TABLE.save(request.get("config"))
    elif op == "start":
        start(port)
    elif op == "stop":
        stop(port)
    elif op == "restart":
        stop(port)
        start(port)
    elif op == "install":
        # A release goes into a folder of its own, so a running llama-server keeps serving until it's restarted onto the new one.
        start_install(str(request.get("variant") or ""))
    elif op == "uninstall":
        if pid():
            raise Refused("stop llama.cpp before removing it")
        uninstall()
    else:
        raise Refused(f"unknown request: {op} llamacpp")


# Installing

_job_lock = threading.Lock()
_job: Optional[Dict[str, Any]] = None


def installing() -> bool:
    with _job_lock:
        return _job is not None and _job["state"] == "installing"


def progress() -> Optional[Dict[str, Any]]:
    """The job's state for the console, or None when nothing was installed since agent-machine started."""
    with _job_lock:
        if _job is None:
            return None
        shown = {key: _job[key] for key in ("state", "variant", "release", "step", "done", "total", "error", "at")}
        for key in ("release", "step", "error"):
            if not shown[key]:
                del shown[key]
        return shown


def _set(**fields: Any) -> None:
    with _job_lock:
        if _job is not None:
            _job.update(fields)


# Termux's packages: the backends are split out, each with what it needs to reach the phone's own
# driver (the vendor's OpenCL library, Android's Vulkan loader).
TERMUX_VARIANTS = [
    {"id": "opencl", "label": "OpenCL (phone GPU)", "help": "Adreno and other phone GPUs, through the phone's own OpenCL driver. The fastest on Snapdragon phones.",
     "packages": ["llama-cpp", "llama-cpp-backend-opencl", "ocl-icd", "opencl-vendor-driver"]},
    {"id": "vulkan", "label": "Vulkan (phone GPU)", "help": "Through Android's Vulkan driver; often slower than OpenCL on Adreno, and not every model works.",
     "packages": ["llama-cpp", "llama-cpp-backend-vulkan", "vulkan-loader-android"]},
    {"id": "cpu", "label": "CPU only", "packages": ["llama-cpp"]},
]

ASSET_NAME = re.compile(r"^llama-(b\d+)-bin-([A-Za-z0-9.-]+)\.(tar\.gz|zip)$")
RUNTIME_NAME = re.compile(r"^cudart-llama-(?:b\d+-)?bin-([A-Za-z0-9.-]+)\.(tar\.gz|zip)$")
VARIANT_ID = re.compile(r"[a-z0-9][a-z0-9.-]{0,40}")
OS_RELEASE = re.compile(r"^\d+\.\d+$")


def go_os() -> str:
    """This computer's system as Go names it."""
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def go_arch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


def platform_tokens(goos: str, goarch: str) -> Tuple[List[str], str]:
    """How llama.cpp's release names this computer: its system's names and its architecture."""
    arch = {"amd64": "x64", "arm64": "arm64"}.get(goarch, "")
    systems = {"linux": ["ubuntu", "linux"], "windows": ["win"], "darwin": ["macos"]}.get(goos, [])
    return systems, arch


def variant_of(platform_name: str, goos: str, goarch: str) -> str:
    """The backend a release platform name is for on this system, "" when it's another system's."""
    systems, arch = platform_tokens(goos, goarch)
    if not arch:
        return ""
    tokens = platform_name.split("-")
    if len(tokens) < 2 or tokens[0] not in systems:
        return ""
    rest: List[str] = []
    found = False
    for token in tokens[1:]:
        if token == arch and not found:
            found = True
            continue
        rest.append(token)
    if not found:
        return ""
    # An OS release number (ubuntu-26.04-s390x) isn't a backend.
    if rest and OS_RELEASE.match(rest[0]):
        return ""
    return "-".join(rest) or "cpu"


def variant_label(variant: str, goos: str) -> Tuple[str, str]:
    if variant == "cpu" and goos == "darwin":
        return "Metal (Apple GPU)", "Apple's GPU, and the CPU."
    if variant == "cpu":
        return "CPU only", ""
    if variant == "vulkan":
        return "Vulkan", "AMD, Intel and NVIDIA GPUs, through their Vulkan drivers."
    if variant.startswith("cuda-"):
        return f"CUDA {variant[5:]} (NVIDIA)", "Needs an NVIDIA driver that supports this CUDA version; its runtime is downloaded too."
    if variant.startswith("rocm-"):
        return f"ROCm {variant[5:]} (AMD)", "Needs AMD's ROCm driver."
    if variant == "sycl":
        return "SYCL (Intel)", "Intel GPUs, through oneAPI."
    if variant.startswith("sycl-"):
        return f"SYCL {variant[5:].upper()} (Intel)", "Intel GPUs, through oneAPI."
    if variant.startswith("openvino"):
        return "OpenVINO (Intel)", "Intel CPUs, GPUs and NPUs, through OpenVINO."
    if variant == "snapdragon":
        return "Snapdragon (Adreno GPU, Hexagon NPU)", ""
    if variant == "opencl-adreno":
        return "OpenCL (Adreno)", "Snapdragon laptops' Adreno GPU."
    return variant, ""


def release_variants(found: Dict[str, Any], goos: str, goarch: str) -> List[Dict[str, Any]]:
    """What a release offers this computer, best guess first (recommended), in id order.
    Each variant's asset (and CUDA's runtime) ride along under "_asset" and "_runtime"."""
    assets = [item for item in found.get("assets") or [] if isinstance(item, dict)]
    runtimes: Dict[str, Dict[str, Any]] = {}
    for item in assets:
        match = RUNTIME_NAME.match(str(item.get("name") or ""))
        if match:
            variant = variant_of(match.group(1), goos, goarch)
            if variant:
                runtimes[variant] = item
    variants = []
    for item in assets:
        match = ASSET_NAME.match(str(item.get("name") or ""))
        if not match or match.group(1) != found.get("tag_name"):
            continue
        variant_id = variant_of(match.group(2), goos, goarch)
        if not variant_id or not VARIANT_ID.fullmatch(variant_id):
            continue
        label, help_text = variant_label(variant_id, goos)
        entry: Dict[str, Any] = {"id": variant_id, "label": label, "help": help_text, "size": int(item.get("size") or 0), "recommended": False, "_asset": item}
        if variant_id in runtimes:
            entry["_runtime"] = runtimes[variant_id]
            entry["size"] += int(runtimes[variant_id].get("size") or 0)
        variants.append(entry)
    variants.sort(key=lambda entry: entry["id"])
    recommend(variants)
    return variants


def recommend(variants: List[Dict[str, Any]]) -> None:
    """Marks the variant most likely to use this computer's GPU: CUDA with an NVIDIA driver here, else
    the plain build (CPU, or Metal on a Mac)."""
    pick = None
    if shutil.which("nvidia-smi"):
        for entry in variants:
            if entry["id"].startswith("cuda-") and (pick is None or entry["id"] > pick["id"]):
                pick = entry
    if pick is None:
        pick = next((entry for entry in reversed(variants) if entry["id"] == "cpu"), None)
    if pick is not None:
        pick["recommended"] = True


def public(variant: Dict[str, Any]) -> Dict[str, Any]:
    """A variant as the console sees it, as Go writes it (help and size left out when empty)."""
    shown = {"id": variant["id"], "label": variant["label"]}
    if variant.get("help"):
        shown["help"] = variant["help"]
    if variant.get("size"):
        shown["size"] = variant["size"]
    shown["recommended"] = bool(variant.get("recommended"))
    return shown


def latest_release() -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """The newest llama.cpp release with a build for this computer. Every build is published as a
    prerelease, so "latest" isn't one of them."""
    address = os.environ.get(RELEASES_ENV) or RELEASES_URL
    try:
        with httpx.Client(trust_env=False, timeout=30, follow_redirects=True) as http:
            response = http.get(address, headers={"Accept": "application/vnd.github+json"})
    except httpx.HTTPError as error:
        raise Refused(f"couldn't ask GitHub for llama.cpp's releases: {error}")
    if response.status_code != 200:
        raise Refused(f"GitHub answered {response.status_code} for llama.cpp's releases: {response.text[:4096].strip()}")
    try:
        releases = response.json()
    except ValueError as error:
        raise Refused(f"couldn't read llama.cpp's releases: {error}")
    for found in releases if isinstance(releases, list) else []:
        if not isinstance(found, dict) or found.get("draft"):
            continue
        variants = release_variants(found, go_os(), go_arch())
        if variants:
            return found, variants
    raise Refused(f"llama.cpp publishes no build for {go_os()}/{go_arch()}: build it from source, then put llama-server on PATH")


def install_options() -> Dict[str, Any]:
    """The "llamacpp_install" answer: how llama.cpp can be installed here."""
    if termux_prefix():
        variants = [{key: value for key, value in item.items() if key != "packages"} for item in TERMUX_VARIANTS]
        for index, variant in enumerate(variants):
            variant["recommended"] = index == 0
        return {"source": "termux", "variants": variants}
    try:
        found, variants = latest_release()
    except Refused as error:
        return {"source": "github", "variants": [], "error": str(error)}
    return {"source": "github", "release": found.get("tag_name"), "published_at": found.get("published_at") or "", "variants": [public(variant) for variant in variants]}


def start_install(variant: str) -> None:
    global _job
    if not VARIANT_ID.fullmatch(variant):
        raise Refused("pick how to install llama.cpp (cpu, vulkan, cuda-12.8, ...)")
    with _job_lock:
        if _job is not None and _job["state"] == "installing":
            raise Refused("llama.cpp is already being installed here")
        _job = {"state": "installing", "variant": variant, "release": "", "step": "starting", "done": 0, "total": 0, "error": "", "at": now()}

    def work() -> None:
        error: Optional[BaseException] = None
        try:
            if termux_prefix():
                install_termux(variant)
            else:
                install_release(variant)
        except Exception as failure:  # noqa: BLE001 - a failed install is reported, never raised into the connection
            error = failure
        if error is None:
            _set(state="installed", step="", at=now())
        else:
            _set(state="failed", error=str(error), at=now())
        log_install(f"install {variant}: {'done' if error is None else error}")

    threading.Thread(target=work, name="llamacpp-install", daemon=True).start()


def log_install(line: str) -> None:
    try:
        install_log().parent.mkdir(parents=True, exist_ok=True)
        with open(install_log(), "a", encoding="utf-8") as log:
            log.write(f"{datetime.datetime.now().astimezone().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def install_termux(variant: str) -> None:
    """Installs Termux's packages for the variant with its own apt-get."""
    packages = next((item["packages"] for item in TERMUX_VARIANTS if item["id"] == variant), None)
    if packages is None:
        raise Refused(f"Termux has no {json.dumps(variant)} build of llama.cpp: pick opencl, vulkan or cpu")

    def find_apt() -> Optional[str]:
        path = os.path.join(termux_prefix(), "bin", "apt-get")
        return path if os.path.exists(path) else None

    apt = servers.command(APT_ENV, find_apt)
    if apt is None:
        raise Refused("Termux's apt-get isn't here")
    for step in (["update"], ["install", "-y", "-o", "Dpkg::Options::=--force-confnew", *packages]):
        _set(step="apt-get " + step[0])
        error = run_logged(20 * 60, apt, step)
        if error:
            raise Refused(f"apt-get {step[0]}: {error} (see {install_log()})")
    binary = os.path.join(termux_prefix(), "bin", "llama-server")
    if not os.path.exists(binary):
        raise Refused(f"Termux's packages are in, but {binary} isn't there")
    save_install({"source": "termux", "variant": variant, "binary": binary, "installed_at": now()})


def run_logged(timeout: float, program: List[str], args: List[str]) -> Optional[str]:
    install_log().parent.mkdir(parents=True, exist_ok=True)
    with open(install_log(), "ab") as log:
        try:
            done = subprocess.run([*program, *args], stdin=subprocess.DEVNULL, stdout=log, stderr=log, timeout=timeout, check=False,
                                  env={**os.environ, "DEBIAN_FRONTEND": "noninteractive"}, **servers.QUIET)
        except subprocess.TimeoutExpired:
            return f"timed out after {timeout:g}s"
        except OSError as error:
            return str(error)
    return None if done.returncode == 0 else f"exit status {done.returncode}"


def install_release(variant: str) -> None:
    """Downloads, checks and unpacks a GitHub build into a folder of its own, then points the install
    record at its llama-server. The build before it is kept until the next install, so one still
    running keeps its files."""
    _set(step="finding the release")
    found, variants = latest_release()
    chosen = next((entry for entry in variants if entry["id"] == variant), None)
    tag = str(found.get("tag_name") or "")
    if chosen is None:
        raise Refused(f"llama.cpp {tag} has no {json.dumps(variant)} build for this computer")
    _set(release=tag, total=chosen["size"])
    install_root().mkdir(parents=True, exist_ok=True)
    final = install_root() / f"{tag}-{variant}"
    staging = Path(str(final) + ".partial")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        done = 0
        for item in (chosen["_asset"], chosen.get("_runtime")):
            if item is None:
                continue
            name = str(item.get("name"))
            _set(step="downloading " + name)
            archive = download(item, lambda count, base=done: _set(done=base + count))
            done += int(item.get("size") or 0)
            _set(step="unpacking " + name)
            try:
                unpack(archive, name, staging)
            except Refused as error:
                raise Refused(f"couldn't unpack {name}: {error}")
            except (OSError, tarfile.TarError, zipfile.BadZipFile) as error:
                raise Refused(f"couldn't unpack {name}: {error}")
            finally:
                try:
                    os.remove(archive)
                except OSError:
                    pass
        binary = find_server(staging)
        # CUDA's runtime comes in an archive of its own: it goes beside llama-server, where it's looked for.
        if chosen.get("_runtime") is not None:
            gather_libraries(staging, os.path.dirname(binary))
        relative = os.path.relpath(binary, staging)
        shutil.rmtree(final, ignore_errors=True)
        os.replace(staging, final)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    previous = load_install()
    save_install({"source": "github", "release": tag, "variant": variant, "binary": str(final / relative), "dir": str(final), "installed_at": now()})
    prune(str(final), str(previous.get("dir") or ""))


def prune(*keep: str) -> None:
    """Removes the builds unpacked here before, apart from the new one and the one before it."""
    try:
        entries = list(install_root().iterdir())
    except OSError:
        return
    for entry in entries:
        if entry.is_dir() and str(entry) not in keep:
            shutil.rmtree(entry, ignore_errors=True)


def uninstall() -> None:
    """Removes the builds the console installed; Termux's packages are Termux's to remove."""
    if installing():
        raise Refused("llama.cpp is being installed here")
    if load_install().get("source") == "termux":
        raise Refused("llama.cpp came from Termux's packages: remove them in Termux (pkg uninstall llama-cpp)")
    shutil.rmtree(install_root(), ignore_errors=True)
    if install_root().exists():
        raise Refused(f"couldn't remove {install_root()}")
    try:
        os.remove(install_record_path())
    except FileNotFoundError:
        pass


def download(item: Dict[str, Any], counted) -> str:
    """Saves an asset to a temporary file, checking its SHA-256 against GitHub's digest."""
    name = str(item.get("name"))
    digest = str(item.get("digest") or "").lower()
    want = digest[len("sha256:"):] if digest.startswith("sha256:") else ""
    if len(want) != 64:
        raise Refused(f"GitHub lists no SHA-256 for {name}, so it isn't installed")
    url = str(item.get("browser_download_url") or "")
    if not url.startswith("https://") and not os.environ.get(RELEASES_ENV):
        raise Refused(f"{name} isn't an HTTPS download")
    handle, path = tempfile.mkstemp(prefix="download-", dir=str(install_root()))
    hasher = hashlib.sha256()
    written = 0
    last = 0.0
    try:
        with os.fdopen(handle, "wb") as file, httpx.Client(trust_env=False, timeout=httpx.Timeout(60, read=300), follow_redirects=True) as http:
            with http.stream("GET", url) as response:
                if response.status_code != 200:
                    raise Refused(f"downloading {name}: GitHub answered {response.status_code}")
                for chunk in response.iter_bytes():
                    written += len(chunk)
                    if written > MAX_UNPACKED:
                        raise Refused(f"downloading {name}: it's bigger than it should be")
                    file.write(chunk)
                    hasher.update(chunk)
                    if time.monotonic() - last > 0.25:
                        last = time.monotonic()
                        counted(written)
    except httpx.HTTPError as error:
        os.remove(path)
        raise Refused(f"couldn't download {name}: {error}")
    except BaseException:
        os.remove(path)
        raise
    got = hasher.hexdigest()
    if got != want:
        os.remove(path)
        raise Refused(f"{name} isn't what GitHub lists (SHA-256 {got}, expected {want}), so it isn't installed")
    return path


def inside(dest: Path, name: str) -> Path:
    """The path an archive entry unpacks to, refusing any that would land outside dest."""
    clean = os.path.normpath(name.replace("/", os.sep))
    if os.path.isabs(clean) or clean == ".." or clean.startswith(".." + os.sep) or os.path.splitdrive(clean)[0] or name.startswith(("/", "\\")):
        raise Refused(f"{json.dumps(name)} is outside the archive")
    return dest / clean


def unpack(archive: str, name: str, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    if name.endswith(".zip"):
        unzip(archive, dest)
    else:
        untar(archive, dest)


def write_file(path: Path, source, executable: bool, room: int) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with open(path, "wb") as target:
        while True:
            chunk = source.read(1 << 20)
            if not chunk:
                break
            written += len(chunk)
            if written > room:
                raise Refused("the archive unpacks to more than it should")
            target.write(chunk)
    try:
        os.chmod(path, 0o700 if executable else 0o600)
    except OSError:
        pass
    return written


def unzip(archive: str, dest: Path) -> None:
    total = 0
    with zipfile.ZipFile(archive) as reader:
        for entry in reader.infolist():
            path = inside(dest, entry.filename)
            if entry.is_dir():
                path.mkdir(parents=True, exist_ok=True)
                continue
            mode = entry.external_attr >> 16
            # Only regular files: a zip's links and devices aren't unpacked.
            if mode and (mode & 0o170000) not in (0, 0o100000):
                continue
            with reader.open(entry) as source:
                total += write_file(path, source, bool(mode & 0o111), MAX_UNPACKED - total)


def untar(archive: str, dest: Path) -> None:
    total = 0
    with tarfile.open(archive, "r:gz") as reader:
        for entry in reader:
            path = inside(dest, entry.name)
            if entry.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif entry.isreg():
                source = reader.extractfile(entry)
                if source is not None:
                    total += write_file(path, source, bool(entry.mode & 0o111), MAX_UNPACKED - total)
            elif entry.issym():
                # Shared libraries link to their versioned names; a link may only point within the folder.
                target = entry.linkname
                if os.path.isabs(target) or target.startswith(("/", "\\")):
                    raise Refused(f"{json.dumps(entry.name)} links outside the archive")
                try:
                    inside(dest, os.path.join(os.path.dirname(entry.name), target))
                except Refused:
                    raise Refused(f"{json.dumps(entry.name)} links outside the archive")
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.remove(path)
                except OSError:
                    pass
                os.symlink(target, path)


def find_server(root: Path) -> str:
    """The llama-server in an unpacked build, made runnable."""
    for folder, _, files in sorted(os.walk(root)):
        if server_name() in files:
            found = os.path.join(folder, server_name())
            if sys.platform != "win32":
                os.chmod(found, 0o700)
            return found
    raise Refused(f"the download has no {server_name()}")


def gather_libraries(root: Path, folder: str) -> None:
    """Moves the shared libraries elsewhere in the build beside llama-server."""
    moves = []
    for here, _, files in os.walk(root):
        if os.path.normpath(here) == os.path.normpath(folder):
            continue
        for name in files:
            lower = name.lower()
            if lower.endswith(".dll") or ".so" in lower or lower.endswith(".dylib"):
                moves.append((os.path.join(here, name), os.path.join(folder, name)))
    for source, target in moves:
        if not os.path.lexists(target):
            os.replace(source, target)
