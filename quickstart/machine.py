"""Local demo provisioning through the issuer's normal CSRF-bound device flow."""
from __future__ import annotations

from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from urllib.parse import urlencode
from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener

API = "http://localhost:19999"
ISSUER = "http://localhost:20000"
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
        "username": "admin", "password": password, "approve": "yes",
    }, origin=True)
    # Approval has already completed, so the first token poll can succeed immediately.
    token = json.loads(browser.request("/token", {
        "client_id": CLIENT, "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device["device_code"],
    }))
    if not token.get("access_token") or not token.get("refresh_token") or token.get("token_type", "").lower() != "bearer":
        raise RuntimeError("Issuer did not return rotating OAuth credentials")
    entry = {
        "url": API, "user": "admin", "access_token": token["access_token"],
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
    print(f"Signed {path.parent.name} in as admin", flush=True)


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
    for arguments in (
        ["name", socket.gethostname()], ["address", socket.gethostname()],
        ["launches", "on"], ["roots", "add", "/workspace"],
        ["commands", "on"], ["containers", "off"], ["servers", "off"],
    ):
        subprocess.run(["agent-machine", *arguments], check=True)
    os.execvp("agent-machine", ["agent-machine", "run"])


if __name__ == "__main__":
    main()
