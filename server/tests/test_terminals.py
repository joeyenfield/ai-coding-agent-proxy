from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from agent_server import remote
from agent_server.app import create_app
from agent_server.config import Backend, Settings
from agent_server import terminals
from agent_server.fleet import Fleet, Machine
from agent_server.terminals import input_parts, screen_question
from oidc_helpers import authenticated_client
from oidc_helpers import database, stored_rows
from agent_server.db import schema


LOCAL = {"base_url": "http://127.0.0.1:8181", "client": ("127.0.0.1", 50000)}
# TestClient always connects WebSockets to ws://testserver; a browser or launcher here names this computer.
LOCAL_HOST = {"host": "127.0.0.1:8181"}
# The kind of key old launchers and paired phones sent; it no longer authenticates anything.
LEGACY_ACCESS_KEY = "Hb3xQ2v9mK1tW7pL0sZ4rN8cF6yJ5dA_eG-uI2oT3qE"


def settings(tmp_path: Path) -> Settings:
    return Settings(log_dir=tmp_path / "logs", default_backend="test", backends={"test": Backend("test", "http://ollama.test")})


def local_client(tmp_path: Path, app=None) -> TestClient:
    return authenticated_client(app or create_app(settings(tmp_path)), **LOCAL)


def new_session(client: TestClient) -> str:
    return client.post("/api/sessions", json={"client": "claude", "backend": "test", "project": "demo"}).json()["session_id"]


def ws_connect(client: TestClient, path: str, **headers: str):
    return client.websocket_connect(path, headers={**LOCAL_HOST, **headers})


@contextmanager
def open_host(client: TestClient, session_id: str, cols: int = 100, rows: int = 30):
    """The launcher's side: says hello, then yields the socket and the proxy's resume message."""
    with ws_connect(client, f"/api/terminals/{session_id}/host") as ws:
        ws.send_text(json.dumps({"type": "hello", "cols": cols, "rows": rows, "command": "claude"}))
        yield ws, json.loads(ws.receive_text())


def read_viewer_start(ws) -> tuple[dict, bytes]:
    state = json.loads(ws.receive_text())
    replay = b""
    while True:
        message = ws.receive()
        if message.get("bytes") is not None:
            replay += message["bytes"]
        elif json.loads(message["text"])["type"] == "replayed":
            return state, replay


def test_input_parts_send_enter_separately_and_paste_multiline_text():
    assert input_parts({"keys": "\x1b"}, False) == ["\x1b"]
    assert input_parts({"text": "fix the tests", "enter": True}, False) == ["fix the tests", "\r"]
    assert input_parts({"text": "one\r\ntwo", "enter": True}, True) == ["\x1b[200~one\ntwo\x1b[201~", "\r"]
    assert input_parts({"text": "one\ntwo"}, False) == ["one\ntwo"]
    assert input_parts({}, True) == []


def test_terminals_require_oauth_even_on_loopback_or_with_the_access_key(tmp_path):
    app = create_app(settings(tmp_path))
    with TestClient(app) as stranger:
        assert stranger.get("/api/terminals").status_code == 401
        assert "key" not in stranger.get("/api/remote").json()
    with TestClient(app, base_url="http://evil.example", client=("127.0.0.1", 50000)) as rebinding:
        # A page on another site resolving to 127.0.0.1 still names its own host.
        assert rebinding.get("/api/terminals").status_code == 401
    with local_client(tmp_path, app) as local:
        status = local.get("/api/remote").json()
        assert status["trusted"] and not status["local"] and "key" not in status
        assert local.get("/api/terminals").json() == {"terminals": []}
        key = LEGACY_ACCESS_KEY
        with TestClient(app) as launcher:
            assert launcher.get("/api/terminals", headers={remote.HEADER: key}).status_code == 401
            assert launcher.get("/api/terminals", headers={remote.HEADER: key + "x"}).status_code == 401


@pytest.mark.parametrize("path", ["/api/remote/pair", "/api/remote/code", "/api/remote/rotate"])
def test_legacy_pairing_cannot_replace_oauth(tmp_path, path):
    app = create_app(settings(tmp_path))
    with local_client(tmp_path, app) as local, TestClient(app, base_url="http://192.168.1.10:8181", client=("192.168.1.50", 40000)) as phone:
        key = LEGACY_ACCESS_KEY
        assert phone.post(path, json={"key": key}).status_code == 401
        assert phone.get("/api/terminals").status_code == 401
        response = local.post(path, json={"key": key})
        if path == "/api/remote/pair":
            assert response.status_code == 410
            local.headers.pop("x-ai-proxy-token")
            assert local.get("/api/terminals").status_code == 401
        else:
            assert response.status_code == 403


def test_authenticated_remote_status_does_not_publish_pairing_links(tmp_path):
    config = settings(tmp_path)
    config.host, config.public_url = "0.0.0.0", "http://proxy.example.net:9999"
    with local_client(tmp_path, create_app(config)) as local:
        status = local.get("/api/remote").json()
    assert status["accounts"] is True and "addresses" not in status and "key" not in status


def test_host_streams_to_viewers_and_viewer_input_reaches_the_host(tmp_path):
    with local_client(tmp_path) as client:
        session_id = new_session(client)
        with open_host(client, session_id) as (ws, resume):
            assert resume == {"type": "resume", "from": 0}
            ws.send_bytes(b"\x1b[?2004h> Welcome to Claude Code\r\n")
            listed = client.get("/api/terminals").json()["terminals"]
            assert [(item["session_id"], item["connected"], item["cols"], item["agent"], item["project"]) for item in listed] == [
                (session_id, True, 100, "claude", "demo"),
            ]
            with ws_connect(client, f"/api/terminals/{session_id}/view") as viewer:
                state, replay = read_viewer_start(viewer)
                assert state["terminal"]["connected"] is True
                assert replay == b"\x1b[?2004h> Welcome to Claude Code\r\n"
                ws.send_bytes(b"Thinking...\r\n")
                assert viewer.receive_bytes() == b"Thinking...\r\n"
                viewer.send_text(json.dumps({"type": "input", "text": "line one\nline two", "enter": True}))
                assert json.loads(ws.receive_text()) == {"type": "input", "parts": ["\x1b[200~line one\nline two\x1b[201~", "\r"]}
                ws.send_text(json.dumps({"type": "resize", "cols": 80, "rows": 24}))
                assert json.loads(viewer.receive_text()) == {"type": "resize", "cols": 80, "rows": 24}
                ws.send_text(json.dumps({"type": "exit", "status": 3}))
                ended = json.loads(viewer.receive_text())
                assert ended["type"] == "state" and ended["terminal"]["exit_status"] == 3
        db = client.app.state.runtime.db
        chunks = sorted((row["offset"], row["data"]) for row in stored_rows(db, schema.terminal_chunks) if row["session_id"] == session_id)
        assert b"".join(bytes(data) for _, data in chunks) == b"\x1b[?2004h> Welcome to Claude Code\r\nThinking...\r\n"
        [meta] = [row["meta"] for row in stored_rows(db, schema.terminals) if row["session_id"] == session_id]
        assert (meta["cols"], meta["rows"], meta["exit_status"], meta["total"]) == (80, 24, 3, 47)


def test_viewer_input_without_a_host_explains_and_unknown_sessions_close(tmp_path):
    with local_client(tmp_path) as client:
        session_id = new_session(client)
        with open_host(client, session_id) as (ws, _):
            ws.send_bytes(b"bye\r\n")
        with ws_connect(client, f"/api/terminals/{session_id}/view") as viewer:
            state, replay = read_viewer_start(viewer)
            assert state["terminal"]["connected"] is False and replay == b"bye\r\n"
            viewer.send_text(json.dumps({"type": "input", "keys": "y"}))
            assert json.loads(viewer.receive_text()) == {"type": "error", "message": "The agent's terminal isn't connected."}
        other = new_session(client)
        with ws_connect(client, f"/api/terminals/{other}/view") as viewer:
            with pytest.raises(WebSocketDisconnect) as closed:
                viewer.receive_text()
            assert closed.value.code == 4404


def test_websockets_from_other_sites_are_refused(tmp_path):
    with local_client(tmp_path) as client:
        session_id = new_session(client)
        for path in (f"/api/terminals/{session_id}/host", f"/api/terminals/{session_id}/view"):
            with pytest.raises(WebSocketDisconnect) as closed:
                with ws_connect(client, path, origin="http://evil.example"):
                    pytest.fail("A foreign origin completed the WebSocket handshake")
            assert closed.value.code == 4403


def test_reconnecting_host_resumes_and_history_survives_a_restart(tmp_path):
    with local_client(tmp_path) as client:
        session_id = new_session(client)
        with open_host(client, session_id) as (ws, _):
            ws.send_bytes(b"first\r\n")
        with open_host(client, session_id) as (ws, resume):
            assert resume["from"] == 7
            # The launcher dropped bytes 7-9 from its own history: it says so, then carries on.
            ws.send_text(json.dumps({"type": "gap", "from": 10}))
            ws.send_bytes(b"second\r\n")
        client.patch(f"/api/sessions/{session_id}", json={"ended": True, "exit_status": 0})
    with local_client(tmp_path) as restarted:
        listed = restarted.get("/api/terminals").json()["terminals"]
        assert [(item["session_id"], item["connected"], item["exit_status"], item["bytes"]) for item in listed] == [(session_id, False, 0, 18)]
        with ws_connect(restarted, f"/api/terminals/{session_id}/view") as viewer:
            state, replay = read_viewer_start(viewer)
            assert replay == b"first\r\nsecond\r\n"
            assert state["terminal"]["ended_at"] is not None
        with open_host(restarted, session_id) as (ws, resume):
            assert resume["from"] == 18
        restarted.delete(f"/api/sessions/{session_id}")
        assert restarted.get("/api/terminals").json() == {"terminals": []}


def test_session_ids_cannot_leave_the_sessions_folder(tmp_path):
    with local_client(tmp_path) as client:
        for bad in ("..", ".hidden", "a/b", "a\\b", ""):
            response = client.post("/api/sessions", json={"client": "x", "backend": "test", "session_id": bad})
            assert response.status_code == (201 if bad == "" else 400), bad
        assert client.post("/api/sessions", json={"client": "x", "backend": "test", "session_id": "bench-2026.10_1"}).status_code == 201


PERMISSION_PROMPT = (
    "\x1b[32m⏺\x1b[0m Retries are in.\r\n\r\n"
    "\x1b[34m╭──╮\x1b[0m\r\n"
    "\x1b[34m│\x1b[0m \x1b[1mEdit file\x1b[0m tests/test_client.py\r\n"
    "\x1b[34m│\x1b[0m Do you want to make this edit to test_client.py?   \x1b[34m│\x1b[0m\r\n"
    "\x1b[34m│\x1b[0m \x1b[36m❯ 1. Yes\x1b[0m\r\n"
    "\x1b[34m│\x1b[0m   2. No, and tell Claude what to do differently\r\n"
).encode()


def test_a_question_at_the_end_of_the_screen_is_read_as_text():
    assert screen_question(PERMISSION_PROMPT) == "Do you want to make this edit to test_client.py?"
    assert screen_question(b"Apply these changes? (y/n) ") == "Apply these changes? (y/n)"
    assert screen_question(b"\x1b[2m>\x1b[0m \x1b[7m \x1b[0m\r\n  ? for shortcuts\r\n") is None
    # Answered and scrolled away: the agent's later output is what's on screen now.
    assert screen_question(PERMISSION_PROMPT + b"".join(b"Updated line %d\r\n" % n for n in range(20))) is None


def test_running_agents_say_their_machine_and_when_they_wait_for_you(tmp_path, monkeypatch):
    monkeypatch.setattr(terminals, "QUIET_SECONDS", 0.0)
    app = create_app(settings(tmp_path))
    with local_client(tmp_path, app) as client:
        fleet = app.state.runtime.fleet
        fleet.machines["m1"] = Machine(id="m1", name="Build box", hostname="BUILD-01")
        by_hand = client.post("/api/sessions", json={"client": "claude", "backend": "test", "tags": {"hostname": "build-01", "machine_name": "build-01"}}).json()["session_id"]
        elsewhere = client.post("/api/sessions", json={"client": "claude", "backend": "test", "tags": {"machine_name": "laptop"}}).json()["session_id"]
        with open_host(client, by_hand) as (ws, _), open_host(client, elsewhere) as (other, _):
            ws.send_bytes(PERMISSION_PROMPT)
            other.send_bytes(b"> ")
            # The sockets' output lands on the server's loop a moment later.
            for _ in range(100):
                listed = {item["session_id"]: item for item in client.get("/api/terminals").json()["terminals"]}
                if all(item["bytes"] for item in listed.values()):
                    break
                time.sleep(0.02)
            assert (listed[by_hand]["machine"], listed[by_hand]["machine_name"]) == ("m1", "Build box")
            assert listed[by_hand]["attention"]["kind"] == "question"
            assert listed[by_hand]["attention"]["question"] == "Do you want to make this edit to test_client.py?"
            assert (listed[elsewhere]["machine"], listed[elsewhere]["machine_name"]) == (None, "laptop")
            assert listed[elsewhere]["attention"]["kind"] == "idle"
            sessions = {item["session_id"]: item["machine"] for item in client.get("/api/sessions").json()}
            assert sessions[by_hand] == "m1" and sessions[elsewhere] is None
            # While a model request is in flight it's working, not waiting.
            app.state.runtime.active["x"] = {"session_id": by_hand}
            assert next(item for item in client.get("/api/terminals").json()["terminals"] if item["session_id"] == by_hand)["attention"] is None
            del app.state.runtime.active["x"]
        # Its launcher went: no one to answer it.
        assert all(item["attention"] is None for item in client.get("/api/terminals").json()["terminals"])


def test_sessions_only_join_a_machine_of_their_own_owner(tmp_path):
    fleet = Fleet(database(tmp_path))
    fleet.machines["mine"] = Machine(id="mine", name="desk", owner="joey")
    fleet.machines["theirs"] = Machine(id="theirs", name="desk", owner="sam", hostname="desk")
    assert fleet.machine_for({"machine": "mine", "owner": "joey"}).id == "mine"
    assert fleet.machine_for({"machine": "theirs", "owner": "joey"}) is None
    assert fleet.machine_for({"hostname": "DESK", "owner": "joey"}).id == "mine"
    assert fleet.machine_for({"hostname": "desk", "owner": "sam"}).id == "theirs"
    assert fleet.machine_for({"hostname": "other"}) is None
    assert fleet.machine_for({}) is None
