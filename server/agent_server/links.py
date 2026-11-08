"""Links: this server joined to another server, as a very restricted peer.

A link is the trusted channel two servers' site admins set up between them. Nothing crosses a
link by itself: organizations then open bridges over it (bridges.py), and only what both sides
let out and let in crosses. This module is the channel: who the other server is, how its
requests are signed and checked, and how they travel.

Who is who. Each server has a mesh id (a slug chosen once: AI_PROXY_MESH_ID, or set on Server
settings -> Links before the first link) and Ed25519 link keys (the secret link-keys, never in
the settings), published with the id at /.well-known/mesh-link. The issuer is not involved: no
account, service client or token is ever made for a peer, so a peer can never call an ordinary
/api/* route. It reaches only the peer routes (routes/links.py), which take nothing but link-key
signatures.

Setting one up. Each site admin makes a one-use code (msl_..., an hour) and enters the other's
(join), after checking the other's key fingerprint read out to them. Joining presents this
server's code, mesh id, address and keys to the other (POST /api/links/present, signed with the
key it presents). A side that has presented and been presented to has an active link; one side
alone does nothing. The link's id is a digest of both (mesh id, first key) pairs, so both sides
name it the same and presenting at the same moment makes one link.

Every request between the servers is signed (sign/verify): method, path, the link, this server's
mesh id, a time within SKEW, a nonce never seen in NONCE_TTL (in cluster keys, so every process
sees them), the body's digest and the assertion bridges.py makes (who, which resource, which
policies). The receiving side checks all of it before anything else.

How they travel. A side that reached the other over HTTPS while joining dials the link socket
(WS /api/links/connect), authenticated by a challenge signed both ways and re-signed every
RESIGN_SECONDS, and sends its requests to the other's address directly. The other side, which
may not be able to reach it (NAT), sends its requests over that socket instead (Channel): each
one is the same signed HTTP request, framed, and the receiving side runs it through its own app,
so it meets exactly the checks a direct request does. WebSockets (a terminal viewed across) are
carried the same way. A present the other side couldn't deliver directly waits for the socket.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import random
import secrets
import struct
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable
from urllib.parse import unquote, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from sqlalchemy import select

from . import routing
from .db import Database, kv, schema

LOGGER = logging.getLogger("agent-proxy")

# The protocol between linked servers, apart from the client and machine PROTOCOL (version.py):
# two servers on different releases still link while they share a link_protocol.
LINK_PROTOCOL = 1
MIN_LINK_PROTOCOL = 1
WELL_KNOWN = "/.well-known/mesh-link"
PEER_PREFIX = "/api/links/relay/"
STATES = ("pending", "active", "paused")
# What a link policy lets out or in, by kind (bridges.py names the items).
KINDS = ("provider", "mcp", "agent", "launch", "terminal")

CODE_LIFETIME = 3600.0
PENDING_SECONDS = 7 * 86400.0
WRONG_CODES = 20
WRONG_CODES_WINDOW = 600.0
SKEW = 30.0
NONCE_TTL = 90.0
PING_SECONDS = 20.0
SILENT_SECONDS = 60.0
RESIGN_SECONDS = 60.0
RECHECK_SECONDS = 15.0
BACKOFF_MAX = 60.0
KEY_OVERLAP = 24 * 3600.0
OPEN_TIMEOUT = 10.0
# Requests per link and second (burst), and requests carried over its socket at once.
RATE = 50.0
BURST = 200.0
MAX_STREAMS = 64
FRAME = 256 * 1024

CLOSE_UNAUTHORIZED = 4401
CLOSE_PAUSED = 4403
CLOSE_POLICY = 4409
CLOSE_REMOVED = 4410
CLOSE_PROTOCOL = 4426

SECRET = "link-keys"
MESH_META = "mesh_id"


class Refused(Exception):
    """A peer's request refused, with the status to answer (401 signature, 403 not permitted, 409 stale policy, ...)."""

    def __init__(self, status: int, detail: str, extra: dict[str, Any] | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.extra = extra or {}


class LinkDown(Exception):
    """The other server can't be reached now."""


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def _unb64(text: str) -> bytes:
    return base64.b64decode(text.encode(), validate=True)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def kid_of(public: str) -> str:
    return hashlib.sha256(_unb64(public)).hexdigest()[:16]


def fingerprint(public: str) -> str:
    """What two site admins read out to each other: the key's digest, in groups of four."""
    value = hashlib.sha256(_unb64(public)).hexdigest()[:32]
    return " ".join(value[index:index + 4] for index in range(0, len(value), 4))


def _public_key(public: str) -> Ed25519PublicKey:
    return Ed25519PublicKey.from_public_bytes(_unb64(public))


def verify_signature(public: str, signature: str, data: bytes) -> bool:
    try:
        _public_key(public).verify(_unb64(signature), data)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def link_id(one: tuple[str, str], other: tuple[str, str]) -> str:
    """Both servers' (mesh id, first key) pairs, sorted: the same on each side."""
    pairs = sorted([list(one), list(other)])
    return "lnk_" + hashlib.sha256(canonical(pairs)).hexdigest()[:24]


class KeyRing:
    """This server's link keys: the newest signs; an older one stays valid for KEY_OVERLAP after a rotation."""

    def __init__(self, db: Database):
        self.db = db
        self._keys: list[dict[str, Any]] | None = None

    def _load(self) -> list[dict[str, Any]]:
        if self._keys is None:
            with self.db.write() as connection:
                stored = kv.get(connection, schema.secrets, SECRET)
                keys = json.loads(bytes(stored).decode()) if stored is not None else []
                if not keys:
                    keys = [self._new()]
                    kv.put(connection, schema.secrets, SECRET, json.dumps(keys).encode())
            self._keys = keys
        now = time.time()
        return [key for key in self._keys if not key.get("until") or key["until"] > now]

    def reload(self) -> None:
        self._keys = None

    @staticmethod
    def _new() -> dict[str, Any]:
        private = Ed25519PrivateKey.generate()
        raw = private.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw, serialization.NoEncryption())
        public = _b64(private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw))
        return {"kid": kid_of(public), "private": _b64(raw), "public": public, "created": time.time(), "until": None}

    def _save(self, keys: list[dict[str, Any]]) -> None:
        with self.db.write() as connection:
            kv.put(connection, schema.secrets, SECRET, json.dumps(keys).encode())
        self._keys = keys

    def current(self) -> dict[str, Any]:
        keys = [key for key in self._load() if not key.get("until")]
        return max(keys, key=lambda key: key["created"]) if keys else self._load()[-1]

    def public(self) -> list[dict[str, Any]]:
        return [{"kid": key["kid"], "public": key["public"], "until": key.get("until")} for key in self._load()]

    def first(self) -> dict[str, Any]:
        """The key a link's id is made from: the oldest still held."""
        return min(self._load(), key=lambda key: key["created"])

    def sign(self, data: bytes, kid: str | None = None) -> tuple[str, str]:
        key = next((item for item in self._load() if item["kid"] == kid), None) if kid else None
        key = key or self.current()
        private = Ed25519PrivateKey.from_private_bytes(_unb64(key["private"]))
        return key["kid"], _b64(private.sign(data))

    def rotate(self) -> dict[str, Any]:
        keys = self._load()
        now = time.time()
        for key in keys:
            if not key.get("until"):
                key["until"] = now + KEY_OVERLAP
        new = self._new()
        self._save([*keys, new])
        return new

    def remove(self, kid: str) -> bool:
        keys = self._load()
        kept = [key for key in keys if key["kid"] != kid]
        if len(kept) == len(keys) or not kept:
            return False
        self._save(kept)
        return True


@dataclass
class Link:
    id: str
    peer: str
    url: str
    state: str
    data: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    @property
    def keys(self) -> list[dict[str, Any]]:
        now = time.time()
        return [key for key in self.data.get("keys") or [] if not key.get("until") or key["until"] > now]

    def key(self, kid: str) -> str | None:
        for key in self.keys:
            if key.get("kid") == kid:
                return str(key["public"])
        return None

    @property
    def complete(self) -> bool:
        return bool(self.data.get("we_presented") and self.data.get("they_presented"))

    def public(self, connected: bool) -> dict[str, Any]:
        keys = self.keys
        return {
            "id": self.id, "peer": self.peer, "url": self.url, "state": self.state, "connected": connected,
            "we_presented": bool(self.data.get("we_presented")), "they_presented": bool(self.data.get("they_presented")),
            "dials": bool(self.data.get("dials")), "waiting": bool(self.data.get("outgoing")),
            "fingerprint": fingerprint(keys[0]["public"]) if keys else None,
            "keys": [{"kid": key["kid"], "fingerprint": fingerprint(key["public"]), "until": key.get("until")} for key in keys],
            "created_at": self.created_at, "updated_at": self.updated_at,
            "link_protocol": self.data.get("link_protocol"),
        }


@dataclass
class Peer:
    """A request a peer signed, checked: which link it came over and what it asserts."""

    link: Link
    assertion: dict[str, Any]


class Links:
    """Links, their keys and policies, and the transport between linked servers."""

    def __init__(self, db: Database, settings: Any, cluster: Any):
        self.db = db
        self.settings = settings
        self.cluster = cluster
        self.keys = KeyRing(db)
        self.links: dict[str, Link] = {}
        self.channels: dict[str, Channel] = {}
        self.client: httpx.AsyncClient | None = None
        # The app (create_app's), which requests carried over a link socket run through.
        self.app: Any = None
        self.runtime: Any = None
        self._buckets: dict[str, tuple[float, float]] = {}
        self._wrong: dict[str, list[float]] = {}
        self._dialers: dict[str, asyncio.Task[Any]] = {}
        self._closing = False
        # Called with (scope, id) when a policy or catalogue may have changed (bridges.py listens).
        self.listeners: list[Callable[[str, str], Any]] = []
        self.reload()
        if cluster is not None:
            cluster.on("bridge.changed", lambda data, _: self._heard(data))

    # Who this server is

    @property
    def mesh_id(self) -> str | None:
        import os

        value = os.getenv("AI_PROXY_MESH_ID")
        if value:
            return value
        with self.db.read() as connection:
            stored = kv.get_meta(connection, MESH_META)
        return str(stored) if stored else None

    def set_mesh_id(self, value: str) -> None:
        import os

        if os.getenv("AI_PROXY_MESH_ID"):
            raise ValueError("AI_PROXY_MESH_ID sets this server's mesh id")
        if not routing.MESH_ID.fullmatch(value or ""):
            raise ValueError("A mesh id is lower case letters, digits and '-', up to 40, starting with a letter or digit")
        if self.links and value != self.mesh_id:
            raise ValueError("The mesh id can't change while the server has links: other servers know it by it")
        with self.db.write() as connection:
            kv.put_meta(connection, MESH_META, value)

    @property
    def url(self) -> str:
        import os

        return (os.getenv("AI_PROXY_MESH_URL") or self.settings.public_url or self.settings.url or "").rstrip("/")

    def well_known(self) -> dict[str, Any]:
        keys = self.keys.public()
        return {
            "mesh_id": self.mesh_id, "url": self.url, "keys": keys, "link_protocol": [MIN_LINK_PROTOCOL, LINK_PROTOCOL],
            "fingerprint": fingerprint(self.keys.first()["public"]),
        }

    def me(self) -> str:
        mesh = self.mesh_id
        if not mesh:
            raise ValueError("Give this server a mesh id first (Server settings -> Links, or AI_PROXY_MESH_ID)")
        return mesh

    # Stored links

    def reload(self) -> None:
        with self.db.read() as connection:
            rows = connection.execute(select(schema.links)).mappings().all()
        now = time.time()
        links = {}
        for row in rows:
            link = Link(row["id"], row["peer"], row["url"], row["state"], dict(row["data"] or {}), row["created_at"] or 0.0, row["updated_at"] or 0.0)
            if link.state == "pending" and not link.complete and now - link.created_at > PENDING_SECONDS:
                continue
            links[link.id] = link
        self.links = links

    def _save(self, link: Link) -> None:
        link.updated_at = time.time()
        values = {"id": link.id, "peer": link.peer, "url": link.url, "state": link.state, "data": link.data, "created_at": link.created_at, "updated_at": link.updated_at}
        with self.db.write() as connection:
            kv.upsert(connection, schema.links, values)
        self.links[link.id] = link
        self.changed("link", link.id)

    def remove(self, link_id: str) -> bool:
        link = self.links.pop(link_id, None)
        with self.db.write() as connection:
            connection.execute(schema.links.delete().where(schema.links.c.id == link_id))
            connection.execute(schema.peer_policies.delete().where((schema.peer_policies.c.scope == "link") & (schema.peer_policies.c.scope_id == link_id)))
        channel = self.channels.pop(link_id, None)
        if channel is not None:
            asyncio.ensure_future(channel.close(CLOSE_REMOVED, "The link was removed"))
        task = self._dialers.pop(link_id, None)
        if task is not None:
            task.cancel()
        self.changed("link", link_id)
        return link is not None

    def set_state(self, link: Link, state: str) -> None:
        link.state = state
        self._save(link)
        if state == "paused":
            channel = self.channels.pop(link.id, None)
            if channel is not None:
                asyncio.ensure_future(channel.close(CLOSE_PAUSED, "The link is paused"))
        else:
            self.ensure_dialing(link)

    def changed(self, scope: str, scope_id: str, local: bool = True) -> None:
        for listener in list(self.listeners):
            try:
                listener(scope, scope_id)
            except Exception:  # noqa: BLE001 - one listener's failure mustn't stop the others
                LOGGER.exception("link listener failed")
        if local and self.cluster is not None:
            self.cluster.publish("bridge.changed", {"scope": scope, "id": scope_id})

    def _heard(self, data: dict[str, Any]) -> None:
        self.reload()
        self.keys.reload()
        self.changed(str(data.get("scope") or ""), str(data.get("id") or ""), local=False)

    def connected(self, link_id: str) -> bool:
        if link_id in self.channels:
            return True
        return bool(self.cluster is not None and self.cluster.scaled and self.cluster.get(f"link:socket:{link_id}"))

    def public(self) -> dict[str, Any]:
        mesh = self.well_known()
        return {
            "mesh": {"id": mesh["mesh_id"], "url": mesh["url"], "fingerprint": mesh["fingerprint"], "link_protocol": LINK_PROTOCOL,
                     "keys": [{"kid": key["kid"], "fingerprint": fingerprint(key["public"]), "until": key["until"]} for key in mesh["keys"]]},
            "links": [link.public(self.connected(link.id)) for link in sorted(self.links.values(), key=lambda item: item.peer)],
        }

    # Codes

    def make_code(self, kind: str, by: str | None, *, org: str | None = None, link: str | None = None, lifetime: float = CODE_LIFETIME) -> tuple[str, float]:
        code = ("msl_" if kind == "link" else "msb_") + secrets.token_urlsafe(18)
        expires = time.time() + lifetime
        with self.db.write() as connection:
            connection.execute(schema.peer_codes.delete().where(schema.peer_codes.c.exp < time.time()))
            connection.execute(schema.peer_codes.insert().values(
                digest=hashlib.sha256(code.encode()).hexdigest(), kind=kind, org=org, link=link, exp=expires, created_by=by, used_at=None,
            ))
        return code, expires

    def find_code(self, code: str, kind: str) -> dict[str, Any] | None:
        if not isinstance(code, str) or len(code) > 100:
            return None
        table = schema.peer_codes
        with self.db.read() as connection:
            row = connection.execute(select(table).where(table.c.digest == hashlib.sha256(code.encode()).hexdigest())).mappings().first()
        if row is None or row["kind"] != kind or row["used_at"] is not None or row["exp"] < time.time():
            return None
        return dict(row)

    def spend_code(self, code: str, kind: str) -> dict[str, Any] | None:
        """The code's row, now spent: once only, even with two presents at the same moment."""
        table = schema.peer_codes
        key = hashlib.sha256(code.encode()).hexdigest() if isinstance(code, str) and len(code) <= 100 else ""
        with self.db.write() as connection:
            row = connection.execute(select(table).where(table.c.digest == key).with_for_update()).mappings().first()
            if row is None or row["kind"] != kind or row["used_at"] is not None or row["exp"] < time.time():
                return None
            connection.execute(table.update().where(table.c.digest == key).values(used_at=time.time()))
        return dict(row)

    def codes(self, kind: str, org: str | None = None) -> list[dict[str, Any]]:
        table = schema.peer_codes
        with self.db.read() as connection:
            query = select(table).where((table.c.kind == kind) & (table.c.used_at.is_(None)) & (table.c.exp > time.time()))
            if org is not None:
                query = query.where(table.c.org == org)
            rows = connection.execute(query).mappings().all()
        return [{"id": row["digest"][:12], "org": row["org"], "link": row["link"], "expires_at": row["exp"], "created_by": row["created_by"]} for row in rows]

    def revoke_code(self, code_id: str, kind: str, org: str | None = None) -> bool:
        table = schema.peer_codes
        with self.db.write() as connection:
            rows = connection.execute(select(table.c.digest, table.c.org).where(table.c.kind == kind)).all()
            for row_digest, row_org in rows:
                if row_digest.startswith(code_id) and len(code_id) >= 8 and (org is None or row_org == org):
                    connection.execute(table.delete().where(table.c.digest == row_digest))
                    return True
        return False

    def wrong_code(self, address: str) -> bool:
        """Counts a wrong code from this address; True once it has sent too many lately."""
        now = time.time()
        if self.cluster is not None and self.cluster.scaled:
            window = int(now // WRONG_CODES_WINDOW)
            return self.cluster.incr(f"link:wrong:{address}:{window}", 1, WRONG_CODES_WINDOW) > WRONG_CODES
        recent = [at for at in self._wrong.get(address, []) if now - at < WRONG_CODES_WINDOW]
        recent.append(now)
        self._wrong[address] = recent
        return len(recent) > WRONG_CODES

    def too_many_wrong(self, address: str) -> bool:
        now = time.time()
        if self.cluster is not None and self.cluster.scaled:
            value = self.cluster.get(f"link:wrong:{address}:{int(now // WRONG_CODES_WINDOW)}")
            return int(value or 0) > WRONG_CODES
        return len([at for at in self._wrong.get(address, []) if now - at < WRONG_CODES_WINDOW]) > WRONG_CODES

    # Setting a link up

    async def inspect(self, url: str) -> dict[str, Any]:
        """What another server says it is, at url (/.well-known/mesh-link), for the site admin to check."""
        url = _checked_url(url)
        try:
            response = await self._http().get(url + WELL_KNOWN, timeout=OPEN_TIMEOUT)
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ValueError(f"Couldn't read {url}{WELL_KNOWN}: {exc}") from exc
        if not isinstance(data, dict) or not routing.MESH_ID.fullmatch(str(data.get("mesh_id") or "")):
            raise ValueError(f"{url} isn't an AI Control Mesh server with a mesh id")
        keys = [key for key in data.get("keys") or [] if isinstance(key, dict) and isinstance(key.get("public"), str)]
        if not keys:
            raise ValueError(f"{url} publishes no link keys")
        supported = data.get("link_protocol") or [1, 1]
        if not (isinstance(supported, list) and len(supported) == 2 and supported[0] <= LINK_PROTOCOL and supported[1] >= MIN_LINK_PROTOCOL):
            raise ValueError(f"{url} speaks link protocol {supported}; this server speaks {MIN_LINK_PROTOCOL}..{LINK_PROTOCOL}")
        if data["mesh_id"] == self.mesh_id:
            raise ValueError("That's this server")
        keys = [{"kid": kid_of(key["public"]), "public": key["public"], "until": key.get("until")} for key in keys]
        # The key a link is made from is the oldest it holds (KeyRing.first), which it lists first.
        return {"mesh_id": data["mesh_id"], "url": url, "keys": keys, "fingerprint": fingerprint(keys[0]["public"]), "link_protocol": min(LINK_PROTOCOL, int(supported[1]))}

    async def join(self, code: str, by: str | None, *, url: str | None = None, link_id_: str | None = None, expected: str | None = None) -> Link:
        """This site admin entered the other server's code: present ourselves to it. url for a server not met yet
        (its fingerprint must be the one the admin confirmed), or the id of a link it already presented over."""
        me = self.me()
        if link_id_:
            link = self.links.get(link_id_)
            if link is None or not link.keys:
                raise ValueError("No such link")
            peer_keys, peer_url, peer = link.keys, link.url, link.peer
            protocol = link.data.get("link_protocol") or LINK_PROTOCOL
        else:
            info = await self.inspect(url or "")
            if not expected or _plain(expected) != _plain(info["fingerprint"]):
                raise ValueError("The fingerprint doesn't match what the other server's admin read out: check it with them")
            peer_keys, peer_url, peer, protocol = info["keys"], info["url"], info["mesh_id"], info["link_protocol"]
            link = None
        mine = self.keys.first()
        key_id = link_id((me, mine["public"]), (peer, peer_keys[0]["public"]))
        existing = self.links.get(key_id)
        if existing is not None and existing.keys and existing.keys[0]["public"] != peer_keys[0]["public"]:
            raise ValueError("That server presented a different key before: remove the link and set it up again")
        link = existing or link or Link(key_id, peer, peer_url, "pending", {}, time.time())
        if link.id != key_id:
            raise ValueError("That server's key changed since it presented: remove the link and set it up again")
        link.url = peer_url
        link.data.setdefault("keys", peer_keys)
        link.data["link_protocol"] = protocol
        link.data["joined_by"] = by
        body = {"code": code, "mesh_id": me, "url": self.url, "keys": self.keys.public(), "link_protocol": LINK_PROTOCOL}
        self._save(link)
        delivered = False
        try:
            response = await self.send(link, "POST", "/api/links/present", json_body=body, direct=True, presenting=True)
            delivered = True
        except LinkDown:
            response = None
        if delivered and response is not None:
            if response.status_code >= 400:
                raise ValueError(_detail(response, "The other server refused the code: check it and that it hasn't expired"))
            link.data["we_presented"] = True
            link.data["reachable"] = True
            link.data["dials"] = True
            link.data.pop("outgoing", None)
        elif link.id in self.channels:
            response = await self.send(link, "POST", "/api/links/present", json_body=body, presenting=True)
            if response.status_code >= 400:
                raise ValueError(_detail(response, "The other server refused the code"))
            link.data["we_presented"] = True
        else:
            # It can't be reached from here: the code goes over its link socket when it dials.
            link.data["outgoing"] = {"code": code}
        self._activate(link)
        self._save(link)
        self.ensure_dialing(link)
        return link

    def presented(self, scope: dict[str, Any], headers: Any, raw: bytes) -> Link:
        """The other server presented its code, mesh id, address and keys (POST /api/links/present), signed with
        the key it presents. Raises Refused."""
        me = self.me()
        try:
            body = json.loads(raw or b"{}")
        except ValueError as exc:
            raise Refused(400, "Send a JSON object") from exc
        if not isinstance(body, dict):
            raise Refused(400, "Send a JSON object")
        peer = str(body.get("mesh_id") or "")
        keys = [{"kid": kid_of(str(key["public"])), "public": str(key["public"]), "until": key.get("until")} for key in body.get("keys") or [] if isinstance(key, dict) and key.get("public")]
        if not routing.MESH_ID.fullmatch(peer) or not keys or peer == me or headers.get("mesh-from") != peer or headers.get("mesh-link") != "new":
            raise Refused(400, "Present a mesh id and keys")
        kid, _, sig = (headers.get("mesh-signature") or "").partition(",")
        public = next((key["public"] for key in keys if key["kid"] == kid.removeprefix("kid=")), None)
        date, nonce, content = headers.get("mesh-date") or "", headers.get("mesh-nonce") or "", headers.get("content-digest") or ""
        if not date.isdigit() or abs(time.time() - int(date)) > SKEW or content != "sha-256=:" + _b64(hashlib.sha256(raw or b"").digest()) + ":":
            raise Refused(401, "The request's date or digest is wrong")
        signed = _signed_text(scope.get("method", "POST"), _target(scope), "new", peer, date, nonce, content, "")
        if public is None or not verify_signature(public, sig.removeprefix("sig="), signed) or not self._fresh_nonce("new", nonce):
            raise Refused(401, "That isn't signed by the key presented")
        over_socket = (scope.get("state") or {}).get("link_channel") is not None
        key_id = link_id((me, self.keys.first()["public"]), (peer, keys[0]["public"]))
        existing = self.links.get(key_id)
        if existing is not None and existing.keys and existing.keys[0]["public"] != keys[0]["public"]:
            raise Refused(401, "That server presented a different key before")
        if existing is not None and existing.data.get("they_presented"):
            return existing
        if self.spend_code(str(body.get("code") or ""), "link") is None:
            raise Refused(403, "The code is wrong, used or expired")
        link = existing or Link(key_id, peer, _checked_url(str(body.get("url") or "")), "pending", {}, time.time())
        link.data["keys"] = keys
        link.data["they_presented"] = True
        link.data["link_protocol"] = min(LINK_PROTOCOL, int(body.get("link_protocol") or 1))
        if body.get("url"):
            link.url = _checked_url(str(body["url"]))
        if link.data.get("dials") and not over_socket:
            # It joined second and reached us: it dials (the side that joined second does, unless it can't).
            link.data["dials"] = False
            task = self._dialers.pop(link.id, None)
            if task is not None:
                task.cancel()
        self._activate(link)
        self._save(link)
        return link

    def _activate(self, link: Link) -> None:
        if link.complete and link.state == "pending":
            link.state = "active"

    # Policies, signed by each side for itself (bridges.py uses the same for bridges)

    def sign_policy(self, scope: str, scope_id: str, document: dict[str, Any]) -> dict[str, Any]:
        current = self.policy(scope, scope_id, "local")
        version = int(current["document"]["version"]) + 1 if current else 1
        document = {**document, "version": version, "server": self.me(), "issued_at": time.time()}
        value = digest(document)
        kid, signature = self.keys.sign(("mesh-policy-v1\n" + value).encode())
        envelope = {"document": document, "digest": value, "kid": kid, "signature": signature}
        self._store_policy(scope, scope_id, "local", envelope)
        return envelope

    def accept_policy(self, link: Link, scope: str, scope_id: str, envelope: Any) -> bool:
        """A policy the other side signed for itself: kept if its signature is good and it's newer. Raises Refused."""
        if not isinstance(envelope, dict) or not isinstance(envelope.get("document"), dict):
            raise Refused(400, "Send a signed policy")
        document = envelope["document"]
        public = link.key(str(envelope.get("kid") or ""))
        if public is None or digest(document) != envelope.get("digest") or not verify_signature(public, str(envelope.get("signature") or ""), ("mesh-policy-v1\n" + str(envelope["digest"])).encode()):
            raise Refused(401, "The policy isn't signed by the other server's link key")
        if document.get("server") != link.peer:
            raise Refused(403, "That policy is another server's")
        current = self.policy(scope, scope_id, "peer")
        if current is not None and int(document.get("version") or 0) <= int(current["document"].get("version") or 0):
            return False
        self._store_policy(scope, scope_id, "peer", envelope)
        return True

    def _store_policy(self, scope: str, scope_id: str, side: str, envelope: dict[str, Any]) -> None:
        with self.db.write() as connection:
            connection.execute(schema.peer_policies.insert().values(
                scope=scope, scope_id=scope_id, side=side, version=int(envelope["document"]["version"]),
                document=envelope["document"], digest=envelope["digest"], signature=envelope["signature"], kid=envelope["kid"], created_at=time.time(),
            ))
        self.changed(scope, scope_id)

    def policy(self, scope: str, scope_id: str, side: str) -> dict[str, Any] | None:
        table = schema.peer_policies
        with self.db.read() as connection:
            row = connection.execute(
                select(table).where((table.c.scope == scope) & (table.c.scope_id == scope_id) & (table.c.side == side)).order_by(table.c.version.desc()).limit(1)
            ).mappings().first()
        if row is None:
            return None
        return {"document": row["document"], "digest": row["digest"], "kid": row["kid"], "signature": row["signature"]}

    def link_policy(self, link: Link, side: str) -> dict[str, Any]:
        """A side's link policy document; nothing out or in until its site admin sets one."""
        envelope = self.policy("link", link.id, side)
        return envelope["document"] if envelope else {"out": [], "in": [], "orgs": []}

    def set_link_policy(self, link: Link, out: Any, into: Any, orgs: Any) -> dict[str, Any]:
        def kinds(value: Any, what: str) -> list[str]:
            items = sorted({str(item) for item in value or []})
            unknown = [item for item in items if item not in KINDS]
            if unknown:
                raise ValueError(f"{what}: {', '.join(unknown)} isn't one of {', '.join(KINDS)}")
            return items

        allowed_orgs = sorted({str(item) for item in orgs or []})
        envelope = self.sign_policy("link", link.id, {"type": "link", "link": link.id, "out": kinds(out, "Out"), "in": kinds(into, "In"), "orgs": allowed_orgs})
        return envelope

    @staticmethod
    def org_allowed(policy: dict[str, Any], org: str) -> bool:
        orgs = policy.get("orgs") or []
        return "*" in orgs or org in orgs

    # Signing and checking requests

    def signed_headers(self, link: Link | None, method: str, target: str, body: bytes, assertion: dict[str, Any] | None = None, kid: str | None = None) -> dict[str, str]:
        link_name = link.id if link else "new"
        date = str(int(time.time()))
        nonce = secrets.token_urlsafe(16)
        content = "sha-256=:" + _b64(hashlib.sha256(body or b"").digest()) + ":"
        encoded = base64.urlsafe_b64encode(canonical(assertion)).decode().rstrip("=") if assertion is not None else ""
        me = self.me()
        kid, signature = self.keys.sign(_signed_text(method, target, link_name, me, date, nonce, content, encoded), kid)
        headers = {
            "mesh-link": link_name, "mesh-from": me, "mesh-date": date, "mesh-nonce": nonce, "content-digest": content,
            "mesh-signature": f"kid={kid},sig={signature}",
        }
        if encoded:
            headers["mesh-assertion"] = encoded
        return headers

    def verify(self, scope: dict[str, Any], headers: Any, body: bytes) -> Peer:
        """Checks a peer's signed request (raises Refused): the link, its key, the time, the nonce, the body, the signature."""
        link_name = headers.get("mesh-link") or ""
        link = self.links.get(link_name)
        if link is None:
            raise Refused(401, "Unknown link")
        channel_link = (scope.get("state") or {}).get("link_channel")
        if channel_link is not None and channel_link != link.id:
            raise Refused(401, "That request came over another link")
        if link.state == "paused":
            raise Refused(403, "The link is paused")
        if headers.get("mesh-from") != link.peer:
            raise Refused(401, "That isn't the linked server")
        kid, _, sig = (headers.get("mesh-signature") or "").partition(",")
        public = link.key(kid.removeprefix("kid="))
        date = headers.get("mesh-date") or ""
        nonce = headers.get("mesh-nonce") or ""
        content = headers.get("content-digest") or ""
        encoded = headers.get("mesh-assertion") or ""
        if public is None:
            raise Refused(401, "Not signed with a current link key")
        if not date.isdigit() or abs(time.time() - int(date)) > SKEW:
            raise Refused(401, "The request's date is too far from this server's clock")
        if content != "sha-256=:" + _b64(hashlib.sha256(body or b"").digest()) + ":":
            raise Refused(401, "The body doesn't match its digest")
        target = _target(scope)
        if not verify_signature(public, sig.removeprefix("sig="), _signed_text(scope.get("method", "GET"), target, link.id, link.peer, date, nonce, content, encoded)):
            raise Refused(401, "Bad signature")
        if not nonce or len(nonce) > 64 or not self._fresh_nonce(link.id, nonce):
            raise Refused(401, "That request was already seen")
        if not self._within_rate(link.id):
            raise Refused(429, "Too many requests over this link")
        assertion: dict[str, Any] = {}
        if encoded:
            try:
                assertion = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            except ValueError as exc:
                raise Refused(400, "Bad assertion") from exc
            if not isinstance(assertion, dict):
                raise Refused(400, "Bad assertion")
            now = time.time()
            if assertion.get("link") != link.id or assertion.get("from") != link.peer or assertion.get("to") != self.mesh_id:
                raise Refused(401, "The assertion is for another link")
            if not isinstance(assertion.get("exp"), (int, float)) or assertion["exp"] + SKEW < now or float(assertion.get("iat") or 0) - SKEW > now:
                raise Refused(401, "The assertion expired")
        return Peer(link, assertion)

    def _fresh_nonce(self, link_name: str, nonce: str) -> bool:
        if self.cluster is not None:
            return bool(self.cluster.set(f"link:nonce:{link_name}:{nonce}", "1", NONCE_TTL, nx=True))
        return True

    def _within_rate(self, link_name: str) -> bool:
        now = time.monotonic()
        tokens, at = self._buckets.get(link_name, (BURST, now))
        tokens = min(BURST, tokens + (now - at) * RATE)
        if tokens < 1:
            self._buckets[link_name] = (tokens, now)
            return False
        self._buckets[link_name] = (tokens - 1, now)
        return True

    # Sending requests to the other server

    def _http(self) -> httpx.AsyncClient:
        if self.runtime is not None and self.runtime.client is not None:
            return self.runtime.client
        if self.client is None:
            from .runtime import upstream_client

            self.client = upstream_client(timeout=httpx.Timeout(600, connect=OPEN_TIMEOUT, pool=30))
        return self.client

    def route(self, link: Link, *, direct: bool = False) -> tuple[httpx.AsyncClient, str]:
        """Which way a request to the peer goes: its address (when this side reached it while joining) or its
        link socket. Raises LinkDown when neither works."""
        if direct or (link.data.get("reachable") and link.url):
            return self._http(), link.url
        channel = self.channels.get(link.id)
        if channel is not None:
            return channel.client, "http://link.invalid"
        raise LinkDown(f"{link.peer} can't be reached: its link socket isn't connected")

    def prepare(self, link: Link, method: str, target: str, body: bytes, assertion: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> tuple[httpx.AsyncClient, str, dict[str, str]]:
        """The client, URL and headers of one signed request to the peer (target is its path and query)."""
        if link.state != "active":
            raise LinkDown(f"The link to {link.peer} is {link.state}")
        client, base = self.route(link)
        return client, base + target, {**(headers or {}), **self.signed_headers(link, method, target, body, assertion)}

    async def send(
        self, link: Link, method: str, target: str, *, json_body: Any = None, body: bytes = b"", assertion: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None, stream: bool = False, direct: bool = False, presenting: bool = False, timeout: float | None = 30.0,
        kid: str | None = None,
    ) -> httpx.Response:
        if json_body is not None:
            body = canonical(json_body)
            headers = {**(headers or {}), "content-type": "application/json"}
        if not presenting and link.state != "active":
            raise LinkDown(f"The link to {link.peer} is {link.state}")
        try:
            client, base = self.route(link, direct=direct)
        except LinkDown:
            raise
        signed = self.signed_headers(None if presenting else link, method, target, body, assertion, kid)
        request = client.build_request(method, base + target, content=body or None, headers={**(headers or {}), **signed}, timeout=timeout)
        try:
            return await client.send(request, stream=stream)
        except httpx.HTTPError as exc:
            raise LinkDown(f"{link.peer} didn't answer: {exc or type(exc).__name__}") from exc

    async def get_json(self, link: Link, target: str, assertion: dict[str, Any] | None = None) -> Any:
        response = await self.send(link, "GET", target, assertion=assertion)
        if response.status_code >= 400:
            raise LinkDown(_detail(response, f"{link.peer} answered {response.status_code}"))
        return response.json()

    async def post_json(self, link: Link, target: str, value: Any, assertion: dict[str, Any] | None = None) -> httpx.Response:
        return await self.send(link, "POST", target, json_body=value, assertion=assertion)

    async def websocket(self, link: Link, target: str, assertion: dict[str, Any] | None = None) -> "Socket":
        """A WebSocket to the peer's target: dialled directly, or carried over the link socket."""
        if link.state != "active":
            raise LinkDown(f"The link to {link.peer} is {link.state}")
        headers = self.signed_headers(link, "GET", target, b"", assertion)
        if link.data.get("reachable") and link.url:
            import websockets

            url = link.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + target
            try:
                connection = await websockets.connect(url, additional_headers=headers, max_size=None, open_timeout=OPEN_TIMEOUT, proxy=None)
            except Exception as exc:  # noqa: BLE001 - refused or unreachable
                raise LinkDown(f"{link.peer} didn't take the connection: {exc}") from exc
            return ClientSocket(connection)
        channel = self.channels.get(link.id)
        if channel is None:
            raise LinkDown(f"{link.peer} can't be reached: its link socket isn't connected")
        return await channel.open_websocket(target, headers)

    async def notify(self, link: Link, scope: str, scope_id: str) -> None:
        """Tell the peer one of this side's policies changed, so it fetches them now."""
        try:
            response = await self.post_json(link, PEER_PREFIX + "changed", {"scope": scope, "id": scope_id})
            if response.status_code >= 400:
                LOGGER.warning("%s didn't take news of a change: %s", link.peer, _detail(response, str(response.status_code)))
        except LinkDown as exc:
            LOGGER.info("couldn't tell %s about a change: %s", link.peer, exc)

    async def announce_keys(self, link: Link, old_kid: str) -> None:
        """After a rotation: the new key to the peer, in a request signed with the old one (which it knows), with a
        proof made by the new key itself."""
        new = self.keys.current()
        _, proof = self.keys.sign(_key_proof(link.id, new["kid"]), new["kid"])
        body = {"keys": self.keys.public(), "kid": new["kid"], "proof": proof}
        try:
            response = await self.send(link, "POST", PEER_PREFIX + "keys", json_body=body, kid=old_kid)
            if response.status_code >= 400:
                LOGGER.warning("%s didn't take the new link key: %s", link.peer, _detail(response, str(response.status_code)))
        except LinkDown as exc:
            LOGGER.warning("couldn't send %s the new link key: %s", link.peer, exc)

    def accept_keys(self, link: Link, body: dict[str, Any]) -> None:
        keys = {str(key.get("kid")): key for key in body.get("keys") or [] if isinstance(key, dict) and key.get("public")}
        new = keys.get(str(body.get("kid") or ""))
        if new is None or kid_of(str(new["public"])) != body.get("kid"):
            raise Refused(400, "Send the new key")
        if not verify_signature(str(new["public"]), str(body.get("proof") or ""), _key_proof(link.id, str(body["kid"]))):
            raise Refused(401, "The new key didn't sign its proof")
        known = {key["kid"]: key for key in link.data.get("keys") or []}
        for kid, key in keys.items():
            if kid_of(str(key["public"])) == kid:
                known[kid] = {"kid": kid, "public": str(key["public"]), "until": key.get("until")}
        link.data["keys"] = list(known.values())
        self._save(link)

    # The link socket

    def ensure_dialing(self, link: Link) -> None:
        if self._closing or not link.data.get("dials") or link.state == "paused" or link.id in self._dialers:
            return
        if self.runtime is not None and "fleet" not in self.runtime.roles:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._dialers[link.id] = asyncio.ensure_future(self._dial_forever(link.id))

    def start(self) -> None:
        for link in list(self.links.values()):
            self.ensure_dialing(link)

    async def _dial_forever(self, link_name: str) -> None:
        delay = 1.0
        try:
            while not self._closing:
                link = self.links.get(link_name)
                if link is None or not link.data.get("dials") or link.state == "paused":
                    return
                if self.cluster is not None and self.cluster.scaled and not self.cluster.leader(f"link-dial:{link_name}", ttl=30):
                    await asyncio.sleep(10)
                    continue
                try:
                    await self._dial(link)
                    delay = 1.0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - the peer is down or refused; try again later
                    LOGGER.info("link socket to %s: %s", link.peer, exc or type(exc).__name__)
                await asyncio.sleep(delay + random.uniform(0, delay / 2))
                delay = min(BACKOFF_MAX, delay * 2)
        finally:
            self._dialers.pop(link_name, None)

    async def _dial(self, link: Link) -> None:
        import websockets

        url = link.url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/api/links/connect"
        async with websockets.connect(url, additional_headers={"mesh-link": link.id, "mesh-from": self.me()}, max_size=None, open_timeout=OPEN_TIMEOUT, proxy=None, ping_interval=None) as connection:
            socket = ClientSocket(connection)
            challenge = await socket.receive_json(OPEN_TIMEOUT)
            if challenge.get("type") != "challenge" or challenge.get("server") != link.peer:
                raise ConnectionError("not the linked server")
            client_nonce = secrets.token_urlsafe(16)
            hello = {"type": "hello", "link": link.id, "from": self.me(), "to": link.peer, "nonce": challenge.get("nonce"), "client_nonce": client_nonce,
                     "time": time.time(), "link_protocol": LINK_PROTOCOL}
            await socket.send_text(json.dumps(self._signed_message(hello)))
            welcome = await socket.receive_json(OPEN_TIMEOUT)
            if welcome.get("type") != "welcome" or welcome.get("nonce") != client_nonce or not self._message_valid(link, welcome):
                raise ConnectionError("the linked server's welcome isn't signed by its key")
            await self._hold(link, socket, dialled=True)

    async def accept(self, websocket: Any) -> None:
        """The other server dialled its link socket (WS /api/links/connect)."""
        await websocket.accept()
        link = self.links.get(websocket.headers.get("mesh-link") or "")
        if link is None or not link.data.get("they_presented") or websocket.headers.get("mesh-from") != link.peer:
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Unknown link")
            return
        if link.state == "paused":
            await websocket.close(code=CLOSE_PAUSED, reason="The link is paused")
            return
        socket = ServerSocket(websocket)
        nonce = secrets.token_urlsafe(16)
        await socket.send_text(json.dumps({"type": "challenge", "nonce": nonce, "server": self.me(), "link_protocol": [MIN_LINK_PROTOCOL, LINK_PROTOCOL]}))
        try:
            hello = await socket.receive_json(OPEN_TIMEOUT)
        except Exception:  # noqa: BLE001 - it went before saying hello
            return
        if hello.get("type") != "hello" or hello.get("nonce") != nonce or hello.get("link") != link.id or hello.get("to") != self.me() or not self._message_valid(link, hello):
            await websocket.close(code=CLOSE_UNAUTHORIZED, reason="Bad signature")
            return
        if not MIN_LINK_PROTOCOL <= int(hello.get("link_protocol") or 0) <= LINK_PROTOCOL:
            await websocket.close(code=CLOSE_PROTOCOL, reason="Link protocol not supported")
            return
        welcome = {"type": "welcome", "link": link.id, "from": self.me(), "to": link.peer, "nonce": hello.get("client_nonce"), "time": time.time()}
        await socket.send_text(json.dumps(self._signed_message(welcome)))
        client = getattr(websocket, "client", None)
        await self._hold(link, socket, dialled=False, client_host=getattr(client, "host", None) or "0.0.0.0")

    def _signed_message(self, message: dict[str, Any]) -> dict[str, Any]:
        kid, signature = self.keys.sign(b"mesh-socket-v1\n" + canonical(message))
        return {**message, "kid": kid, "signature": signature}

    def _message_valid(self, link: Link, message: dict[str, Any]) -> bool:
        body = {key: value for key, value in message.items() if key not in ("kid", "signature")}
        public = link.key(str(message.get("kid") or ""))
        return bool(
            public and message.get("from") == link.peer and abs(time.time() - float(message.get("time") or 0)) <= SKEW
            and verify_signature(public, str(message.get("signature") or ""), b"mesh-socket-v1\n" + canonical(body))
        )

    async def _hold(self, link: Link, socket: "Socket", *, dialled: bool, client_host: str = "0.0.0.0") -> None:
        channel = Channel(self, link.id, socket, dialled=dialled, client_host=client_host)
        old = self.channels.get(link.id)
        self.channels[link.id] = channel
        if self.cluster is not None and self.cluster.scaled:
            self.cluster.set(f"link:socket:{link.id}", self.cluster.pod_id, SILENT_SECONDS * 2)
        if old is not None:
            await old.close(4409, "Replaced by a newer link socket")
        LOGGER.info("link socket to %s open (%s)", link.peer, "dialled" if dialled else "accepted")
        self.changed("socket", link.id, local=False)
        keepers = [asyncio.ensure_future(self._keep(link.id, channel))]
        try:
            if link.data.get("outgoing"):
                asyncio.ensure_future(self._deliver_outgoing(link))
            await channel.run()
        finally:
            for task in keepers:
                task.cancel()
            if self.channels.get(link.id) is channel:
                self.channels.pop(link.id, None)
                if self.cluster is not None and self.cluster.scaled:
                    self.cluster.delete(f"link:socket:{link.id}")
            await channel.close(1000, "")
            LOGGER.info("link socket to %s closed", link.peer)
            self.changed("socket", link.id, local=False)

    async def _keep(self, link_name: str, channel: "Channel") -> None:
        """Pings, re-signs and re-checks the link while the socket is open."""
        last_ping = last_sign = last_check = time.monotonic()
        while True:
            await asyncio.sleep(1.0)
            now = time.monotonic()
            if now - channel.heard > SILENT_SECONDS:
                await channel.close(1011, "Nothing heard from the linked server")
                return
            if now - last_ping >= PING_SECONDS:
                last_ping = now
                await channel.send_control({"type": "ping"})
            if now - last_sign >= RESIGN_SECONDS:
                last_sign = now
                await channel.send_control(self._signed_message({"type": "auth", "link": link_name, "from": self.me(), "time": time.time(), "nonce": secrets.token_urlsafe(8)}))
            if now - last_check >= RECHECK_SECONDS:
                last_check = now
                link = self.links.get(link_name)
                if link is None:
                    await channel.close(CLOSE_REMOVED, "The link was removed")
                    return
                if link.state == "paused":
                    await channel.close(CLOSE_PAUSED, "The link is paused")
                    return
                if self.cluster is not None and self.cluster.scaled:
                    self.cluster.set(f"link:socket:{link_name}", self.cluster.pod_id, SILENT_SECONDS * 2)
                if now - channel.signed_at > RESIGN_SECONDS * 2 + SKEW:
                    await channel.close(CLOSE_UNAUTHORIZED, "The linked server stopped signing its socket")
                    return

    def control(self, channel: "Channel", message: dict[str, Any]) -> None:
        """A text message on a link socket."""
        kind = message.get("type")
        link = self.links.get(channel.link_id)
        if link is None:
            return
        if kind == "ping":
            asyncio.ensure_future(channel.send_control({"type": "pong"}))
        elif kind == "auth":
            if self._message_valid(link, message):
                channel.signed_at = time.monotonic()
            else:
                asyncio.ensure_future(channel.close(CLOSE_UNAUTHORIZED, "Bad signature"))

    async def _deliver_outgoing(self, link: Link) -> None:
        outgoing = link.data.get("outgoing") or {}
        body = {"code": outgoing.get("code"), "mesh_id": self.me(), "url": self.url, "keys": self.keys.public(), "link_protocol": LINK_PROTOCOL}
        try:
            response = await self.send(link, "POST", "/api/links/present", json_body=body, presenting=True)
        except LinkDown as exc:
            LOGGER.info("couldn't present to %s over its socket: %s", link.peer, exc)
            return
        if response.status_code < 400:
            link.data["we_presented"] = True
            link.data.pop("outgoing", None)
            self._activate(link)
            self._save(link)
        else:
            LOGGER.warning("%s refused the code entered for it: %s", link.peer, _detail(response, str(response.status_code)))
            link.data.pop("outgoing", None)
            self._save(link)

    async def close_all(self) -> None:
        """Before a restart or shutdown: end the link sockets; the dialling side reconnects."""
        self._closing = True
        for task in list(self._dialers.values()):
            task.cancel()
        self._dialers.clear()
        for channel in list(self.channels.values()):
            await channel.close(1012, "The server is restarting")
        self.channels.clear()
        if self.client is not None:
            await self.client.aclose()
            self.client = None


def _signed_text(method: str, target: str, link: str, sender: str, date: str, nonce: str, content: str, assertion: str) -> bytes:
    return "\n".join(["mesh-link-v1", method.upper(), target, link, sender, date, nonce, content, assertion]).encode()


def _key_proof(link: str, kid: str) -> bytes:
    return "\n".join(["mesh-key-v1", link, kid]).encode()


def _target(scope: dict[str, Any]) -> str:
    raw = scope.get("raw_path")
    path = raw.decode("latin-1") if isinstance(raw, (bytes, bytearray)) and raw else scope.get("path", "")
    query = scope.get("query_string") or b""
    return path + ("?" + query.decode("latin-1") if query else "")


def _checked_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Give the other server's address: http(s)://host[:port]")
    return url


def _plain(value: str) -> str:
    return "".join(value.split()).lower()


def _detail(response: httpx.Response, default: str) -> str:
    try:
        data = response.json()
        if isinstance(data, dict) and data.get("detail"):
            return str(data["detail"])
    except ValueError:
        pass
    return default


# The link socket's transport: WebSockets on either side, and requests framed over one


class Socket:
    async def send_text(self, text: str) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def receive(self) -> tuple[str, Any]: ...
    async def close(self, code: int = 1000, reason: str = "") -> None: ...

    async def receive_json(self, timeout: float) -> dict[str, Any]:
        kind, data = await asyncio.wait_for(self.receive(), timeout)
        if kind != "text":
            raise ConnectionError("expected a text message")
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ConnectionError("expected an object")
        return value


class ServerSocket(Socket):
    """A Starlette WebSocket this server accepted."""

    def __init__(self, websocket: Any):
        self.websocket = websocket

    async def send_text(self, text: str) -> None:
        await self.websocket.send_text(text)

    async def send_bytes(self, data: bytes) -> None:
        await self.websocket.send_bytes(data)

    async def receive(self) -> tuple[str, Any]:
        message = await self.websocket.receive()
        if message["type"] == "websocket.disconnect":
            return "close", message.get("code", 1000)
        if message.get("bytes") is not None:
            return "bytes", message["bytes"]
        return "text", message.get("text") or ""

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001 - already closed
            pass


class ClientSocket(Socket):
    """A WebSocket this server dialled (the websockets library)."""

    def __init__(self, connection: Any):
        self.connection = connection

    async def send_text(self, text: str) -> None:
        await self.connection.send(text)

    async def send_bytes(self, data: bytes) -> None:
        await self.connection.send(data)

    async def receive(self) -> tuple[str, Any]:
        import websockets

        try:
            data = await self.connection.recv()
        except websockets.ConnectionClosed as exc:
            return "close", getattr(exc.rcvd, "code", 1006) if exc.rcvd else 1006
        return ("bytes", data) if isinstance(data, bytes) else ("text", data)

    async def close(self, code: int = 1000, reason: str = "") -> None:
        try:
            await self.connection.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001 - already closed
            pass


def _frame(header: dict[str, Any], payload: bytes = b"") -> bytes:
    encoded = json.dumps(header, separators=(",", ":")).encode()
    return struct.pack(">I", len(encoded)) + encoded + payload


def _unframe(data: bytes) -> tuple[dict[str, Any], bytes]:
    length = struct.unpack(">I", data[:4])[0]
    return json.loads(data[4:4 + length]), data[4 + length:]


class _Stream:
    def __init__(self) -> None:
        self.queue: asyncio.Queue[tuple[dict[str, Any], bytes] | None] = asyncio.Queue()


class Channel:
    """One link socket: control messages as text, and requests and WebSockets carried as binary frames.

    A frame is a 4-byte length, a JSON header ({"t": kind, "id": stream, ...}) and a payload. The dialling
    side numbers its streams odd and the other even, so both can open streams at once.
    """

    def __init__(self, links: Links, link_id: str, socket: Socket, *, dialled: bool, client_host: str):
        self.links = links
        self.link_id = link_id
        self.socket = socket
        self.dialled = dialled
        self.client_host = client_host
        self.heard = time.monotonic()
        self.signed_at = time.monotonic()
        self._next = 1 if dialled else 2
        self._streams: dict[int, _Stream] = {}
        self._serving: dict[int, asyncio.Task[Any]] = {}
        self._incoming_ws: dict[int, asyncio.Queue[tuple[str, Any] | None]] = {}
        self._send_lock = asyncio.Lock()
        self._closed = False
        self.client = httpx.AsyncClient(transport=ChannelTransport(self), timeout=httpx.Timeout(600, connect=OPEN_TIMEOUT))

    def _new_id(self) -> int:
        value = self._next
        self._next += 2
        return value

    async def send_control(self, message: dict[str, Any]) -> None:
        if self._closed:
            return
        try:
            async with self._send_lock:
                await self.socket.send_text(json.dumps(message))
        except Exception:  # noqa: BLE001 - the socket went; run() notices
            pass

    async def send_frame(self, header: dict[str, Any], payload: bytes = b"") -> None:
        if self._closed:
            raise LinkDown("the link socket closed")
        try:
            async with self._send_lock:
                await self.socket.send_bytes(_frame(header, payload))
        except Exception as exc:  # noqa: BLE001
            raise LinkDown(f"the link socket closed: {exc}") from exc

    async def run(self) -> None:
        try:
            while True:
                kind, data = await self.socket.receive()
                self.heard = time.monotonic()
                if kind == "close":
                    return
                if kind == "text":
                    try:
                        message = json.loads(data)
                    except ValueError:
                        continue
                    if isinstance(message, dict):
                        self.links.control(self, message)
                    continue
                try:
                    header, payload = _unframe(data)
                except (ValueError, struct.error):
                    continue
                await self._dispatch(header, payload)
        finally:
            self._closed = True
            for stream in self._streams.values():
                stream.queue.put_nowait(None)
            for queue in self._incoming_ws.values():
                queue.put_nowait(None)
            for task in self._serving.values():
                task.cancel()

    async def _dispatch(self, header: dict[str, Any], payload: bytes) -> None:
        kind, stream_id = header.get("t"), int(header.get("id") or 0)
        if kind in ("req", "wso"):
            if (stream_id % 2 == 1) == self.dialled or stream_id in self._serving:
                return  # a stream number only the other side may open
            if len(self._serving) >= MAX_STREAMS:
                await self.send_frame({"t": "err", "id": stream_id, "status": 503, "message": "Too many requests over this link at once"})
                return
            work = self._serve_request(stream_id, header, payload) if kind == "req" else self._serve_websocket(stream_id, header)
            task = asyncio.ensure_future(work)
            self._serving[stream_id] = task
            task.add_done_callback(lambda _: self._serving.pop(stream_id, None))
            return
        if kind == "cancel":
            task = self._serving.get(stream_id)
            if task is not None:
                task.cancel()
            return
        if kind in ("wsm", "wsc") and stream_id in self._incoming_ws:
            # A message for a WebSocket the other side opened here.
            queue = self._incoming_ws[stream_id]
            queue.put_nowait(("close", header.get("code", 1000)) if kind == "wsc" else (("bytes", payload) if header.get("binary") else ("text", payload.decode())))
            return
        stream = self._streams.get(stream_id)
        if stream is not None:
            stream.queue.put_nowait((header, payload))

    # Requests this side sends over the socket

    async def request(self, method: str, target: str, headers: list[tuple[str, str]], body: bytes) -> tuple[int, list[tuple[str, str]], AsyncIterator[bytes], Callable[[], Awaitable[None]]]:
        if len(self._streams) >= MAX_STREAMS:
            raise LinkDown("Too many requests over this link at once")
        stream_id = self._new_id()
        stream = _Stream()
        self._streams[stream_id] = stream
        try:
            await self.send_frame({"t": "req", "id": stream_id, "method": method, "target": target, "headers": headers}, body)
            # As long as a direct request waits for its headers: a reply that isn't streamed comes whole.
            first = await asyncio.wait_for(stream.queue.get(), 600)
        except BaseException:
            self._streams.pop(stream_id, None)
            raise
        if first is None:
            self._streams.pop(stream_id, None)
            raise LinkDown("the link socket closed")
        header, _ = first
        if header.get("t") == "err":
            self._streams.pop(stream_id, None)
            return int(header.get("status") or 502), [("content-type", "application/json")], _once(json.dumps({"detail": header.get("message") or "failed"}).encode()), _nothing
        status = int(header.get("status") or 502)
        response_headers = [(str(key), str(value)) for key, value in header.get("headers") or []]

        async def body_chunks() -> AsyncIterator[bytes]:
            try:
                while True:
                    item = await stream.queue.get()
                    if item is None:
                        raise httpx.ReadError("the link socket closed")
                    part, payload = item
                    if part.get("t") == "data":
                        yield payload
                    elif part.get("t") in ("end", "err"):
                        return
            finally:
                self._streams.pop(stream_id, None)

        async def cancel() -> None:
            if stream_id in self._streams:
                self._streams.pop(stream_id, None)
                try:
                    await self.send_frame({"t": "cancel", "id": stream_id})
                except LinkDown:
                    pass

        return status, response_headers, body_chunks(), cancel

    async def open_websocket(self, target: str, headers: dict[str, str]) -> "ChannelSocket":
        stream_id = self._new_id()
        stream = _Stream()
        self._streams[stream_id] = stream
        await self.send_frame({"t": "wso", "id": stream_id, "target": target, "headers": list(headers.items())})
        first = await asyncio.wait_for(stream.queue.get(), OPEN_TIMEOUT)
        if first is None or first[0].get("t") != "wsa":
            self._streams.pop(stream_id, None)
            code = first[0].get("code") if first else 1006
            raise LinkDown(f"the other server refused the connection ({code})")
        return ChannelSocket(self, stream_id, stream)

    # Requests the other side sent over the socket: run through this server's own app

    def _scope(self, kind: str, header: dict[str, Any]) -> dict[str, Any]:
        target = str(header.get("target") or "/")
        path, _, query = target.partition("?")
        headers = [(str(key).lower().encode("latin-1"), str(value).encode("latin-1")) for key, value in header.get("headers") or []]
        scope: dict[str, Any] = {
            "type": kind, "asgi": {"version": "3.0"}, "http_version": "1.1", "scheme": "https" if kind == "http" else "wss",
            "path": unquote(path), "raw_path": path.encode("latin-1"), "query_string": query.encode("latin-1"), "root_path": "",
            "headers": headers, "client": (self.client_host, 0), "server": ("link", 0), "state": {"link_channel": self.link_id},
        }
        if kind == "http":
            scope["method"] = str(header.get("method") or "GET").upper()
        else:
            scope["subprotocols"] = []
        return scope

    async def _serve_request(self, stream_id: int, header: dict[str, Any], body: bytes) -> None:
        app = self.links.app
        if app is None:
            await self.send_frame({"t": "err", "id": stream_id, "status": 503, "message": "Not ready"})
            return
        delivered = False
        finished = asyncio.Event()

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await finished.wait()
            return {"type": "http.disconnect"}

        started = False

        async def send(message: dict[str, Any]) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = [(key.decode("latin-1"), value.decode("latin-1")) for key, value in message.get("headers") or []]
                await self.send_frame({"t": "res", "id": stream_id, "status": message["status"], "headers": headers})
            elif message["type"] == "http.response.body":
                data = message.get("body") or b""
                for offset in range(0, len(data), FRAME):
                    await self.send_frame({"t": "data", "id": stream_id}, data[offset:offset + FRAME])
                if not message.get("more_body"):
                    await self.send_frame({"t": "end", "id": stream_id})

        try:
            await app(self._scope("http", header), receive, send)
        except asyncio.CancelledError:
            finished.set()
            raise
        except LinkDown:
            pass
        except Exception:  # noqa: BLE001 - the app failed; tell the other side
            LOGGER.exception("a request over the link failed")
            try:
                await self.send_frame({"t": "end" if started else "err", "id": stream_id, "status": 500, "message": "Internal error"})
            except LinkDown:
                pass
        finally:
            finished.set()

    async def _serve_websocket(self, stream_id: int, header: dict[str, Any]) -> None:
        app = self.links.app
        queue: asyncio.Queue[tuple[str, Any] | None] = asyncio.Queue()
        self._incoming_ws[stream_id] = queue
        connected = False
        accepted = False

        async def receive() -> dict[str, Any]:
            nonlocal connected
            if not connected:
                connected = True
                return {"type": "websocket.connect"}
            item = await queue.get()
            if item is None or item[0] == "close":
                return {"type": "websocket.disconnect", "code": item[1] if item else 1006}
            kind, value = item
            return {"type": "websocket.receive", "bytes": value} if kind == "bytes" else {"type": "websocket.receive", "text": value}

        async def send(message: dict[str, Any]) -> None:
            nonlocal accepted
            kind = message["type"]
            if kind == "websocket.accept":
                accepted = True
                await self.send_frame({"t": "wsa", "id": stream_id})
            elif kind == "websocket.send":
                if message.get("bytes") is not None:
                    await self.send_frame({"t": "wsm", "id": stream_id, "binary": True}, message["bytes"])
                else:
                    await self.send_frame({"t": "wsm", "id": stream_id}, str(message.get("text") or "").encode())
            elif kind == "websocket.close":
                await self.send_frame({"t": "wsc", "id": stream_id, "code": message.get("code", 1000)})

        try:
            if app is not None:
                await app(self._scope("websocket", header), receive, send)
        except (asyncio.CancelledError, LinkDown):
            pass
        except Exception:  # noqa: BLE001
            LOGGER.exception("a WebSocket over the link failed")
        finally:
            self._incoming_ws.pop(stream_id, None)
            if not self._closed:
                try:
                    await self.send_frame({"t": "wsc", "id": stream_id, "code": 1000 if accepted else 4403})
                except LinkDown:
                    pass

    async def close(self, code: int, reason: str) -> None:
        if not self._closed:
            self._closed = True
            await self.socket.close(code, reason)
        for stream in self._streams.values():
            stream.queue.put_nowait(None)
        try:
            await self.client.aclose()
        except Exception:  # noqa: BLE001
            pass


class ChannelSocket(Socket):
    """A WebSocket this side opened over the link socket."""

    def __init__(self, channel: Channel, stream_id: int, stream: _Stream):
        self.channel = channel
        self.stream_id = stream_id
        self.stream = stream

    async def send_text(self, text: str) -> None:
        await self.channel.send_frame({"t": "wsm", "id": self.stream_id}, text.encode())

    async def send_bytes(self, data: bytes) -> None:
        await self.channel.send_frame({"t": "wsm", "id": self.stream_id, "binary": True}, data)

    async def receive(self) -> tuple[str, Any]:
        item = await self.stream.queue.get()
        if item is None:
            return "close", 1006
        header, payload = item
        if header.get("t") == "wsc":
            self.channel._streams.pop(self.stream_id, None)
            return "close", header.get("code", 1000)
        return ("bytes", payload) if header.get("binary") else ("text", payload.decode())

    async def close(self, code: int = 1000, reason: str = "") -> None:
        if self.stream_id in self.channel._streams:
            self.channel._streams.pop(self.stream_id, None)
            try:
                await self.channel.send_frame({"t": "wsc", "id": self.stream_id, "code": code})
            except LinkDown:
                pass


class _ChannelBody(httpx.AsyncByteStream):
    def __init__(self, chunks: AsyncIterator[bytes], cancel: Callable[[], Awaitable[None]]):
        self.chunks = chunks
        self.cancel = cancel

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        await self.cancel()


class ChannelTransport(httpx.AsyncBaseTransport):
    """httpx requests carried over a link socket, so code that sends to a peer doesn't care which way it goes."""

    def __init__(self, channel: Channel):
        self.channel = channel

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        target = request.url.raw_path.decode("latin-1")
        headers = [(key.decode("latin-1"), value.decode("latin-1")) for key, value in request.headers.raw if key.lower() not in (b"host", b"content-length", b"transfer-encoding", b"connection")]
        try:
            status, response_headers, chunks, cancel = await self.channel.request(request.method, target, headers, body)
        except (LinkDown, asyncio.TimeoutError) as exc:
            raise httpx.ConnectError(str(exc) or "the link socket didn't answer", request=request) from exc
        return httpx.Response(status, headers=response_headers, stream=_ChannelBody(chunks, cancel), request=request)


async def _once(data: bytes) -> AsyncIterator[bytes]:
    yield data


async def _nothing() -> None:
    return None
