"""The request log: every request saved for its days, payloads only in debug mode, secrets never."""

from __future__ import annotations

import time

from agent_server import config_io
from agent_server.app import create_app
from agent_server.db import schema
from agent_server.request_log import body, redact, redact_query
from test_auth import org_settings, signed_in


def storage(client, **request_log):
    response = client.put("/api/config/storage", json={"request_log": request_log})
    assert response.status_code == 200, response.text
    return response.json()["request_log"]


def test_nothing_is_saved_until_the_request_log_is_turned_on(tmp_path):
    app = create_app(org_settings(tmp_path))
    sally = signed_in(app, "sally")
    with sally:
        assert sally.get("/api/status").status_code == 200
        summary = sally.get("/api/server/request-log/summary").json()
        assert summary["settings"] == {"enabled": False, "days": 7, "payloads": False, "payload_days": 1}
        assert summary["count"] == 0 and sally.get("/api/server/request-log").json()["entries"] == []
        # Debug mode alone saves nothing either: it's part of the log.
        storage(sally, payloads=True)
        sally.get("/api/status")
        assert sally.get("/api/server/request-log/summary").json()["count"] == 0


def test_every_request_is_saved_for_site_admins_without_payloads_by_default(tmp_path):
    settings = org_settings(tmp_path)
    app = create_app(settings)
    sally = signed_in(app, "sally")  # a site admin
    with sally:
        assert storage(sally, enabled=True)["enabled"] is True
        assert sally.get("/api/status").status_code == 200
        anonymous = signed_in(app, "alice")
        anonymous.cookies.clear()
        assert anonymous.get("/api/sessions?code=secret-oauth-code").status_code == 401
        assert signed_in(app, "dave").get("/api/sessions").status_code == 200

        # Organization admins and members don't run the server.
        assert signed_in(app, "dave").get("/api/server/request-log").status_code == 403
        assert signed_in(app, "alice").get("/api/server/request-log/summary").status_code == 403

        log = sally.get("/api/server/request-log").json()
        assert log["settings"] == {"enabled": True, "days": 7, "payloads": False, "payload_days": 1}
        entries = log["entries"]
        # Successful console calls too (the access log saves only failures), but not the log's own.
        assert {(entry["path"], entry["status"], entry["user"]) for entry in entries} >= {
            ("/api/status", 200, "sally"), ("/api/sessions", 401, None), ("/api/sessions", 200, "dave"),
        }
        assert all(not entry["path"].startswith("/api/server/request-log") for entry in entries)
        assert all(not entry["payload"] for entry in entries) and "secret-oauth-code" not in str(log)

        # Filters: status class, user, method, words; paging by time.
        failed = sally.get("/api/server/request-log", params={"status": "4xx"}).json()["entries"]
        assert {entry["status"] for entry in failed} == {401, 403} or {entry["status"] for entry in failed} == {401}
        assert {entry["user"] for entry in sally.get("/api/server/request-log", params={"user": "dave"}).json()["entries"]} == {"dave"}
        assert sally.get("/api/server/request-log", params={"method": "post"}).json()["entries"] == []
        assert [entry["path"] for entry in sally.get("/api/server/request-log", params={"q": "status sally"}).json()["entries"]] == ["/api/status"]
        assert sally.get("/api/server/request-log", params={"status": "teapot"}).status_code == 400
        oldest = min(entry["time"] for entry in entries)
        assert sally.get("/api/server/request-log", params={"before": oldest}).json()["entries"] == []

        one = sally.get(f"/api/server/request-log/{entries[0]['id']}").json()
        assert one["id"] == entries[0]["id"] and one["payloads"] is None
        assert sally.get("/api/server/request-log/nope").status_code == 404
        summary = sally.get("/api/server/request-log/summary").json()
        assert summary["count"] >= 3 and summary["payloads"] == 0


def test_debug_mode_saves_payloads_with_secrets_redacted(tmp_path):
    app = create_app(org_settings(tmp_path))
    sally = signed_in(app, "sally")
    with sally:
        assert storage(sally, enabled=True, payloads=True, payload_days=2) == {"enabled": True, "days": 7, "payloads": True, "payload_days": 2}
        created = sally.post("/api/users", json={"name": "newbie", "password": "hunter22-secret", "tenant": "acme"},
                             headers={"X-Api-Key": "not-for-the-log"})
        assert created.status_code in (200, 201), created.text
        # Sign-in is never kept, whatever debug mode says.
        sally.get("/api/auth/status")

        entries = sally.get("/api/server/request-log", params={"method": "POST", "q": "/api/users"}).json()["entries"]
        assert len(entries) == 1 and entries[0]["payload"]
        saved = sally.get(f"/api/server/request-log/{entries[0]['id']}").json()["payloads"]
        assert saved["request"]["body"]["json"] == {"name": "newbie", "password": "[redacted]", "tenant": "acme"}
        assert saved["request"]["headers"]["x-api-key"] == saved["request"]["headers"]["cookie"] == "[redacted]"
        assert saved["response"]["body"]["json"]["users"]
        token = sally.cookies.get("ai_proxy_session")
        assert "hunter22-secret" not in str(saved) and "not-for-the-log" not in str(saved) and token not in str(saved)
        status = sally.get("/api/server/request-log", params={"q": "/api/auth/status"}).json()["entries"]
        assert status and not status[0]["payload"]

        # Turned off: nothing more is saved, and what was saved stays until its days pass.
        assert storage(sally, enabled=False)["enabled"] is False
        before = sally.get("/api/server/request-log/summary").json()["count"]
        sally.get("/api/status")
        assert sally.get("/api/server/request-log/summary").json()["count"] == before

        # Requests after days, payloads after payload_days.
        log = app.state.runtime.request_log
        with log.db.write() as connection:
            connection.execute(schema.request_log_payloads.update().values(time=time.time() - 3 * 86400))
        log.prune()
        assert sally.get(f"/api/server/request-log/{entries[0]['id']}").json()["payloads"] is None
        with log.db.write() as connection:
            connection.execute(schema.request_log.update().values(time=time.time() - 8 * 86400))
        assert log.prune() == before
        assert sally.get("/api/server/request-log/summary").json()["count"] == 0


def test_request_log_settings_are_checked_and_carried_by_config_export(tmp_path):
    settings = org_settings(tmp_path)
    app = create_app(settings)
    sally = signed_in(app, "sally")
    with sally:
        for bad in ({"days": 0}, {"days": "many"}, {"payloads": "yes"}, {"payload_days": 400}, {"colour": "blue"}):
            assert sally.put("/api/config/storage", json={"request_log": bad}).status_code == 400, bad
        assert sally.put("/api/config/storage", json={}).status_code == 400
        assert signed_in(app, "dave").put("/api/config/storage", json={"request_log": {"days": 2}}).status_code == 403
        assert storage(sally, days=30)["days"] == 30
        assert sally.get("/api/config/storage").json()["request_log"]["days"] == 30
        # retention_days alone leaves the request log as it is.
        assert sally.put("/api/config/storage", json={"retention_days": 10}).json()["request_log"]["days"] == 30

    document = config_io.export(settings)
    assert document["request_log"] == {"enabled": False, "days": 30, "payloads": False, "payload_days": 1}
    assert config_io.apply(settings, {"request_log": {"payloads": True}}) == ["request_log"]
    assert settings.request_log["payloads"] is True and settings.request_log["days"] == 7


def test_redaction():
    assert redact({"user": "a", "Password": "x", "nested": [{"access_token": "t", "keep": 1}], "api_key": ""}) == {
        "user": "a", "Password": "[redacted]", "nested": [{"access_token": "[redacted]", "keep": 1}], "api_key": "",
    }
    assert redact_query("code=abc&state=1&model=qwen") == "code=%5Bredacted%5D&state=1&model=qwen"
    assert body(b"name=a&password=b", 17, "application/x-www-form-urlencoded")["text"] == "name=a&password=%5Bredacted%5D"
    cut = body(b'{"messages": [], "refresh_token": "abc", "x', 10**6, "application/json")
    assert cut["truncated"] and '"refresh_token": "[redacted]"' in cut["text"] and "abc" not in cut["text"]
    assert body(b"\x89PNG....", 8, "image/png") == {"bytes": 8, "truncated": False, "omitted": "image/png body"}
