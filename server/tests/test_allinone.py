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


def test_it_copies_only_what_exists_and_is_published_with_the_others():
    for line in re.findall(r"^COPY (?!--from)(.+)$", ALLINONE, re.MULTILINE):
        for source in line.split()[:-1]:
            assert (ROOT / source).exists(), source
    workflow = (ROOT / ".github" / "workflows" / "images.yml").read_text(encoding="utf-8")
    assert "{ name: ai-control-mesh, dockerfile: devhelpers/allinone/Dockerfile }" in workflow
    assert "ai-control-mesh-site, ai-control-mesh]" in workflow
    # One image's name starts another's (ai-control-mesh, ai-control-mesh-site): the merge job must
    # take only its own platforms' digests, so their artifacts end the name with a character names lack.
    assert "name: digests_${{ matrix.image.name }}_" in workflow and "pattern: digests_${{ matrix.image }}_*" in workflow


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
