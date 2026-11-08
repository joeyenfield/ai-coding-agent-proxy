"""Local demo provisioning through the issuer's normal CSRF-bound device flow.

Optional, for the test-data stack (devhelpers/testdata): QUICKSTART_MCP shares MCP servers
(name=command,...); once the machine is in the fleet, QUICKSTART_ADOPT adds its model servers as its
organization's providers (ollama,...) and QUICKSTART_GRANT gives its whole organization features
(launch,files,mcp,...), as its owner would in the console.
"""
from __future__ import annotations

from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import shlex
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

# The basic quickstart's and the all-in-one image's addresses, and admin; the peer quickstart names others.
API = os.getenv("QUICKSTART_API") or "http://localhost:19999"
ISSUER = os.getenv("QUICKSTART_ISSUER") or "http://localhost:20000"
USER = os.getenv("QUICKSTART_USER") or "admin"
CLIENT = "mesh-cli"


class Browser:
    def __init__(self):
        self.opener = build_opener(ProxyHandler({}), HTTPCookieProcessor(CookieJar()))

    def request(self, path, data=None, *, origin=False):
        headers = {"Origin": ISSUER} if origin else {}
        body = urlencode(data).encode() if data is not None else None
        with self.opener.open(Request(ISSUER + path, data=body, headers=headers), timeout=15) as response:
            return response.read().decode()


def sign_in(path, password):
    if path.exists():
        saved = json.loads(path.read_text())
        entry = saved.get("servers", {}).get(API, {})
        if entry.get("refresh_token") and entry.get("issuer") == ISSUER:
            print(f"Reusing {path.parent.name} sign-in", flush=True)
            return  # The normal Go client owns refresh and its cross-process lock.

    browser = Browser()
    device = json.loads(browser.request("/device_authorization", {
        "client_id": CLIENT, "scope": "openid profile offline_access mesh-api",
    }))
    form = browser.request("/device")
    transaction = re.search(r'name="transaction" value="([^"]+)"', form)
    if transaction is None:
        raise RuntimeError("Issuer did not return a device approval form")
    browser.request("/device", {
        "transaction": transaction.group(1), "user_code": device["user_code"],
        "username": USER, "password": password, "approve": "yes",
    }, origin=True)
    # Approval has already completed, so the first token poll can succeed immediately.
    token = json.loads(browser.request("/token", {
        "client_id": CLIENT, "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device["device_code"],
    }))
    if not token.get("access_token") or not token.get("refresh_token") or token.get("token_type", "").lower() != "bearer":
        raise RuntimeError("Issuer did not return rotating OAuth credentials")
    entry = {
        "url": API, "user": USER, "access_token": token["access_token"],
        "refresh_token": token["refresh_token"], "expires_at": int(time.time()) + token["expires_in"],
        "issuer": ISSUER, "client_id": CLIENT, "token_endpoint": ISSUER + "/token",
        "revocation_endpoint": ISSUER + "/revoke",
    }
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_suffix(".pending")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output:
        json.dump({"default": API, "servers": {API: entry}}, output, indent=2)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)
    print(f"Signed {path.parent.name} in as {USER}", flush=True)


def main():
    password = os.environ.pop("QUICKSTART_PASSWORD")
    config = Path.home() / ".config"
    sign_in(config / "agent-machine" / "credentials.json", password)
    sign_in(config / "agent-proxy" / "credentials.json", password)
    del password
    if os.getenv("QUICKSTART_OLLAMA") == "1":
        # The relay accepts connections before Ollama answers, so wait for a real reply.
        opener = build_opener(ProxyHandler({}))
        for attempt in range(120):
            try:
                with opener.open("http://127.0.0.1:11434/api/version", timeout=2):
                    break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError("Ollama did not start on machine-1's loopback")
    # Servers on where the quickstart's Ollama is, so the console can pull models into it (the
    # guide's next step) with nothing to type on the machine. It's another container: agent-machine
    # can't start or stop it, only manage its models. machine-2 has no model server to manage.
    servers = "on" if os.getenv("QUICKSTART_OLLAMA") == "1" else "off"
    for arguments in (
        ["name", socket.gethostname()], ["address", socket.gethostname()],
        ["launches", "on"], ["roots", "add", "/workspace"],
        ["commands", "on"], ["containers", "off"], ["servers", servers],
    ):
        subprocess.run(["agent-machine", *arguments], check=True)
    share_mcp(os.getenv("QUICKSTART_MCP") or "")
    if os.getenv("QUICKSTART_ADOPT") or os.getenv("QUICKSTART_GRANT"):
        # The fleet only knows the machine once agent-machine run connects: do the rest beside it.
        subprocess.Popen([sys.executable, __file__, "provision"])
    os.execvp("agent-machine", ["agent-machine", "run"])


def share_mcp(servers: str) -> None:
    """name=command args,... as agent-machine mcp add; replaced on each start, so a changed command applies."""
    for entry in filter(None, (item.strip() for item in servers.split(","))):
        name, _, command = entry.partition("=")
        subprocess.run(["agent-machine", "mcp", "remove", name], capture_output=True)
        subprocess.run(["agent-machine", "mcp", "add", name, "--dir", "/workspace",
                        "--description", f"The test data's {name} MCP server", "--", *shlex.split(command)], check=True)


def api(method, path, body=None):
    """The server's API as this machine's owner, with agent-machine's current access token (it refreshes it)."""
    saved = json.loads((Path.home() / ".config" / "agent-machine" / "credentials.json").read_text())
    token = saved["servers"][API]["access_token"]
    request = Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(request, timeout=15) as response:
        return json.loads(response.read() or b"null")


def provision() -> None:
    machine = json.loads(subprocess.run(["agent-machine", "status", "--json"], capture_output=True, text=True, check=True).stdout)["id"]
    wanted = {kind.strip() for kind in (os.getenv("QUICKSTART_ADOPT") or "").split(",") if kind.strip()}
    # Connected, and reporting the model servers to adopt (it finds them on its first look round).
    for attempt in range(90):
        try:
            found = api("GET", f"/api/machines/{machine}")
            if found.get("connected") and wanted <= {provider.get("type") for provider in found.get("providers", [])}:
                break
        except (OSError, ValueError, KeyError):
            pass
        time.sleep(2)
    else:
        print("provision: the machine never showed up in the fleet with its model servers", flush=True)
        return
    org = api("GET", "/api/auth/status")["identity"]["tenant"]
    for kind in filter(None, (os.getenv("QUICKSTART_ADOPT") or "").split(",")):
        try:
            api("POST", f"/api/machines/{machine}/providers/adopt", {"type": kind.strip()})
            print(f"provision: {kind} is now a provider of {org}", flush=True)
        except OSError as error:
            # 409: adopted on an earlier start.
            print(f"provision: {kind} not adopted ({error})", flush=True)
    features = [feature.strip() for feature in (os.getenv("QUICKSTART_GRANT") or "").split(",") if feature.strip()]
    if features:
        api("PUT", f"/api/machines/{machine}/access", {"access": {feature: {"orgs": [org], "teams": []} for feature in features}})
        print(f"provision: everyone in {org} may use {', '.join(features)}", flush=True)


if __name__ == "__main__":
    provision() if sys.argv[1:] == ["provision"] else main()
