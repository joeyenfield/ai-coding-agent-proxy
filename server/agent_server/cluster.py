"""Several agent-server processes as one server: what they share, and how they reach each other.

One agent-server process keeps everything it needs in memory, which is what a small install
runs (single-node mode: no AI_PROXY_REDIS_URL). Run as several (pods in Kubernetes, or uvicorn
workers), some of that has to be shared, and some has to stay with the one process that holds
a connection:

  - Shared keys and counters (Redis): who holds each machine's connection and each shared
    terminal, request ids, usage counts, launch grants, leases for jobs only one may run.
  - Messages to every process (Redis pub/sub): a session, a machine, the settings or a usage
    limit changed; live request text, for consoles watching on another process.
  - Calls to one process (its internal port, AI_PROXY_INTERNAL_PORT): a request to a machine
    whose connection another process holds, a tunnel the machine dialled back to the wrong one,
    a terminal viewer whose terminal is hosted elsewhere. These carry AI_PROXY_INTERNAL_TOKEN,
    which only the server's own processes have.

LocalCluster is the single-node implementation: the same calls, kept in this process. Nothing
the cluster carries is a secret in the clear: machines' Strata and MCP keys travel sealed with a
key derived from the internal token (seal/unseal), so Redis only ever sees ciphertext.

Every process has an id (AI_PROXY_POD_ID, else the host's name, plus the process id when one
pod runs several workers). Each announces itself every HEARTBEAT seconds; one that hasn't for
POD_TIMEOUT is gone, and so are the connections it held.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import socket
import threading
import time
from typing import Any, Awaitable, Callable

import httpx
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


LOGGER = logging.getLogger("agent-proxy")
PREFIX = "mesh:"
HEARTBEAT = 5.0
POD_TIMEOUT = 20.0
# The header internal calls carry; the internal port refuses anything without it.
INTERNAL_HEADER = "x-ai-proxy-internal"
# Who a call forwarded to another process came from, as the first process saw it.
FORWARDED_FOR = "x-ai-proxy-forwarded-for"

Handler = Callable[[dict[str, Any], str], Any]


class ClusterError(RuntimeError):
    """Another process couldn't be reached, or the shared store failed."""


def pod_id_from_environment(workers: int = 1) -> str:
    base = (os.getenv("AI_PROXY_POD_ID") or socket.gethostname() or "agent-server").strip()[:60]
    # Several workers in one pod are several processes: each needs an id of its own.
    return f"{base}-{os.getpid()}" if workers > 1 else base


class Cluster:
    """What every process of one server shares. See the module docstring."""

    scaled = False

    def __init__(self, pod_id: str, internal_token: str = "", internal_url: str = ""):
        self.pod_id = pod_id
        self.internal_token = internal_token
        # Where other processes reach this one's internal port (set once it listens).
        self.internal_url = internal_url
        self._handlers: dict[str, list[Handler]] = {}
        # Extra state each heartbeat carries (live subscribers, ...): name -> callable.
        self.beacons: dict[str, Callable[[], Any]] = {}
        self._pods: dict[str, dict[str, Any]] = {}
        self._client: httpx.AsyncClient | None = None
        # Tests point internal calls at in-process apps: an httpx transport.
        self.transport: httpx.AsyncBaseTransport | None = None
        self._seal_key = _derive(internal_token) if internal_token else None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._tasks: list[asyncio.Task[Any]] = []

    # Messages

    def on(self, channel: str, handler: Handler) -> None:
        """handler(data, sender) runs on the event loop for each message another process publishes."""
        self._handlers.setdefault(channel, []).append(handler)

    def publish(self, channel: str, data: dict[str, Any]) -> None:
        """Tell every other process (never this one). Fire and forget."""

    def _dispatch(self, channel: str, data: dict[str, Any], sender: str) -> None:
        for handler in self._handlers.get(channel, []):
            try:
                result = handler(data, sender)
                if asyncio.iscoroutine(result):
                    asyncio.ensure_future(result)
            except Exception:  # noqa: BLE001 - one bad handler mustn't stop the rest
                LOGGER.exception("cluster message %s failed", channel)

    # Shared keys (sync: callers are often sync code; Redis answers in well under a millisecond)

    def get(self, key: str) -> str | None:
        raise NotImplementedError

    def set(self, key: str, value: str, ttl: float | None = None, nx: bool = False) -> bool:
        raise NotImplementedError

    def delete(self, *keys: str) -> None:
        raise NotImplementedError

    def getdel(self, key: str) -> str | None:
        raise NotImplementedError

    def incr(self, key: str, by: int = 1, ttl: float | None = None) -> int:
        raise NotImplementedError

    def hincrby(self, key: str, field: str, by: int, ttl: float | None = None) -> int:
        raise NotImplementedError

    def hgetall(self, key: str) -> dict[str, str]:
        raise NotImplementedError

    def hset(self, key: str, mapping: dict[str, str], ttl: float | None = None) -> None:
        raise NotImplementedError

    def keys(self, prefix: str) -> list[str]:
        raise NotImplementedError

    def get_json(self, key: str) -> Any:
        value = self.get(key)
        if value is None:
            return None
        try:
            return json.loads(value)
        except ValueError:
            return None

    def set_json(self, key: str, value: Any, ttl: float | None = None, nx: bool = False) -> bool:
        return self.set(key, json.dumps(value, separators=(",", ":")), ttl, nx)

    # Leases: jobs only one process runs at a time (retention, audit checkpoints, exports)

    def leader(self, name: str, ttl: float = 30.0) -> bool:
        """Whether this process holds (or just took) the lease; call again before ttl to keep it."""
        key = f"lease:{name}"
        if self.set(key, self.pod_id, ttl, nx=True):
            return True
        if self.get(key) == self.pod_id:
            self.set(key, self.pod_id, ttl)
            return True
        return False

    # Processes

    def pods(self) -> dict[str, dict[str, Any]]:
        """The processes alive now, this one included: id -> what its last heartbeat said."""
        now = time.time()
        alive = {pod: info for pod, info in self._pods.items() if now - float(info.get("at") or 0) <= POD_TIMEOUT}
        alive[self.pod_id] = self._beacon()
        return alive

    def alive(self, pod: str | None) -> bool:
        return bool(pod) and (pod == self.pod_id or pod in self.pods())

    def _beacon(self) -> dict[str, Any]:
        state = {"at": time.time(), "url": self.internal_url}
        for name, value in self.beacons.items():
            try:
                state[name] = value()
            except Exception:  # noqa: BLE001
                state[name] = None
        return state

    def _heard(self, data: dict[str, Any], sender: str) -> None:
        if sender != self.pod_id:
            self._pods[sender] = {**data, "at": time.time()}

    # Calls to one process

    async def call(self, pod: str, method: str, path: str, *, json_body: Any = None, timeout: float | None = 30.0, headers: dict[str, str] | None = None) -> httpx.Response:
        """One request to another process's internal port."""
        url = self.url_of(pod)
        client = self._http()
        try:
            return await client.request(
                method, url + path, json=json_body, timeout=timeout,
                headers={INTERNAL_HEADER: self.internal_token, **(headers or {})},
            )
        except httpx.HTTPError as exc:
            raise ClusterError(f"couldn't reach {pod}: {exc or type(exc).__name__}") from exc

    def stream(self, pod: str, method: str, path: str, *, json_body: Any = None, timeout: float | None = None) -> Any:
        """A streamed request to another process (its answer read as it comes): an async context manager."""
        return self._http().stream(
            method, self.url_of(pod) + path, json=json_body, timeout=timeout, headers={INTERNAL_HEADER: self.internal_token},
        )

    def url_of(self, pod: str) -> str:
        info = self.pods().get(pod)
        url = str((info or {}).get("url") or "")
        if not url:
            raise ClusterError(f"{pod} isn't running (or doesn't listen for the others)")
        return url.rstrip("/")

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            options: dict[str, Any] = {"trust_env": False, "timeout": httpx.Timeout(30, connect=5)}
            if self.transport is not None:
                options["transport"] = self.transport
            self._client = httpx.AsyncClient(**options)
        return self._client

    def authentic(self, value: str | None) -> bool:
        """Whether a call carries this server's internal token."""
        return bool(self.internal_token) and bool(value) and secrets.compare_digest(str(value), self.internal_token)

    # Sealing: machine keys and the like travel between processes only encrypted

    def seal(self, value: Any) -> str | None:
        if self._seal_key is None:
            return None
        nonce = os.urandom(12)
        data = AESGCM(self._seal_key).encrypt(nonce, json.dumps(value).encode(), b"agent-mesh")
        return base64.urlsafe_b64encode(nonce + data).decode()

    def unseal(self, value: Any) -> Any:
        if self._seal_key is None or not isinstance(value, str):
            return None
        try:
            raw = base64.urlsafe_b64decode(value.encode())
            return json.loads(AESGCM(self._seal_key).decrypt(raw[:12], raw[12:], b"agent-mesh"))
        except Exception:  # noqa: BLE001 - not ours, or tampered with
            return None

    # Lifecycle

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def ready(self) -> tuple[bool, str]:
        """Whether the shared store answers (for /ready)."""
        return True, "in-process"


class LocalCluster(Cluster):
    """One process: everything is here. Keys expire like Redis's, messages go nowhere."""

    def __init__(self, pod_id: str = "local", internal_token: str = "", internal_url: str = ""):
        super().__init__(pod_id, internal_token, internal_url)
        self._values: dict[str, tuple[Any, float | None]] = {}
        self._lock = threading.Lock()

    def _live(self, key: str) -> Any:
        item = self._values.get(key)
        if item is None:
            return None
        value, expires = item
        if expires is not None and expires <= time.monotonic():
            self._values.pop(key, None)
            return None
        return value

    def _expiry(self, ttl: float | None) -> float | None:
        return time.monotonic() + ttl if ttl else None

    def get(self, key: str) -> str | None:
        with self._lock:
            value = self._live(key)
            return value if isinstance(value, str) else None

    def set(self, key: str, value: str, ttl: float | None = None, nx: bool = False) -> bool:
        with self._lock:
            if nx and self._live(key) is not None:
                return False
            self._values[key] = (value, self._expiry(ttl))
            return True

    def delete(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._values.pop(key, None)

    def getdel(self, key: str) -> str | None:
        with self._lock:
            value = self._live(key)
            self._values.pop(key, None)
            return value if isinstance(value, str) else None

    def incr(self, key: str, by: int = 1, ttl: float | None = None) -> int:
        with self._lock:
            current = int(self._live(key) or 0) + by
            expires = self._values.get(key, (None, None))[1] if ttl is None else self._expiry(ttl)
            self._values[key] = (str(current), expires)
            return current

    def hincrby(self, key: str, field: str, by: int, ttl: float | None = None) -> int:
        with self._lock:
            table = self._live(key)
            if not isinstance(table, dict):
                table = {}
            table[field] = str(int(table.get(field) or 0) + by)
            self._values[key] = (table, self._expiry(ttl) if ttl else self._values.get(key, (None, None))[1])
            return int(table[field])

    def hgetall(self, key: str) -> dict[str, str]:
        with self._lock:
            table = self._live(key)
            return dict(table) if isinstance(table, dict) else {}

    def hset(self, key: str, mapping: dict[str, str], ttl: float | None = None) -> None:
        with self._lock:
            table = self._live(key)
            table = dict(table) if isinstance(table, dict) else {}
            table.update({name: str(value) for name, value in mapping.items()})
            self._values[key] = (table, self._expiry(ttl) if ttl else self._values.get(key, (None, None))[1])

    def keys(self, prefix: str) -> list[str]:
        with self._lock:
            return [key for key in list(self._values) if key.startswith(prefix) and self._live(key) is not None]


class RedisCluster(Cluster):
    """Several processes sharing one Redis (or Valkey): keys, counters, leases and pub/sub."""

    scaled = True

    def __init__(self, url: str, pod_id: str, internal_token: str, internal_url: str = ""):
        super().__init__(pod_id, internal_token, internal_url)
        self.url = url
        self.redis, self._async_factory = _redis_clients(url)
        self._listener: Any = None
        self._ready = (False, "not started")

    def _k(self, key: str) -> str:
        return PREFIX + key

    def publish(self, channel: str, data: dict[str, Any]) -> None:
        message = json.dumps({"pod": self.pod_id, "data": data}, separators=(",", ":"), default=str)
        try:
            self.redis.publish(self._k(channel), message)
        except Exception as exc:  # noqa: BLE001 - the others miss one message; their heartbeats and reloads catch up
            LOGGER.warning("cluster publish %s failed: %s", channel, exc)

    def get(self, key: str) -> str | None:
        value = self.redis.get(self._k(key))
        return value.decode() if isinstance(value, bytes) else value

    def set(self, key: str, value: str, ttl: float | None = None, nx: bool = False) -> bool:
        return bool(self.redis.set(self._k(key), value, px=int(ttl * 1000) if ttl else None, nx=nx))

    def delete(self, *keys: str) -> None:
        if keys:
            self.redis.delete(*(self._k(key) for key in keys))

    def getdel(self, key: str) -> str | None:
        value = self.redis.getdel(self._k(key))
        return value.decode() if isinstance(value, bytes) else value

    def incr(self, key: str, by: int = 1, ttl: float | None = None) -> int:
        pipe = self.redis.pipeline()
        pipe.incrby(self._k(key), by)
        if ttl:
            pipe.pexpire(self._k(key), int(ttl * 1000))
        return int(pipe.execute()[0])

    def hincrby(self, key: str, field: str, by: int, ttl: float | None = None) -> int:
        pipe = self.redis.pipeline()
        pipe.hincrby(self._k(key), field, by)
        if ttl:
            pipe.pexpire(self._k(key), int(ttl * 1000))
        return int(pipe.execute()[0])

    def hgetall(self, key: str) -> dict[str, str]:
        found = self.redis.hgetall(self._k(key)) or {}
        return {_text(name): _text(value) for name, value in found.items()}

    def hset(self, key: str, mapping: dict[str, str], ttl: float | None = None) -> None:
        if not mapping:
            return
        pipe = self.redis.pipeline()
        pipe.hset(self._k(key), mapping={name: str(value) for name, value in mapping.items()})
        if ttl:
            pipe.pexpire(self._k(key), int(ttl * 1000))
        pipe.execute()

    def keys(self, prefix: str) -> list[str]:
        return [_text(key)[len(PREFIX):] for key in self.redis.scan_iter(match=self._k(prefix) + "*", count=500)]

    async def start(self) -> None:
        await super().start()
        try:
            self.redis.ping()
        except Exception as exc:  # noqa: BLE001
            raise ClusterError(f"Redis at {_redacted(self.url)} doesn't answer: {exc}") from exc
        self._ready = (True, "redis")
        self.on("cluster.heartbeat", self._heard)
        self._tasks.append(asyncio.ensure_future(self._listen()))
        self._tasks.append(asyncio.ensure_future(self._beat()))

    async def _listen(self) -> None:
        while True:
            client = self._async_factory()
            pubsub = client.pubsub()
            try:
                await pubsub.psubscribe(PREFIX + "*")
                self._ready = (True, "redis")
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message is None:
                        continue
                    channel = _text(message.get("channel"))[len(PREFIX):]
                    try:
                        envelope = json.loads(_text(message.get("data")))
                    except ValueError:
                        continue
                    sender = str(envelope.get("pod") or "")
                    if sender == self.pod_id or not isinstance(envelope.get("data"), dict):
                        continue
                    self._dispatch(channel, envelope["data"], sender)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Redis restarted: subscribe again
                self._ready = (False, f"redis: {exc}")
                LOGGER.warning("cluster subscription lost: %s", exc)
                await asyncio.sleep(1)
            finally:
                try:
                    await pubsub.aclose()
                    await client.aclose()
                except Exception:  # noqa: BLE001
                    pass

    async def _beat(self) -> None:
        while True:
            beacon = self._beacon()
            try:
                await asyncio.to_thread(self.redis.set, self._k(f"pod:{self.pod_id}"), json.dumps(beacon), px=int(POD_TIMEOUT * 1000))
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("cluster heartbeat failed: %s", exc)
            self.publish("cluster.heartbeat", beacon)
            await asyncio.sleep(HEARTBEAT)

    def pods(self) -> dict[str, dict[str, Any]]:
        alive = super().pods()
        if len(alive) == 1 and not self._pods:
            # Just started, nothing heard yet: what the others last wrote.
            try:
                for key in self.keys("pod:"):
                    info = self.get_json(key)
                    pod = key[len("pod:"):]
                    if isinstance(info, dict) and pod != self.pod_id:
                        self._pods[pod] = info
            except Exception:  # noqa: BLE001
                pass
            alive = super().pods()
        return alive

    async def close(self) -> None:
        try:
            self.redis.delete(self._k(f"pod:{self.pod_id}"))
        except Exception:  # noqa: BLE001
            pass
        await super().close()

    def ready(self) -> tuple[bool, str]:
        try:
            self.redis.ping()
        except Exception as exc:  # noqa: BLE001
            return False, f"redis: {exc}"
        return self._ready


def from_environment(workers: int = 1) -> Cluster:
    """The cluster this process is part of: RedisCluster with AI_PROXY_REDIS_URL, else LocalCluster."""
    url = os.getenv("AI_PROXY_REDIS_URL", "").strip()
    token = os.getenv("AI_PROXY_INTERNAL_TOKEN", "").strip()
    pod = pod_id_from_environment(workers)
    if not url:
        return LocalCluster(pod, token)
    if len(token) < 16:
        raise ClusterError("Several processes (AI_PROXY_REDIS_URL) need AI_PROXY_INTERNAL_TOKEN: a secret of 16 or more characters every one of them has")
    return RedisCluster(url, pod, token)


# fakeredis://<name> in tests: one in-memory server per name, shared by every client in this process.
_FAKE_SERVERS: dict[str, Any] = {}


def _redis_clients(url: str) -> tuple[Any, Callable[[], Any]]:
    if url.startswith("fakeredis://"):
        import fakeredis

        name = url[len("fakeredis://"):] or "default"
        server = _FAKE_SERVERS.setdefault(name, fakeredis.FakeServer())
        return fakeredis.FakeRedis(server=server), lambda: fakeredis.FakeAsyncRedis(server=server)
    try:
        import redis
        import redis.asyncio as redis_async
    except ImportError as exc:  # pragma: no cover - installed with the server
        raise ClusterError("AI_PROXY_REDIS_URL needs the redis package (pip install redis)") from exc
    sync = redis.Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5, health_check_interval=30)
    return sync, lambda: redis_async.Redis.from_url(url, socket_connect_timeout=5, health_check_interval=30)


def _derive(token: str) -> bytes:
    return HKDF(algorithm=SHA256(), length=32, salt=b"agent-mesh-cluster", info=b"seal").derive(token.encode())


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value or "")


def _redacted(url: str) -> str:
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    return f"{scheme}://***@{rest.split('@', 1)[1]}"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def gather_quietly(*calls: Awaitable[Any]) -> list[Any]:
    """Every call's answer, or the exception it raised (one process down mustn't fail the rest)."""
    return list(await asyncio.gather(*calls, return_exceptions=True))
