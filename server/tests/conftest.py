"""Keep tests away from the real configuration and secrets.

Settings() built in code still points at the repository's config/ files unless a
test passes its own (backends_file=tmp_path / "backends.yaml" and so on). This
fails any test that changes those files or creates secrets in certs/, and puts
the files back as they were.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_server.config import ROOT


GUARDED = [ROOT / "config" / name for name in ("backends.yaml", "models.yaml", "agents.yaml")]
SECRETS = [ROOT / "certs" / name for name in ("remote-access-key", "container-keys.json", "users.json", "first-user-password")]


@pytest.fixture(autouse=True)
def real_config_untouched():
    before = {path: path.read_bytes() if path.exists() else None for path in GUARDED}
    secrets = {path: path.exists() for path in SECRETS}
    yield
    changed = [path for path, content in before.items() if (path.read_bytes() if path.exists() else None) != content]
    for path in changed:
        if before[path] is None:
            path.unlink()
        else:
            path.write_bytes(before[path])
    created = [path for path, existed in secrets.items() if not existed and path.exists()]
    for path in created:
        path.unlink()
    assert not changed, f"The test changed the real {', '.join(str(Path(item).relative_to(ROOT)) for item in changed)}; give Settings a tmp_path file"
    assert not created, f"The test created {', '.join(str(Path(item).relative_to(ROOT)) for item in created)}; give Settings a tmp_path key file"
