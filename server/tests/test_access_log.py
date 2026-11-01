from __future__ import annotations

import time

import pytest
from starlette.websockets import WebSocketDisconnect

from agent_server.access_log import AccessLog, outcome
from agent_server.app import create_app
from agent_server.db import schema
from test_auth import BASE, HOST, org_settings, signed_in, ws_headers


def test_site_admins_see_every_request_and_how_it_ended(tmp_path):
    app = create_app(org_settings(tmp_path))
    sally = signed_in(app, "sally")  # a site admin
    with sally:
        # Refused before any route: no sign-in at all.
        anonymous = signed_in(app, "alice")
        anonymous.cookies.clear()
        assert anonymous.get("/api/sessions?code=secret-oauth-code&state=x").status_code == 401
        assert sally.get("/api/status").status_code == 200
        # A machine with no sign-in: the fleet socket is closed with its reason.
        with pytest.raises(WebSocketDisconnect):
            with anonymous.websocket_connect("/api/machines/connect", headers=HOST) as ws:
                ws.receive_text()

        # Organization admins and members don't run the server.
        assert signed_in(app, "dave").get("/api/server/access-log").status_code == 403
        assert signed_in(app, "alice").get("/api/server/access-log").status_code == 403

        log = sally.get("/api/server/access-log").json()
        entries = log["entries"]
        refused = next(entry for entry in entries if entry["path"] == "/api/sessions")
        assert refused["status"] == 401 and refused["outcome"] == "failed" and "Sign in first" in refused["detail"]
        # Only the query's names are kept: an OAuth code must not end up here.
        assert refused["query"] == "code&state" and "secret" not in str(log)
        ok = next(entry for entry in entries if entry["path"] == "/api/status")
        assert ok["status"] == 200 and ok["outcome"] == "ok" and ok["user"] == "sally" and ok["duration_ms"] is not None
        socket = next(entry for entry in entries if entry["path"] == "/api/machines/connect")
        assert socket["kind"] == "websocket" and socket["outcome"] == "failed"
        assert socket["close_code"] == 4401 and socket["closed_by"] == "server"
        # The refusals for dave and alice above, but never the log's own requests.
        assert all(entry["path"] != "/api/server/access-log" for entry in entries)

        failed = sally.get("/api/server/access-log", params={"failed": "true"}).json()["entries"]
        assert {entry["path"] for entry in failed} == {"/api/sessions", "/api/machines/connect"}
        assert [entry["path"] for entry in sally.get("/api/server/access-log", params={"q": "4401"}).json()["entries"]] == ["/api/machines/connect"]
        # Paging back by id.
        newest = entries[0]["id"]
        assert all(entry["id"] < newest for entry in sally.get("/api/server/access-log", params={"before": newest}).json()["entries"])


def test_websocket_outcomes():
    def entry(**values):
        return {"kind": "websocket", "status": 101, "close_code": None, "closed_by": None, **values}

    assert outcome(entry(close_code=1000, closed_by="client")) == "ok"
    assert outcome(entry(close_code=1006, closed_by="client")) == "dropped"
    assert outcome(entry(close_code=4403, closed_by="server")) == "failed"
    assert outcome(entry(status=403, close_code=1000, closed_by="server")) == "failed"
    assert outcome({"kind": "http", "status": 302}) == "ok"
    assert outcome({"kind": "http", "status": 502}) == "failed"


def test_failures_outlast_busy_traffic():
    log = AccessLog(limit=3, failed_limit=3)
    scope = {"type": "http", "path": "/api/machines/connect", "headers": []}
    failure = log.start(scope)
    failure["status"] = 401
    log.finish(failure, scope)
    for _ in range(5):
        ok = log.start({**scope, "path": "/api/status"})
        ok["status"] = 200
        log.finish(ok, scope)
    assert all(item["path"] == "/api/status" for item in log.query()["entries"])
    assert [item["status"] for item in log.query(failed=True)["entries"]] == [401]
    assert log.query()["totals"] == {"ok": 5, "failed": 1, "dropped": 0, "open_websockets": 0}


def test_failures_and_fleet_connections_are_saved_and_outlast_a_restart(tmp_path):
    settings = org_settings(tmp_path)
    app = create_app(settings)
    sally = signed_in(app, "sally")
    with sally:
        assert sally.get("/api/status").status_code == 200
        anonymous = signed_in(app, "alice")
        anonymous.cookies.clear()
        assert anonymous.get("/api/sessions").status_code == 401
        with pytest.raises(WebSocketDisconnect):
            with anonymous.websocket_connect("/api/machines/connect", headers=HOST) as ws:
                ws.receive_text()

    # A new start against the same database: memory is empty, the saved part is still there.
    app = create_app(settings)
    sally = signed_in(app, "sally")
    with sally:
        assert sally.get("/api/server/access-log", params={"failed": "true"}).json()["entries"] == []
        history = sally.get("/api/server/access-log/history").json()
        paths = [entry["path"] for entry in history["entries"]]
        # The console's successful calls aren't saved; the refusal and the fleet connection are.
        assert sorted(paths) == ["/api/machines/connect", "/api/sessions"] and history["kept_days"] == 30
        assert all(entry["saved"] and isinstance(entry["id"], str) for entry in history["entries"])
        fleet = sally.get("/api/server/access-log/history", params={"fleet": "true"}).json()["entries"]
        assert [entry["close_code"] for entry in fleet] == [4401]
        assert [entry["path"] for entry in sally.get("/api/server/access-log/history", params={"q": "sessions 401"}).json()["entries"]] == ["/api/sessions"]
        oldest = min(entry["time"] for entry in history["entries"])
        assert sally.get("/api/server/access-log/history", params={"before": oldest}).json()["entries"] == []

        stats = sally.get("/api/server/access-log/stats").json()
        assert len(stats["per_minute"]) == 60 and stats["totals"]["open_websockets"] == 0
        saved = stats["saved"]
        assert saved["count"] == 2 and sum(day["failed"] for day in saved["days"]) == 2 and len(saved["days"]) == 14
        assert [(item["address"], item["connections"], item["failed"], item["last_answer"]) for item in saved["fleet"]] == [("testclient", 1, 1, "refused 403, 4401")]
        assert signed_in(app, "dave").get("/api/server/access-log/stats").status_code == 403
        assert signed_in(app, "dave").get("/api/server/access-log/history").status_code == 403

        # Kept for the retention setting, or 30 days without one.
        log = app.state.runtime.access_log
        log.db.writer.flush()
        assert log.prune() == 0
        with log.db.write() as connection:
            connection.execute(schema.access_log.update().values(time=time.time() - 31 * 86400))
        assert log.prune() == 2


def test_stats_count_routes_and_open_sockets_beyond_the_window():
    log = AccessLog(limit=2)
    socket_scope = {"type": "websocket", "path": "/api/machines/connect", "headers": [], "client": ("10.0.0.5", 1)}
    socket = log.start(socket_scope)
    socket["status"] = 101
    for number in range(3):
        scope = {"type": "http", "path": f"/session/{number}abcdef0123456789abcdef/v1/chat/completions", "headers": [], "client": ("10.0.0.9", 1)}
        entry = log.start(scope)
        entry["status"] = 404
        log.finish(entry, scope)
    # The fleet connection fell out of the newest two but is still open.
    assert socket not in log.entries and log.query()["totals"]["open_websockets"] == 1
    stats = log.stats()
    assert [(item["route"], item["count"]) for item in stats["failing_routes"]] == [("/session/{id}/v1/chat/completions", 3)]
    assert stats["failing_clients"][0]["address"] == "10.0.0.9" and stats["open_websockets"][0]["address"] == "10.0.0.5"
    assert sum(minute["failed"] for minute in stats["per_minute"]) == 3 and stats["durations"]["count"] == 3
    socket["close_code"], socket["closed_by"] = 1006, "client"
    log.finish(socket, socket_scope)
    assert log.query()["totals"] == {"ok": 0, "failed": 3, "dropped": 1, "open_websockets": 0}


def test_a_fleet_connection_is_known_by_its_machine(tmp_path):
    from test_fleet import machine

    app = create_app(org_settings(tmp_path))
    alice, sally = signed_in(app, "alice"), signed_in(app, "sally")
    with alice, sally:
        with machine(alice):
            [open_socket] = [item for item in sally.get("/api/server/access-log/stats").json()["open_websockets"] if item["path"] == "/api/machines/connect"]
            assert (open_socket["machine_id"], open_socket["machine_name"], open_socket["user"]) == ("m1", "laptop", "alice")
        # Closed normally by the machine: saved, and counted under its machine.
        [item] = sally.get("/api/server/access-log/stats").json()["saved"]["fleet"]
        assert (item["machine_id"], item["machine_name"], item["connections"], item["ok"]) == ("m1", "laptop", 1, 1)
        [saved] = sally.get("/api/server/access-log/history", params={"q": "laptop"}).json()["entries"]
        assert saved["outcome"] == "ok" and saved["user"] == "alice"
