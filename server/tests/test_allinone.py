"""deploy/compose/allinone/: the image with the whole system in one container, checked against what it's made from."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ALLINONE = (ROOT / "devhelpers" / "allinone" / "Dockerfile").read_text(encoding="utf-8")
SANDBOX = (ROOT / "sandbox" / "Dockerfile").read_text(encoding="utf-8")


def argument(dockerfile: str, name: str) -> str:
    match = re.search(rf"^ARG {name}=(.*)$", dockerfile, re.MULTILINE)
    assert match, name
    return match.group(1).strip()


def bases(dockerfile: str) -> list[str]:
    return re.findall(r"^FROM (\S+)", dockerfile, re.MULTILINE)


def test_its_machine_is_built_as_the_sandboxs_is():
    # The Go programs, Ollama and the agents come from the same stages as agent-sandbox's.
    for name in ("OLLAMA_VERSION", "AGENTS", "PACKAGES", "EXTRA_PACKAGES", "AGENT_SUDO"):
        assert argument(ALLINONE, name) == argument(SANDBOX, name)
    for base in bases(SANDBOX):
        assert base in bases(ALLINONE)


def test_a_gpu_is_only_in_the_image_and_the_files_that_ask_for_it():
    import yaml

    def mesh(tree: str, name: str) -> dict:
        return yaml.safe_load((ROOT / tree / "allinone" / name).read_text(encoding="utf-8"))["services"]["mesh"]

    # The plain image keeps no GPU backend (they are most of Ollama's size), and runs without a GPU.
    assert argument(ALLINONE, "OLLAMA_GPU") == ""
    for tree in ("devhelpers", "deploy/compose"):
        assert "devices" not in mesh(tree, "compose.yaml")
        # Podman ignores Compose's deploy.resources device reservations; a CDI device name is what it honours.
        assert mesh(tree, "compose.gpu.yaml")["devices"] == ["nvidia.com/gpu=all"]
    # The GPU image is the same Dockerfile with the CUDA libraries kept, under its own name, so
    # neither build replaces the other: the local release catalog keeps their names and arguments separate.
    catalog = (ROOT / "devhelpers/release_scripts/release/local.py").read_text(encoding="utf-8")
    assert 'Image("ai-control-mesh-gpu", "devhelpers/allinone/Dockerfile", ("OLLAMA_GPU=cuda_v13",))' in catalog
    published = mesh("deploy/compose", "compose.gpu.yaml")
    assert published == {"image": "${AI_PROXY_IMAGES:-ghcr.io/joeyenfield}/ai-control-mesh-gpu:${AI_PROXY_IMAGE_TAG:-latest}",
                         "devices": ["nvidia.com/gpu=all"]}
    built = mesh("devhelpers", "compose.gpu.yaml")
    assert built["build"]["args"]["OLLAMA_GPU"] == "${OLLAMA_GPU:-cuda_v13}"
    assert built["image"] == "localhost/ai-control-mesh-dev-gpu"


def test_it_copies_only_what_exists_and_is_published_with_the_others():
    for line in re.findall(r"^COPY (?!--from)(.+)$", ALLINONE, re.MULTILINE):
        for source in line.split()[:-1]:
            assert (ROOT / source).exists(), source
    catalog = (ROOT / "devhelpers/release_scripts/release/local.py").read_text(encoding="utf-8")
    assert 'Image("ai-control-mesh", "devhelpers/allinone/Dockerfile")' in catalog
    assert 'Image("ai-control-mesh-site", "site/Dockerfile")' in catalog


def test_the_entrypoint_signs_in_with_the_quickstarts_device_flow_at_the_local_ports():
    from agent_server import local

    tree = ast.parse((ROOT / "devhelpers" / "allinone" / "entrypoint.py").read_text(encoding="utf-8"))
    imported = {(node.module, alias.name) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert ("quickstart_machine", "sign_in") in imported and ("agent_server", "local") in imported
    # deploy/compose/quickstart/machine.py signs in at fixed addresses: the local instance's defaults, which the image keeps.
    machine = (ROOT / "deploy" / "compose" / "quickstart" / "machine.py").read_text(encoding="utf-8")
    assert f'API = os.getenv("QUICKSTART_API") or "http://localhost:{local.DEFAULT_PORT}"' in machine
    assert f'ISSUER = os.getenv("QUICKSTART_ISSUER") or "http://localhost:{local.DEFAULT_AUTH_PORT}"' in machine
    assert "def sign_in(path, password):" in machine
    assert f"EXPOSE {local.DEFAULT_PORT} {local.DEFAULT_AUTH_PORT}" in ALLINONE


def test_the_containers_machine_gets_a_name_people_can_read(monkeypatch):
    import os
    import re
    import socket

    # The entrypoint imports modules that only exist in the image, so take the one function.
    tree = ast.parse((ROOT / "devhelpers" / "allinone" / "entrypoint.py").read_text(encoding="utf-8"))
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "machine_name")
    scope = {"os": os, "re": re, "socket": socket}
    exec(compile(ast.Module(body=[function], type_ignores=[]), "entrypoint.py", "exec"), scope)
    name = scope["machine_name"]
    monkeypatch.delenv("MESH_MACHINE_NAME", raising=False)
    monkeypatch.setattr(socket, "gethostname", lambda: "3f9a1c0b7d2e")
    assert name() == "my-machine"  # a container id says nothing
    monkeypatch.setattr(socket, "gethostname", lambda: "build-box")
    assert name() == "build-box"  # docker run --hostname
    monkeypatch.setenv("MESH_MACHINE_NAME", "demo")
    assert name() == "demo"
