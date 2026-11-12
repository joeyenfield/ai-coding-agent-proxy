"""An organization's costs between two days (costs.py): the summary, who sees what, and pricing recorded calls again."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from agent_server import costs
from agent_server.app import create_app
from test_auth import signed_in
from test_tenancy import tenant_settings
from test_usage_limits import call, use_mock

PRICES = [{"match": "claude-test*", "input": 10.0, "output": 20.0}]
# 600 tokens in and 500 out (test_usage_limits.upstream) at $10 and $20 for a million.
CALL_USD = 0.006 + 0.01


def test_a_range_is_whole_days_in_order():
    assert costs.day_range("2026-11-01", "2026-11-03") == ("2026-11-01", "2026-11-04", ["2026-11-01", "2026-11-02", "2026-11-03"])
    for since, until in (("2026-11-03", "2026-11-01"), ("yesterday", "2026-11-01"), (None, None), ("2025-01-01", "2026-11-01")):
        with pytest.raises(ValueError):
            costs.day_range(since, until)


def test_costs_are_summed_per_organization_and_priced_again_on_request(tmp_path):
    app = create_app(tenant_settings(tmp_path))
    site, dave, alice, gina = (signed_in(app, name) for name in ("site", "dave", "alice", "gina"))
    today = datetime.now(timezone.utc).date()
    days = {"since": (today - timedelta(days=1)).isoformat(), "until": today.isoformat()}
    with site, dave, alice, gina:
        old = use_mock(app, [])
        try:
            # Two calls of alice's and one of dave's in Acme, one of gina's in Globex, on a model with no price yet.
            mine = alice.post("/api/sessions", json={"client": "x", "backend": "hosted"}).json()["session_id"]
            his = dave.post("/api/sessions", json={"client": "x", "backend": "hosted"}).json()["session_id"]
            hers = gina.post("/api/sessions", json={"client": "x", "backend": "hosted"}).json()["session_id"]
            for client, session in ((alice, mine), (alice, mine), (dave, his), (gina, hers)):
                assert call(client, session).status_code == 200

            # Its admin sees the organization's; a member only their own; another organization's is a 404.
            whole = dave.get("/api/orgs/acme/costs", params=days).json()
            assert whole["manage"] is True and (whole["requests"], whole["unpriced"], whole["priced"], whole["sessions"]) == (3, 3, 0, 2)
            assert [(item["user"], item["requests"]) for item in whole["users"]] == [("alice", 2), ("dave", 1)]
            assert [item["day"] for item in whole["days"]] == [days["since"], days["until"]] and whole["days"][1]["requests"] == 3
            own = alice.get("/api/orgs/acme/costs", params=days).json()
            assert own["manage"] is False and own["requests"] == 2 and [item["user"] for item in own["users"]] == ["alice"]
            assert gina.get("/api/orgs/acme/costs", params=days).status_code == 404
            assert site.get("/api/orgs/acme/costs", params=days).json()["requests"] == 3
            assert dave.get("/api/orgs/acme/costs", params={"since": "2026-02-01", "until": "2026-01-01"}).status_code == 400

            # Anyone signed in reads the price table; only site admins change it.
            assert alice.get("/api/prices").json()["editable"] is False and alice.get("/api/config/prices").status_code == 403
            assert site.put("/api/config/prices", json={"prices": PRICES}).status_code == 200
            assert site.get("/api/prices").json() == {"currency": "USD", "prices": [{"match": "claude-test*", "input": 10.0, "output": 20.0}], "custom": True, "editable": True}

            # A new price doesn't reach recorded calls by itself; recomputing is the organization's admins' to do.
            assert dave.get("/api/orgs/acme/costs", params=days).json()["priced"] == 0
            assert alice.post("/api/orgs/acme/costs/recompute", json=days).status_code == 403
            assert gina.post("/api/orgs/acme/costs/recompute", json=days).status_code == 404
            assert dave.post("/api/orgs/acme/costs/recompute", json={"since": "soon"}).status_code == 400
            done = dave.post("/api/orgs/acme/costs/recompute", json=days).json()
            assert (done["requests"], done["changed"], done["sessions"]) == (3, 3, 2)
            assert done["before"]["unpriced"] == 3 and done["after"]["unpriced"] == 0
            # An anthropic pass-through is a subscription's: what the tokens would cost at API prices, not money spent.
            assert done["after"]["subscription_usd"] == pytest.approx(3 * CALL_USD) and done["after"]["usd"] == 0

            whole = dave.get("/api/orgs/acme/costs", params=days).json()
            assert whole["priced"] == 3 and whole["subscription_usd"] == pytest.approx(3 * CALL_USD)
            assert whole["models"][0]["model"] == "claude-test" and whole["providers"][0]["provider"] == "hosted"
            # The sessions' own totals and the requests follow, and the other organization's are untouched.
            assert alice.get(f"/api/sessions/{mine}/cost").json()["subscription_usd"] == pytest.approx(2 * CALL_USD)
            assert all(record["cost"]["price"] == "claude-test*" and "unpriced" not in record for record in app.state.runtime.records(app.state.runtime.sessions.get(his)))
            assert all(item.get("cost") for item in app.state.runtime.recent if item["session_id"] in (mine, his))
            assert gina.get("/api/orgs/globex/costs", params=days).json()["unpriced"] == 1

            # Again at the same prices changes nothing; at new prices, calls priced before get the new ones.
            assert dave.post("/api/orgs/acme/costs/recompute", json=days).json()["changed"] == 0
            site.put("/api/config/prices", json={"prices": [{"match": "claude-test*", "input": 20.0, "output": 40.0}]})
            again = dave.post("/api/orgs/acme/costs/recompute", json=days).json()
            assert again["changed"] == 3 and again["after"]["subscription_usd"] == pytest.approx(6 * CALL_USD)
            # An account agent's intercepted call is recorded under the host it went to, and is a subscription's too.
            intercepted = {"kind": "intercept", "backend": "api.anthropic.com", "model": "claude-test", "input_tokens": 1000, "output_tokens": 0}
            app.state.runtime._price(intercepted)
            assert intercepted["cost"]["basis"] == "subscription" and intercepted["cost"]["usd"] == pytest.approx(0.02)
            page = {"kind": "intercept", "backend": "claude.ai", "model": "", "input_tokens": 0, "output_tokens": 0}
            app.state.runtime._price(page)
            assert "cost" not in page and "unpriced" not in page
            # A day range the calls aren't in reads none of them.
            assert dave.post("/api/orgs/acme/costs/recompute", json={"since": "2020-01-01", "until": "2020-01-31"}).json()["requests"] == 0
        finally:
            app.state.runtime.client = old
