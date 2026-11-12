"""An organization's usage between two days (analytics.py): the split, narrowing it, and who sees what."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_server import analytics
from agent_server.app import create_app
from test_auth import signed_in
from test_tenancy import tenant_settings
from test_usage_limits import call, use_mock


def test_usage_is_split_and_narrowed_per_organization(tmp_path, monkeypatch):
    app = create_app(tenant_settings(tmp_path))
    dave, alice, gina = (signed_in(app, name) for name in ("dave", "alice", "gina"))
    today = datetime.now(timezone.utc).date()
    days = {"since": (today - timedelta(days=1)).isoformat(), "until": today.isoformat()}
    with dave, alice, gina:
        old = use_mock(app, [])
        try:
            # Two sessions of alice's (three calls) and one of dave's in Acme, one of gina's in Globex.
            first = alice.post("/api/sessions", json={"client": "x", "backend": "hosted"}).json()["session_id"]
            second = alice.post("/api/sessions", json={"client": "y", "backend": "hosted"}).json()["session_id"]
            his = dave.post("/api/sessions", json={"client": "x", "backend": "hosted"}).json()["session_id"]
            hers = gina.post("/api/sessions", json={"client": "x", "backend": "hosted"}).json()["session_id"]
            for client, session in ((alice, first), (alice, first), (alice, second), (dave, his), (gina, hers)):
                assert call(client, session).status_code == 200

            # Its admin sees everyone's, split by person: the busiest first, each with a series in the day.
            whole = dave.get("/api/orgs/acme/analytics", params={**days, "by": "user"}).json()
            assert whole["manage"] is True and (whole["requests"], whole["sessions"], whole["users"]) == (4, 3, 2)
            assert [(item["key"], item["slot"], item["requests"], item["sessions"]) for item in whole["values"]] == [("alice", 0, 3, 2), ("dave", 1, 1, 1)]
            assert [item["day"] for item in whole["days"]] == [days["since"], days["until"]]
            day = whole["days"][1]
            assert (day["requests"], day["sessions"], day["users"]) == (4, 3, 2) and day["series"]["alice"]["requests"] == 3 and day["series"]["dave"]["users"] == 1
            assert whole["days"][0]["requests"] == 0 and whole["days"][0]["series"] == {}
            assert whole["input_tokens"] == 4 * 600 and {item["session_id"] for item in whole["top_sessions"]} == {first, second, his}

            # Drilling down: one person's calls, split by the agent they ran.
            drilled = dave.get("/api/orgs/acme/analytics", params={**days, "by": "agent", "user": "alice"}).json()
            assert drilled["filters"] == {"user": "alice"} and drilled["requests"] == 3 and drilled["users"] == 1
            assert [(item["key"], item["requests"]) for item in drilled["values"]] == [("x", 2), ("y", 1)]
            assert dave.get("/api/orgs/acme/analytics", params={**days, "by": "model", "user": "alice", "agent": "y"}).json()["requests"] == 1
            assert dave.get("/api/orgs/acme/analytics", params={**days, "model": "no-such-model"}).json()["requests"] == 0

            # A member sees only their own, whoever they ask for; another organization's is a 404.
            own = alice.get("/api/orgs/acme/analytics", params={**days, "by": "user", "user": "dave"}).json()
            assert own["manage"] is False and own["requests"] == 3 and [item["key"] for item in own["values"]] == ["alice"]
            assert gina.get("/api/orgs/acme/analytics", params=days).status_code == 404
            assert gina.get("/api/orgs/globex/analytics", params=days).json()["requests"] == 1
            assert dave.get("/api/orgs/acme/analytics", params={**days, "by": "colour"}).status_code == 400
            assert dave.get("/api/orgs/acme/analytics", params={"since": "2026-02-01", "until": "2026-01-01"}).status_code == 400

            # More values than series: the busiest keep theirs, the rest are counted together.
            monkeypatch.setattr(analytics, "SERIES", 1)
            folded = dave.get("/api/orgs/acme/analytics", params={**days, "by": "user"}).json()
            assert [(item["key"], item["slot"]) for item in folded["values"]] == [("alice", 0), ("dave", None)]
            assert folded["days"][1]["series"][analytics.OTHER]["requests"] == 1 and folded["days"][1]["series"]["alice"]["requests"] == 3
        finally:
            app.state.runtime.client = old


def test_a_split_is_a_known_dimension(tmp_path):
    app = create_app(tenant_settings(tmp_path))
    with pytest.raises(ValueError):
        analytics.usage(app.state.runtime.db, "acme", "2026-11-01", "2026-11-02", by="colour")
