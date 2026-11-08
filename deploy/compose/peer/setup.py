"""Links the peer quickstart's two servers and opens a bridge between acme and globex, through their APIs.

Run it on the host once the stack is up (podman compose up -d): python3 setup.py. It does what the
site admins and the organizations' admins would do in the consoles, so the steps are the guide's:

1. Each site admin makes a link code; server-a looks server-b up, checks its fingerprint and enters
   its code; server-b enters server-a's. Each side sets its link policy (everything, every organization).
2. bob adds gpu-box's Ollama as globex's provider, once the machine is connected.
3. alice (acme's admin) and bob (globex's) each make a bridge code and enter the other's.
4. bob lets gpu-box's provider and launches on gpu-box out, lets the bridge use the machine, and sets
   the bridge a usage limit; alice lets them in.

It signs in through each issuer's device flow as a person would (machine.py's way). Running it again
skips what's done. QUICKSTART_ADMIN_PASSWORD and QUICKSTART_PEOPLE_PASSWORD, if you set them for the stack, are
the site admins' and the people's passwords.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from http.cookiejar import CookieJar
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

ADMIN_PASSWORD = os.getenv("QUICKSTART_ADMIN_PASSWORD") or "secret"
PEOPLE_PASSWORD = os.getenv("QUICKSTART_PEOPLE_PASSWORD") or "peer-demo"
A = {"api": "http://localhost:29999", "issuer": "http://localhost:30000", "inside": "http://server-a:9999"}
B = {"api": "http://127.0.0.1:39999", "issuer": "http://127.0.0.1:40000", "inside": "http://server-b:9999"}
EVERYTHING = {"out": ["provider", "mcp", "agent", "launch", "terminal"], "in": ["provider", "mcp", "agent", "launch", "terminal"], "orgs": ["*"]}


def say(text: str) -> None:
    print(text, flush=True)


def token(server: dict, user: str) -> str:
    """An access token for user, by the issuer's device flow with its CSRF-bound form."""
    opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def call(path: str, data: dict | None = None, origin: bool = False) -> str:
        request = Request(server["issuer"] + path, data=urlencode(data).encode() if data is not None else None, headers={"Origin": server["issuer"]} if origin else {})
        with opener.open(request, timeout=15) as response:
            return response.read().decode()

    device = json.loads(call("/device_authorization", {"client_id": "mesh-cli", "scope": "openid profile offline_access mesh-api"}))
    form = call("/device")
    transaction = re.search(r'name="transaction" value="([^"]+)"', form)
    if transaction is None:
        raise SystemExit(f"{server['issuer']} didn't show its device form")
    call("/device", {"transaction": transaction.group(1), "user_code": device["user_code"], "username": user, "password": ADMIN_PASSWORD if user == "admin" else PEOPLE_PASSWORD, "approve": "yes"}, origin=True)
    answer = json.loads(call("/token", {"client_id": "mesh-cli", "grant_type": "urn:ietf:params:oauth:grant-type:device_code", "device_code": device["device_code"]}))
    if not answer.get("access_token"):
        raise SystemExit(f"{user} couldn't sign in at {server['issuer']}")
    return answer["access_token"]


class Api:
    def __init__(self, server: dict, user: str):
        self.base = server["api"]
        self.user = user
        self.token = token(server, user)
        self.opener = build_opener(ProxyHandler({}))

    def __call__(self, method: str, path: str, body: dict | None = None, ok: tuple[int, ...] = (200, 201)) -> dict:
        request = Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                          headers={"x-ai-proxy-token": self.token, **({"content-type": "application/json"} if body is not None else {})})
        try:
            with self.opener.open(request, timeout=60) as response:
                status, text = response.status, response.read().decode()
        except HTTPError as error:
            status, text = error.code, error.read().decode()
        if status not in ok:
            raise SystemExit(f"{self.user}: {method} {path} answered {status}: {text}")
        return json.loads(text) if text else {}


def wait(what: str, check, seconds: float = 120.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        found = check()
        if found:
            return found
        time.sleep(2)
    raise SystemExit(f"Gave up waiting for {what}")


def main() -> int:
    say("Signing in: admin and alice at server-a, admin and bob at server-b...")
    site_a, alice = Api(A, "admin"), Api(A, "alice")
    site_b, bob = Api(B, "admin"), Api(B, "bob")

    links = site_a("GET", "/api/links")["links"]
    if not any(link["peer"] == "server-b" and link["state"] == "active" for link in links):
        say("Linking server-a and server-b...")
        code_a, code_b = site_a("POST", "/api/links/codes", {}), site_b("POST", "/api/links/codes", {})
        seen = site_a("POST", "/api/links/inspect", {"url": B["inside"]})
        if seen["fingerprint"] != code_b["fingerprint"]:
            raise SystemExit("server-b's fingerprint isn't the one its admin sees: stop here")
        say(f"  server-b's fingerprint: {seen['fingerprint']}")
        site_a("POST", "/api/links/join", {"url": B["inside"], "code": code_b["code"], "fingerprint": seen["fingerprint"]})
        presented = wait("server-a to present", lambda: next((link for link in site_b("GET", "/api/links")["links"] if link["peer"] == "server-a"), None))
        site_b("POST", "/api/links/join", {"link": presented["id"], "code": code_a["code"]})
    link = wait("the link", lambda: next((item for item in site_a("GET", "/api/links")["links"] if item["peer"] == "server-b" and item["state"] == "active"), None))
    for site in (site_a, site_b):
        site("PUT", f"/api/links/{link['id']}/policy", EVERYTHING)
    say(f"Linked ({link['id']}).")

    say("Waiting for gpu-box to connect to server-b...")
    machine = wait("gpu-box", lambda: next((item for item in bob("GET", "/api/machines")["machines"] if item["name"] == "gpu-box" and item["connected"]), None), 300)
    if "gpu-box" not in machine.get("backends", []):
        wait("gpu-box's Ollama", lambda: any(item["type"] == "ollama" for item in bob("GET", f"/api/machines/{machine['id']}")["providers"]), 120)
        bob("POST", f"/api/machines/{machine['id']}/providers/adopt", {"type": "ollama"}, ok=(200, 201, 409))
    say("gpu-box's Ollama is globex's provider gpu-box.")

    bridges = alice("GET", "/api/orgs/acme/bridges")["bridges"]
    if not any(item["peer_org"] == "globex" and item["state"] == "active" for item in bridges):
        say("Opening a bridge between acme and globex...")
        code_b = bob("POST", "/api/orgs/globex/bridges/codes", {"link": link["id"]})
        code_a = alice("POST", "/api/orgs/acme/bridges/codes", {"link": link["id"]})
        named = alice("POST", "/api/orgs/acme/bridges/inspect", {"link": link["id"], "code": code_b["code"]})
        if named["org"] != "globex":
            raise SystemExit(f"That code is {named['org']}'s, not globex's: stop here")
        alice("POST", "/api/orgs/acme/bridges/join", {"link": link["id"], "code": code_b["code"], "peer_org": "globex"})
        bob("POST", "/api/orgs/globex/bridges/join", {"link": link["id"], "code": code_a["code"], "peer_org": "acme"})
    bridge = wait("the bridge", lambda: next((item for item in alice("GET", "/api/orgs/acme/bridges")["bridges"] if item["peer_org"] == "globex" and item["state"] == "active"), None))

    say("globex lets out gpu-box's model and launches on it; acme lets them in...")
    bob("PUT", f"/api/machines/{machine['id']}/access", {"access": {"launch": {"orgs": [], "teams": [], "bridges": [bridge["id"]]}, "terminal": {"orgs": [], "teams": [], "bridges": [bridge["id"]]}}})
    shared = [{"kind": "provider", "name": "gpu-box"}, {"kind": "launch", "machine": "gpu-box"}, {"kind": "terminal", "machine": "gpu-box"}]
    bob("PUT", f"/api/orgs/globex/bridges/{bridge['id']}/policy", {"out": shared, "in": []})
    bob("PUT", f"/api/orgs/globex/bridges/{bridge['id']}/limits", {"provider": "gpu-box", "window": "month", "tokens": 10_000_000})
    alice("PUT", f"/api/orgs/acme/bridges/{bridge['id']}/policy", {"out": [], "in": shared})
    alice("POST", f"/api/orgs/acme/bridges/{bridge['id']}/refresh", {})
    wait("what crosses to acme", lambda: alice("GET", "/api/peer/resources")["providers"], 60)

    say("")
    say("Done. Open http://localhost:29999 and sign in as alice / peer-demo:")
    say("  Agents > Shared with you lists provider:server-b/globex/gpu-box and machine:server-b/globex/gpu-box.")
    say("  Pull a model on gpu-box first: http://127.0.0.1:39999 as bob / peer-demo, Machines > gpu-box > its Ollama.")
    say("  Then start an agent on laptop-a with that provider, or on gpu-box itself from Shared with you.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
