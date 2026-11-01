"""Where large recordings live: traces and terminal output (AI_PROXY_BLOB_URL).

  database           in the database itself, gzipped (the default, and what every install before had)
  file:///path       files in a folder: one computer, or a volume every process mounts
  s3://bucket/prefix any S3-compatible store (AWS S3, Ceph RGW, SeaweedFS, MinIO, ...); AI_PROXY_BLOB_ENDPOINT
                     for one that isn't AWS, AI_PROXY_BLOB_REGION, credentials from AWS_ACCESS_KEY_ID and
                     AWS_SECRET_ACCESS_KEY (AWS_SESSION_TOKEN), or a web identity (AWS_ROLE_ARN with
                     AWS_WEB_IDENTITY_TOKEN_FILE: IRSA in EKS, workload identity federation elsewhere)

Keys are laid out by tenant, month and session (tenant/<org>/<yyyy>/<mm>/<session>/...), so retention
deletes a session's prefix, removing an organization deletes tenant/<org>/, and a bucket lifecycle rule
on the whole store is a backstop for the longest retention the server allows.

Agents resend the whole conversation with every call, so a session's traces repeat the same messages
over and over. In blob storage each message is stored once per session, under the hash of its JSON
(blocks/<sha256>), and each trace keeps {"$block": hash} in its place: pack_trace and unpack_trace.

The S3 client is this file's: AWS Signature Version 4 over httpx, nothing else to install.
"""

from __future__ import annotations

import datetime as _dt
import gzip
import hashlib
import hmac
import json
import os
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import quote, urlsplit

import httpx


class BlobError(RuntimeError):
    pass


class Blobs:
    """A store of named byte strings."""

    kind = "database"

    def put(self, key: str, data: bytes) -> None:
        raise NotImplementedError

    def get(self, key: str) -> bytes | None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        return self.get(key) is not None

    def delete_prefix(self, prefix: str) -> int:
        raise NotImplementedError

    def ready(self) -> tuple[bool, str]:
        return True, self.kind


class FileBlobs(Blobs):
    kind = "file"

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root) or ".." in key.split("/"):
            raise BlobError(f"Not a key: {key}")
        return path

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.part")
        temporary.write_bytes(data)
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                return
            except PermissionError:
                # Windows: another thread is replacing or reading the same key (a message block two traces share).
                if path.is_file() and path.read_bytes() == data:
                    temporary.unlink(missing_ok=True)
                    return
                time.sleep(0.02 * (attempt + 1))
        os.replace(temporary, path)

    def get(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete_prefix(self, prefix: str) -> int:
        base = self._path(prefix.rstrip("/"))
        if base.is_file():
            base.unlink()
            return 1
        if not base.is_dir():
            return 0
        count = 0
        for path in sorted(base.rglob("*"), key=lambda item: -len(item.parts)):
            if path.is_file():
                path.unlink()
                count += 1
            else:
                path.rmdir()
        base.rmdir()
        return count

    def ready(self) -> tuple[bool, str]:
        return (self.root.is_dir() and os.access(self.root, os.W_OK)), f"file {self.root}"


class S3Blobs(Blobs):
    """An S3 bucket (or a prefix in one), signed with AWS Signature Version 4."""

    kind = "s3"

    def __init__(self, bucket: str, prefix: str = "", endpoint: str | None = None, region: str | None = None,
                 credentials: "Credentials | None" = None, client: httpx.Client | None = None):
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.region = region or os.getenv("AI_PROXY_BLOB_REGION") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or "us-east-1"
        endpoint = endpoint or os.getenv("AI_PROXY_BLOB_ENDPOINT") or ""
        if endpoint:
            # An S3-compatible store: path-style addresses (bucket in the path), as they all take.
            self.base = endpoint.rstrip("/") + "/" + bucket
        else:
            self.base = f"https://{bucket}.s3.{self.region}.amazonaws.com"
        self.credentials = credentials or Credentials.from_environment(self.region)
        self.client = client or httpx.Client(timeout=httpx.Timeout(30, connect=10), trust_env=False)

    def _key(self, key: str) -> str:
        return f"{self.prefix}/{key}" if self.prefix else key

    def _request(self, method: str, key: str = "", query: dict[str, str] | None = None, body: bytes = b"") -> httpx.Response:
        url = self.base + ("/" + quote(key, safe="/~") if key else "/")
        headers = sign(method, url, query or {}, body, self.credentials.current(), self.region)
        try:
            return self.client.request(method, url, params=query, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise BlobError(f"S3 {method} {key}: {exc or type(exc).__name__}") from exc

    def put(self, key: str, data: bytes) -> None:
        response = self._request("PUT", self._key(key), body=data)
        if response.status_code >= 300:
            raise BlobError(f"S3 PUT {key}: {response.status_code} {response.text[:200]}")

    def get(self, key: str) -> bytes | None:
        response = self._request("GET", self._key(key))
        if response.status_code == 404:
            return None
        if response.status_code >= 300:
            raise BlobError(f"S3 GET {key}: {response.status_code} {response.text[:200]}")
        return response.content

    def exists(self, key: str) -> bool:
        response = self._request("HEAD", self._key(key))
        return response.status_code < 300

    def list(self, prefix: str) -> list[str]:
        keys: list[str] = []
        token = None
        while True:
            query = {"list-type": "2", "prefix": self._key(prefix)}
            if token:
                query["continuation-token"] = token
            response = self._request("GET", "", query)
            if response.status_code >= 300:
                raise BlobError(f"S3 list {prefix}: {response.status_code} {response.text[:200]}")
            root = ET.fromstring(response.content)
            namespace = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
            keys.extend(item.text or "" for item in root.iter(f"{namespace}Key"))
            truncated = root.find(f"{namespace}IsTruncated")
            token_node = root.find(f"{namespace}NextContinuationToken")
            if truncated is None or truncated.text != "true" or token_node is None:
                return keys
            token = token_node.text

    def delete_prefix(self, prefix: str) -> int:
        count = 0
        for key in self.list(prefix.rstrip("/") + "/"):
            response = self._request("DELETE", key)
            if response.status_code < 300 or response.status_code == 404:
                count += 1
        return count

    def ready(self) -> tuple[bool, str]:
        try:
            response = self._request("GET", "", {"list-type": "2", "max-keys": "1", "prefix": self._key("")})
        except BlobError as exc:
            return False, str(exc)
        return response.status_code < 300, f"s3 {self.bucket}: {response.status_code}"


class Credentials:
    """Static keys from the environment, or a web identity's temporary keys, renewed before they expire."""

    def __init__(self, access_key: str = "", secret_key: str = "", token: str | None = None,
                 renew: Callable[[], tuple[str, str, str | None, float]] | None = None):
        self.value = (access_key, secret_key, token)
        self.expires = float("inf") if renew is None else 0.0
        self.renew = renew
        self.lock = threading.Lock()

    def current(self) -> tuple[str, str, str | None]:
        if self.renew is not None and time.time() > self.expires - 300:
            with self.lock:
                if time.time() > self.expires - 300:
                    access, secret, token, expires = self.renew()
                    self.value, self.expires = (access, secret, token), expires
        return self.value

    @classmethod
    def from_environment(cls, region: str) -> "Credentials":
        access, secret = os.getenv("AWS_ACCESS_KEY_ID", ""), os.getenv("AWS_SECRET_ACCESS_KEY", "")
        if access and secret:
            return cls(access, secret, os.getenv("AWS_SESSION_TOKEN") or None)
        role, token_file = os.getenv("AWS_ROLE_ARN"), os.getenv("AWS_WEB_IDENTITY_TOKEN_FILE")
        if role and token_file:
            return cls(renew=lambda: _assume_role(role, Path(token_file), region))
        raise BlobError("S3 storage needs AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY, or AWS_ROLE_ARN with AWS_WEB_IDENTITY_TOKEN_FILE")


def _assume_role(role: str, token_file: Path, region: str) -> tuple[str, str, str | None, float]:
    """STS AssumeRoleWithWebIdentity: unsigned, the identity token is the proof."""
    endpoint = os.getenv("AWS_STS_ENDPOINT") or f"https://sts.{region}.amazonaws.com"
    response = httpx.post(endpoint, data={
        "Action": "AssumeRoleWithWebIdentity", "Version": "2011-06-15", "RoleArn": role,
        "RoleSessionName": os.getenv("AWS_ROLE_SESSION_NAME") or "agent-server", "WebIdentityToken": token_file.read_text().strip(),
    }, timeout=30, trust_env=False)
    if response.status_code >= 300:
        raise BlobError(f"STS AssumeRoleWithWebIdentity: {response.status_code} {response.text[:200]}")
    root = ET.fromstring(response.content)
    found = {element.tag.split("}")[-1]: element.text for element in root.iter()}
    expires = _dt.datetime.fromisoformat(str(found.get("Expiration")).replace("Z", "+00:00")).timestamp()
    return str(found.get("AccessKeyId")), str(found.get("SecretAccessKey")), found.get("SessionToken"), expires


def sign(method: str, url: str, query: dict[str, str], body: bytes, credentials: tuple[str, str, str | None], region: str,
         now: _dt.datetime | None = None, extra: dict[str, str] | None = None, service: str = "s3") -> dict[str, str]:
    """AWS Signature Version 4 headers for one request."""
    access, secret, token = credentials
    now = now or _dt.datetime.now(_dt.timezone.utc)
    stamp, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    parts = urlsplit(url)
    payload = hashlib.sha256(body).hexdigest()
    headers = {"host": parts.netloc, "x-amz-content-sha256": payload, "x-amz-date": stamp, **{k.lower(): v for k, v in (extra or {}).items()}}
    if token:
        headers["x-amz-security-token"] = token
    canonical_query = "&".join(f"{quote(k, safe='-_.~')}={quote(v, safe='-_.~')}" for k, v in sorted(query.items()))
    signed = ";".join(sorted(headers))
    canonical = "\n".join([
        method, parts.path or "/", canonical_query,
        "".join(f"{name}:{headers[name].strip()}\n" for name in sorted(headers)), signed, payload,
    ])
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = f"AWS4{secret}".encode()
    for part in (day, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = f"AWS4-HMAC-SHA256 Credential={access}/{scope}, SignedHeaders={signed}, Signature={signature}"
    headers.pop("host")
    return headers


def from_environment(log_dir: Path | None = None) -> Blobs | None:
    """The store AI_PROXY_BLOB_URL names; None for the database (recordings stay in their tables)."""
    url = (os.getenv("AI_PROXY_BLOB_URL") or "database").strip()
    if url in ("", "database"):
        return None
    if url.startswith("file://"):
        path = url[len("file://"):]
        if len(path) > 2 and path[0] == "/" and path[2] == ":":
            path = path[1:]  # file:///C:/... on Windows
        return FileBlobs(Path(path))
    if url.startswith("s3://"):
        parts = urlsplit(url)
        if not parts.netloc:
            raise BlobError("AI_PROXY_BLOB_URL=s3://<bucket>/<prefix>")
        return S3Blobs(parts.netloc, parts.path)
    raise BlobError("AI_PROXY_BLOB_URL is database, file:///<folder> or s3://<bucket>/<prefix>")


# Recordings' keys

def session_prefix(tenant: str | None, session_id: str, started_at: str) -> str:
    year, month = (started_at[:4], started_at[5:7]) if len(started_at) >= 7 else ("0000", "00")
    return f"tenant/{tenant or 'none'}/{year}/{month}/{session_id}"


def tenant_prefix(tenant: str) -> str:
    return f"tenant/{tenant}/"


# Traces, with messages stored once per session

MESSAGE_PATHS = (("request", "messages"), ("upstream_request", "messages"), ("request", "input"))


def pack_trace(trace: dict[str, Any], stored: Callable[[str], bool], store: Callable[[str, bytes], None]) -> bytes:
    """The trace with each message replaced by {"$block": hash}; store(hash, data) for blocks not stored yet."""
    copy = dict(trace)
    for outer, inner in MESSAGE_PATHS:
        container = copy.get(outer)
        if not isinstance(container, dict) or not isinstance(container.get(inner), list):
            continue
        replaced = []
        for message in container[inner]:
            data = json.dumps(message, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
            if len(data) < 256:
                replaced.append(message)  # Not worth a block of its own.
                continue
            name = hashlib.sha256(data).hexdigest()
            if not stored(name):
                store(name, gzip.compress(data, compresslevel=5))
            replaced.append({"$block": name})
        copy[outer] = {**container, inner: replaced}
    return gzip.compress(json.dumps(copy, ensure_ascii=False).encode("utf-8"), compresslevel=5)


def unpack_trace(data: bytes, block: Callable[[str], bytes | None]) -> dict[str, Any] | None:
    try:
        trace = json.loads(gzip.decompress(data))
    except (OSError, ValueError):
        return None
    if not isinstance(trace, dict):
        return None
    for outer, inner in MESSAGE_PATHS:
        container = trace.get(outer)
        if not isinstance(container, dict) or not isinstance(container.get(inner), list):
            continue
        restored = []
        for message in container[inner]:
            if isinstance(message, dict) and set(message) == {"$block"}:
                raw = block(str(message["$block"]))
                try:
                    restored.append(json.loads(gzip.decompress(raw)) if raw else {"error": "This message's stored copy is gone"})
                except (OSError, ValueError):
                    restored.append({"error": "This message's stored copy can't be read"})
            else:
                restored.append(message)
        container[inner] = restored
    return trace


def parallel(calls: Iterable[Callable[[], Any]], workers: int = 8) -> list[Any]:
    """Run blocking calls (blob reads) a few at a time; each result, or its exception."""
    from concurrent.futures import ThreadPoolExecutor

    calls = list(calls)
    if len(calls) <= 1:
        return [_safely(call) for call in calls]
    with ThreadPoolExecutor(max_workers=min(workers, len(calls))) as pool:
        return list(pool.map(_safely, calls))


def _safely(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except Exception as exc:  # noqa: BLE001
        return exc
