"""Retention policies, legal holds, the audit log, and blob storage for recordings."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from agent_server import blobs
from agent_server.app import create_app
from agent_server.audit import Audit, otlp, validate_config
from agent_server.config import Settings
from agent_server.db import schema
from agent_server.retention import Retention, RetentionError, validate_org, validate_server
from agent_server.sessions import SessionStore
from oidc_helpers import access_token, database
from test_auth import BASE, HOST


def ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# Policies

def test_the_server_policy_bounds_what_organizations_set():
    server = validate_server({"days": {"traces": 30, "sessions": 400}, "max": {"traces": 90}, "min": {"records": 30}, "allowed_traces": ["off", "opt_in"]})
    assert server["days"]["audit"] == 365 and server["min"]["audit"] == 90
    with pytest.raises(RetentionError):
        validate_server({"days": {"traces": 120}, "max": {"traces": 90}})
    with pytest.raises(RetentionError):
        validate_server({"capture": {"traces": "always"}, "allowed_traces": ["opt_in"]})
    assert validate_org({"days": {"traces": 7}, "capture": {"traces": "off"}}, server) == {"days": {"traces": 7}, "capture": {"traces": "off"}}
    for bad in ({"days": {"traces": 200}}, {"days": {"traces": None}}, {"days": {"records": 3}}, {"capture": {"traces": "always"}}):
        with pytest.raises(RetentionError):
            validate_org(bad, server)


def test_a_session_takes_its_organizations_policy_when_it_starts(tmp_path):
    settings = Settings(log_dir=tmp_path / "logs")
    db = settings.open_database()
    sessions = SessionStore(db)
    retention = Retention(db, settings, sessions)
    sessions.stamp, sessions.trace_check = retention.stamp, retention.check_trace
    retention.set_server({"days": {"traces": 30}}, "admin")
    retention.set_org("acme", {"days": {"traces": 7}, "capture": {"traces": "always"}}, "dave")
    session = sessions.create({"client": "qwen", "backend": "x", "trace": False, "tags": {"tenant": "acme"}})
    assert session.trace is True and session.tags["retention"]["days"]["traces"] == 7
    with pytest.raises(ValueError):
        sessions.set_trace(session, False)
    # A later change to the policy doesn't change what the session started with.
    retention.set_org("acme", {"days": {"traces": 14}}, "dave")
    assert session.tags["retention"]["days"]["traces"] == 7
    other = sessions.create({"client": "qwen", "backend": "x", "trace": True, "tags": {"tenant": "globex"}})
    assert other.tags["retention"]["days"]["traces"] == 30 and other.trace is True


def test_retention_deletes_by_each_sessions_policy_and_never_what_is_held(tmp_path):
    settings = Settings(log_dir=tmp_path / "logs")
    db = settings.open_database()
    sessions = SessionStore(db)
    retention = Retention(db, settings, sessions)
    sessions.stamp = retention.stamp
    retention.set_server({"days": {"traces": 10, "sessions": 100}}, "admin")
    made = {}
    for name, tenant, owner in (("old", "acme", "bob"), ("held", "acme", "carol"), ("ancient", "globex", "x"), ("new", "acme", "bob")):
        session = sessions.create({"client": "qwen", "backend": "x", "trace": True, "tags": {"tenant": tenant, "owner": owner}})
        sessions.append_request(session, {"request_id": "000001", "timestamp": ago(1), "model": "m"})
        sessions.write_trace(session, "000001", {"request": {"messages": []}})
        made[name] = session
    for name, days in (("old", 20), ("held", 20), ("ancient", 200), ("new", 1)):
        made[name].last_activity_at = ago(days)
        made[name].ended_at = ago(days)
        sessions._write_session(made[name])
    retention.add_hold("acme", "user", "carol", "litigation", "dave")
    done = retention.run()
    assert done["traces"] == 1 and done["sessions"] == 1
    assert sessions.traces(made["old"]) == [] and len(sessions.records(made["old"])) == 1
    assert sessions.traces(made["held"]) == ["000001.json"] and sessions.traces(made["new"]) == ["000001.json"]
    assert sessions.get(made["ancient"].session_id) is None
    # Each session's next deletion is noted, so the next run looks at nothing that isn't due.
    with db.read() as connection:
        due = dict(connection.execute(select(schema.sessions.c.id, schema.sessions.c.retention_next)).all())
    assert due[made["old"].session_id] > ago(0) and retention.run()["traces"] == 0


# The audit log

def test_events_chain_and_any_change_to_one_is_found(tmp_path):
    db = database(tmp_path)
    audit = Audit(db)
    for index in range(3):
        audit.record(audit.event("configuration.update", tenant="acme", actor="dave", action=f"PUT /api/x/{index}"), now=True)
    audit.record(audit.event("accounts.create", tenant="globex", actor="gina"), now=True)
    assert audit.verify()["ok"] and audit.checkpoint() is not None and audit.checkpoint() is None
    events = audit.query("acme")
    assert [event["chain"]["seq"] for event in events] == [3, 2, 1] and events[0]["chain"]["prev"] == events[1]["chain"]["hash"]
    # The database refuses changes; someone who gets past that is caught by the chain and the checkpoint.
    with pytest.raises(Exception):
        with db.write() as connection:
            connection.execute(schema.audit_events.update().values(actor="mallory"))
    with db.write() as connection:
        connection.execute(text("DROP TRIGGER IF EXISTS audit_events_no_update")) if db.sqlite else connection.execute(text("ALTER TABLE mesh.audit_events DISABLE TRIGGER audit_events_append_only"))
        connection.execute(schema.audit_events.update().where(schema.audit_events.c.seq == 2).values(actor="mallory"))
    report = audit.verify("acme")
    assert not report["ok"] and any("changed after it was recorded" in problem for problem in report["problems"])


def test_audit_events_go_to_their_exports_once(tmp_path):
    db = database(tmp_path)
    audit = Audit(db)
    received = []
    audit.client = httpx.Client(transport=httpx.MockTransport(lambda request: received.append(request) or httpx.Response(200)))
    audit.set_config({"exports": [{"name": "siem", "kind": "otlp", "url": "https://collector.example/"}, {"name": "hook", "kind": "webhook", "url": "https://hooks.example/audit", "token_env": "HOOK_SECRET"}]})
    for index in range(3):
        audit.record(audit.event("sessions.update", tenant="acme", actor="bob"), now=True)
    assert audit.backlog() == 3
    assert audit.export_once() == 6 and audit.export_once() == 0
    otlp_request = next(item for item in received if item.url.path == "/v1/logs")
    records = json.loads(otlp_request.content)["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
    assert len(records) == 3 and any(attribute["key"] == "mesh.tenant" for attribute in records[0]["attributes"])
    with pytest.raises(ValueError):
        validate_config({"exports": [{"name": "x", "kind": "otlp", "url": "ftp://nope"}]})
    assert otlp([])["resourceLogs"][0]["scopeLogs"][0]["logRecords"] == []


def test_changes_through_the_api_are_audited_and_read_per_organization(tmp_path):
    from test_auth import org_settings

    app = create_app(org_settings(tmp_path))
    store = app.state.runtime.auth.store
    store.add_user("admin2", "admin-password", role="admin")
    with TestClient(app, base_url=BASE) as client:
        site = {**HOST, "x-ai-proxy-token": access_token(app, "admin2"), "origin": BASE}
        dave = {**HOST, "x-ai-proxy-token": access_token(app, "dave"), "origin": BASE}
        erin = {**HOST, "x-ai-proxy-token": access_token(app, "alice"), "origin": BASE}
        assert client.put("/api/config/retention", json={"days": {"traces": 30}}, headers=site).status_code == 200
        assert client.put("/api/orgs/acme/retention", json={"days": {"traces": 7}}, headers=dave).json()["days"]["traces"] == 7
        assert client.put("/api/orgs/globex/retention", json={"days": {"traces": 7}}, headers=dave).status_code == 404
        hold = client.post("/api/holds", json={"scope": "user", "target": "erin", "reason": "case 42"}, headers=dave).json()
        assert hold["tenant"] == "acme" and client.get("/api/holds", headers=dave).json()["holds"][0]["id"] == hold["id"]
        assert client.get("/api/holds", headers=erin).status_code == 403
        app.state.runtime.db.writer.flush()
        acme = client.get("/api/audit", headers=dave).json()["events"]
        assert {event["type"] for event in acme} >= {"mesh.audit.data.update", "mesh.audit.data.create"}
        assert all(event["tenant"] == "acme" for event in acme)
        retention = next(event for event in acme if event["data"]["action"] == "PUT /api/orgs/acme/retention")
        assert retention["type"] == "mesh.audit.data.update" and retention["data"]["changes"] == {"fields": ["days"], "days": {"traces": 7}}
        assert any(event["type"] == "mesh.audit.data.refused" and event["actor"] == "alice" for event in acme)
        everything = client.get("/api/audit", headers=site).json()["events"]
        assert any(event["data"]["action"] == "PUT /api/config/retention" and event["tenant"] == "root" for event in everything)
        assert client.get("/api/audit", headers=erin).status_code == 403
        assert client.get("/api/audit/verify", headers=site).json()["ok"]
        exported = client.get("/api/audit/export?format=csv", headers=dave)
        assert exported.status_code == 200 and exported.text.startswith("time,type,tenant")
        # Refusals are audited too.
        assert client.put("/api/config/retention", json={}, headers=dave).status_code == 403
        app.state.runtime.db.writer.flush()
        refused = client.get("/api/audit?outcome=refused", headers=site).json()["events"]
        assert refused and refused[0]["actor"] == "dave"


# Blob storage

def test_traces_in_blob_storage_keep_each_message_once(tmp_path):
    store = blobs.FileBlobs(tmp_path / "blobs")
    db = database(tmp_path)
    sessions = SessionStore(db, blobs=store)
    session = sessions.create({"client": "claude", "backend": "x", "trace": True, "tags": {"tenant": "acme"}})
    long = {"role": "user", "content": "x" * 2000}
    for index in range(3):
        messages = [long] + [{"role": "assistant", "content": f"answer {n} " * 50} for n in range(index)]
        sessions.write_trace(session, f"{index + 1:06d}", {"request": {"messages": messages}, "response": {"n": index}})
    sessions.flush()
    assert sessions.read_trace(session, "000003")["request"]["messages"][0] == long
    assert len(sessions.read_traces(session, ["000001", "000002", "000003"])) == 3
    prefix = blobs.session_prefix("acme", session.session_id, session.started_at)
    stored = list((tmp_path / "blobs" / prefix / "blocks").iterdir())
    assert len(stored) == 3  # the first message once, and each answer once
    sessions.delete(session)
    assert not (tmp_path / "blobs" / prefix).exists()


def test_s3_requests_are_signed_and_listed():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "GET" and request.url.params.get("list-type") == "2":
            body = b"<ListBucketResult xmlns='http://s3.amazonaws.com/doc/2006-03-01/'><IsTruncated>false</IsTruncated><Contents><Key>p/a/1</Key></Contents></ListBucketResult>"
            return httpx.Response(200, content=body)
        if request.method == "GET":
            return httpx.Response(404)
        return httpx.Response(200)

    store = blobs.S3Blobs("bucket", "p", endpoint="http://minio.local:9000", region="us-east-1",
                          credentials=blobs.Credentials("AKID", "SECRET"), client=httpx.Client(transport=httpx.MockTransport(handler)))
    store.put("a/1", b"data")
    assert store.get("a/1") is None and store.delete_prefix("a") == 1
    put = seen[0]
    assert put.url.path == "/bucket/p/a/1" and put.headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKID/")
    assert "x-amz-content-sha256" in put.headers


def test_audit_pages_never_skip_events_that_share_a_time(tmp_path):
    from oidc_helpers import database as open_database

    db = open_database(tmp_path)
    audit = Audit(db, clock=lambda: 1000.0)
    for index in range(5):
        audit.record(audit.event("sessions.update", tenant="acme", actor=f"u{index}"), now=True)
    first = audit.query("acme", limit=2)
    last = first[-1]
    rest = audit.query("acme", before=last["time_epoch"], before_id=last["id"], limit=10)
    assert len(first) + len(rest) == 5 and not {event["id"] for event in first} & {event["id"] for event in rest}
