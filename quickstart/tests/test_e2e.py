"""End-to-end sign-in and permission tests against the running quickstart stack.

Start the stack first (`podman compose up -d` in quickstart/, with `-f compose.yaml -f compose.build.yaml ... --build` to test this checkout's code), then from the
repository root: `python -m pytest -q quickstart/tests`. The tests talk to the published
ports exactly as a browser and the launchers do, so they cover what the unit tests can't:
the issuer's real headers, cookies across both services, and the fleet's machines.

Without a reachable stack every test is skipped, unless QUICKSTART_E2E=1 makes that a
failure (for CI). The tests clean up the users, organizations and grants they create.

The issuer allows 10 password attempts per address per 10 minutes and the suite makes 7,
so wait before running it again, or reset the limit with `podman compose restart auth`.
"""
from __future__ import annotations

import os
import re
import secrets
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

API = os.getenv("QUICKSTART_API", "http://localhost:19999")
ISSUER = os.getenv("QUICKSTART_ISSUER", "http://localhost:20000")
ADMIN = "admin"
PASSWORD = os.getenv("QUICKSTART_ADMIN_PASSWORD", "secret")
CLI_CLIENT = "mesh-cli"


def _reachable() -> bool:
    try:
        return all(httpx.get(url + "/health", timeout=3, trust_env=False).status_code == 200 for url in (API, ISSUER))
    except httpx.HTTPError:
        return False


if not _reachable():
    if os.getenv("QUICKSTART_E2E") == "1":
        raise RuntimeError(f"The quickstart isn't answering at {API} and {ISSUER}")
    pytest.skip("the quickstart stack isn't running", allow_module_level=True)


class Browser:
    """Cookies shared by both ports, as a browser keeps them for localhost, and its Origin rules."""

    def __init__(self):
        self.client = httpx.Client(timeout=15, trust_env=False, follow_redirects=False)

    def close(self):
        self.client.close()

    def sign_in(self, user: str, password: str) -> httpx.Response:
        """The console's code flow. Returns the issuer's answer to the form, for refusals."""
        start = self.client.get(API + "/api/auth/login")
        assert start.status_code == 302, start.text
        assert start.headers["location"].startswith(ISSUER + "/authorize?")
        page = self.client.get(start.headers["location"])
        assert page.status_code == 200, page.text
        answer = self.submit(page, "/authorize", {"username": user, "password": password, "approve": "yes"})
        if answer.status_code == 429:
            pytest.fail("The issuer's password rate limit was hit: wait 10 minutes or `podman compose restart auth`")
        if answer.status_code != 302:
            return answer
        callback = answer.headers["location"]
        assert callback.startswith(API + "/api/auth/callback?")
        done = self.client.get(callback)
        assert done.status_code in (302, 307), done.text
        return answer

    def submit(self, page: httpx.Response, path: str, fields: dict, origin: str | None = ISSUER) -> httpx.Response:
        # A browser posts the issuer's own forms with the issuer's origin (its Referrer-Policy
        # must not turn that into "null"), and only to targets its form-action allows.
        policy = page.headers["content-security-policy"]
        assert re.search(r"form-action 'self' [^;]*" + re.escape(API), policy), policy
        assert page.headers["referrer-policy"] != "no-referrer"
        transaction = re.search(r'name="transaction" value="([^"]+)"', page.text).group(1)
        headers = {"Origin": origin} if origin else {}
        return self.client.post(ISSUER + path, data={"transaction": transaction, **fields}, headers=headers)

    def api(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {"Origin": API, **kwargs.pop("headers", {})}
        return self.client.request(method, API + path, headers=headers, **kwargs)

    def identity(self) -> dict | None:
        return self.api("GET", "/api/auth/status").json()["identity"]


def device_token(user: str, password: str) -> dict:
    """What `agent login` and `agent-machine login` do, with the approval typed in a browser."""
    with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
        device = client.post(ISSUER + "/device_authorization", data={
            "client_id": CLI_CLIENT, "scope": "openid profile offline_access mesh-api"}).json()
        browser = Browser()
        try:
            page = browser.client.get(ISSUER + "/device")
            approved = browser.submit(page, "/device", {
                "user_code": device["user_code"], "username": user, "password": password, "approve": "yes"})
            assert approved.status_code == 200, approved.text
        finally:
            browser.close()
        token = client.post(ISSUER + "/token", data={
            "client_id": CLI_CLIENT, "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            "device_code": device["device_code"]})
        assert token.status_code == 200, token.text
        return token.json()


@pytest.fixture(scope="module")
def admin():
    browser = Browser()
    assert browser.sign_in(ADMIN, PASSWORD).status_code == 302
    yield browser
    browser.close()


@pytest.fixture(scope="module")
def member(admin):
    """A fresh member, signed in once for the module and removed afterwards."""
    name = "e2e-" + secrets.token_hex(4)
    password = secrets.token_urlsafe(16)
    created = admin.api("POST", "/api/users", json={"name": name, "password": password, "role": "member"})
    assert created.status_code == 201, created.text
    browser = Browser()
    try:
        assert browser.sign_in(name, password).status_code == 302
        yield name, password, browser
    finally:
        browser.close()
        admin.api("DELETE", f"/api/users/{name}")


@pytest.fixture(scope="module")
def machines(admin):
    listed = admin.api("GET", "/api/machines")
    assert listed.status_code == 200, listed.text
    by_name = {machine["name"]: machine for machine in listed.json()["machines"]}
    assert {"machine-1", "machine-2"} <= set(by_name), sorted(by_name)
    return by_name


def test_health_is_public():
    for url in (API, ISSUER):
        assert httpx.get(url + "/health", trust_env=False).json()["status"] == "ok"


def test_signed_out_requests_are_refused():
    with httpx.Client(trust_env=False) as client:
        for path in ("/api/sessions", "/api/machines", "/api/users", "/api/config"):
            assert client.get(API + path).status_code == 401, path
        assert client.get(API + "/api/auth/status").json()["identity"] is None


def test_console_sign_in_and_sign_out(admin):
    identity = admin.identity()
    assert identity["user"] == ADMIN and identity["role"] == "admin"
    browser = Browser()
    try:
        assert browser.sign_in(ADMIN, PASSWORD).status_code == 302
        assert browser.api("GET", "/api/sessions").status_code == 200
        assert browser.api("POST", "/api/auth/logout").status_code == 200
        assert browser.identity() is None
        assert browser.api("GET", "/api/sessions").status_code == 401
    finally:
        browser.close()


def test_wrong_password_is_refused():
    browser = Browser()
    try:
        answer = browser.sign_in(ADMIN, "not-the-password")
        assert answer.status_code == 401 and "Wrong username or password." in answer.text
        assert browser.identity() is None
    finally:
        browser.close()


@pytest.mark.parametrize("origin", ["https://evil.example.com", "null", None])
def test_sign_in_form_needs_the_issuers_origin(origin):
    browser = Browser()
    try:
        page = browser.client.get(browser.client.get(API + "/api/auth/login").headers["location"])
        answer = browser.submit(page, "/authorize", {"username": ADMIN, "password": PASSWORD, "approve": "yes"}, origin=origin)
        assert answer.status_code == 403 and "This page has expired" in answer.text
    finally:
        browser.close()


def test_device_sign_in_gives_launcher_tokens():
    token = device_token(ADMIN, PASSWORD)
    assert token["token_type"].lower() == "bearer" and token["refresh_token"]
    with httpx.Client(trust_env=False) as client:
        status = client.get(API + "/api/auth/status", headers={"X-AI-Proxy-Token": token["access_token"]}).json()
        assert status["identity"]["user"] == ADMIN
        assert client.get(API + "/api/machines", headers={"X-AI-Proxy-Token": "not-a-token"}).status_code == 401


def test_both_machines_are_in_the_fleet(machines):
    for name in ("machine-1", "machine-2"):
        assert machines[name]["connected"], name
        assert machines[name]["you"]["owner"], name
    assert any(provider["type"] == "ollama" for provider in machines["machine-1"]["providers"])


def test_members_cannot_change_the_server(admin, member):
    name, password, browser = member
    assert browser.identity()["role"] == "member"
    config = admin.api("GET", "/api/config").json()["config"]
    assert browser.api("PUT", "/api/config", json=config).status_code == 403
    assert browser.api("POST", "/api/users", json={"name": "e2e-nope", "password": secrets.token_urlsafe(16)}).status_code == 403
    assert browser.api("POST", "/api/orgs", json={"id": "e2e-nope"}).status_code == 403


def test_machines_are_shared_only_through_grants(admin, member, machines):
    name, password, browser = member
    org = "e2e-" + secrets.token_hex(4)
    target = machines["machine-2"]
    before = target["access"]
    try:
        # The admin's machines are invisible to others until shared: 404, not 403.
        assert browser.api("GET", "/api/machines").json()["machines"] == []
        assert browser.api("GET", f"/api/machines/{target['id']}").status_code == 404

        assert admin.api("POST", "/api/orgs", json={"id": org, "name": "E2E"}).status_code == 201
        assert admin.api("PUT", f"/api/orgs/{org}/members/{name}").status_code == 200
        granted = admin.api("PUT", f"/api/machines/{target['id']}/access", json={"access": {**before, "view": {"orgs": [org], "teams": []}}})
        assert granted.status_code == 200, granted.text

        # Permissions are reloaded on every request, so the grant applies without signing in again.
        seen = browser.api("GET", "/api/machines").json()["machines"]
        assert [machine["name"] for machine in seen] == ["machine-2"]
        you = seen[0]["you"]
        assert you["view"] and not you["owner"] and not you["manage"]
        assert not you["launch"] and not you["terminal"]
        assert browser.api("POST", f"/api/machines/{target['id']}/shell", json={"folder": "/workspace"}).status_code == 403
        assert browser.api("PUT", f"/api/machines/{target['id']}/access", json={"access": {}}).status_code == 403
    finally:
        admin.api("PUT", f"/api/machines/{target['id']}/access", json={"access": before})
        admin.api("DELETE", f"/api/orgs/{org}")
    assert browser.api("GET", "/api/machines").json()["machines"] == []


# Keep this last: it removes the module's member.
def test_removing_a_user_ends_their_sign_in(admin, member):
    name, password, browser = member
    token = device_token(name, password)
    assert browser.api("GET", "/api/sessions").status_code == 200
    assert admin.api("DELETE", f"/api/users/{name}").status_code == 200
    assert browser.api("GET", "/api/sessions").status_code == 401
    with httpx.Client(trust_env=False) as client:
        assert client.get(API + "/api/sessions", headers={"X-AI-Proxy-Token": token["access_token"]}).status_code == 401
        refreshed = client.post(ISSUER + "/token", data={"client_id": CLI_CLIENT, "grant_type": "refresh_token", "refresh_token": token["refresh_token"]})
        assert refreshed.status_code == 400
    assert "Wrong username or password." in browser.sign_in(name, password).text
