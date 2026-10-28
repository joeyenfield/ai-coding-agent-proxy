"""The documentation the console's Docs page shows: docs/ served at /help-files/, public, and nothing else."""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent_server.app import create_app
from agent_server.auth import UserStore
from agent_server.config import Backend, Settings


def help_app(tmp_path, accounts: bool = False):
    docs = tmp_path / "docs"
    (docs / "screenshots").mkdir(parents=True)
    (docs / "index.md").write_text("# Hello\n\nSee [Agents](agents.md).\n", encoding="utf-8")
    (docs / "screenshots" / "overview.png").write_bytes(b"\x89PNG fake")
    (docs / "notes.txt").write_text("not a page", encoding="utf-8")
    (tmp_path / "secret.md").write_text("outside docs", encoding="utf-8")
    settings = Settings(
        log_dir=tmp_path / "logs", backends_file=tmp_path / "backends.yaml", docs_dir=docs,
        default_backend="t", backends={"t": Backend("t", "http://ollama.test")},
    )
    if accounts:
        UserStore(settings.users_path).add_user("dave", "correct horse")
    return create_app(settings)


def test_the_docs_pages_and_pictures_are_served(tmp_path):
    with TestClient(help_app(tmp_path)) as client:
        page = client.get("/help-files/index.md")
        assert page.status_code == 200 and page.text.startswith("# Hello") and page.headers["content-type"].startswith("text/markdown")
        picture = client.get("/help-files/screenshots/overview.png")
        assert picture.status_code == 200 and picture.headers["content-type"] == "image/png"
        # Only pages and pictures, and only inside docs/.
        for path in ("/help-files/notes.txt", "/help-files/missing.md", "/help-files/%2e%2e/secret.md", "/help-files/..%2fsecret.md"):
            assert client.get(path).status_code == 404, path
        # A plain ".." is resolved by the client before it's sent, and lands on the console, never the file.
        assert "outside docs" not in client.get("/help-files/../secret.md").text
        # The console's own page is a client-side route.
        assert client.get("/help/agents").status_code in (200, 404)


def test_the_docs_need_no_sign_in(tmp_path):
    with TestClient(help_app(tmp_path, accounts=True)) as client:
        assert client.get("/api/status").status_code == 401
        assert client.get("/help-files/index.md").status_code == 200
