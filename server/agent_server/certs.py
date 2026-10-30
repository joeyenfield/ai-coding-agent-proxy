"""A local certificate authority for the HTTPS intercept proxy.

The CA is generated on first use and kept in the database (secret "intercept-ca"). Its
certificate, and a bundle of it with the system roots, are written to ca_dir for the agents
that read them as files; its key never is. It is never added to the system trust store;
the launcher hands it only to agent processes it starts (through NODE_EXTRA_CA_CERTS), so
nothing else on the machine trusts it.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import os
import ssl
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .db import Database, kv, schema


CA_NAME = "AI Control Mesh local CA"
SECRET = "intercept-ca"


def system_roots() -> str:
    """PEM text of the root certificates this machine normally trusts."""
    paths = ssl.get_default_verify_paths()
    for candidate in (os.getenv("SSL_CERT_FILE"), paths.cafile, paths.openssl_cafile):
        if candidate and Path(candidate).is_file():
            return Path(candidate).read_text(encoding="utf-8", errors="replace")
    import certifi

    return Path(certifi.where()).read_text(encoding="utf-8")


def write_bundle(ca_pem: str, path: Path) -> None:
    """Write system roots plus the local CA, for tools that take one CA file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    roots = system_roots()
    if ca_pem.strip() in roots:
        roots = roots.replace(ca_pem.strip(), "")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(roots.rstrip() + "\n" + ca_pem, encoding="utf-8")
    temporary.replace(path)


def load_pair(db: Database) -> tuple[bytes, bytes] | None:
    """The stored CA: its certificate and key, PEM."""
    with db.read() as connection:
        stored = kv.get(connection, schema.secrets, SECRET)
    if not stored:
        return None
    value = json.loads(stored)
    return value["cert"].encode(), value["key"].encode()


def store_pair(connection: Any, cert_pem: bytes, key_pem: bytes) -> None:
    kv.put(connection, schema.secrets, SECRET, json.dumps({"cert": cert_pem.decode(), "key": key_pem.decode()}).encode())


class CertificateAuthority:
    def __init__(self, db: Database, directory: Path):
        self.directory = directory
        self.cert_path = directory / "ca.pem"
        self.bundle_path = directory / "bundle.pem"
        pair = load_pair(db)
        if pair is None:
            pair = self._create()
            with db.write() as connection:
                # Another process may have made one first; keep theirs.
                if kv.get(connection, schema.secrets, SECRET) is None:
                    store_pair(connection, *pair)
            pair = load_pair(db) or pair
        cert_pem, key_pem = pair
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = self.cert_path.with_suffix(".tmp")
        temporary.write_bytes(cert_pem)
        temporary.replace(self.cert_path)
        write_bundle(cert_pem.decode("ascii"), self.bundle_path)
        self.cert = x509.load_pem_x509_certificate(cert_pem)
        self.key = serialization.load_pem_private_key(key_pem, password=None)
        self._pem = cert_pem.decode("ascii")
        # One key for every leaf keeps per-host certificate creation cheap.
        self.leaf_key = ec.generate_private_key(ec.SECP256R1())
        self._leaf_key_pem = self.leaf_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        )

    @property
    def pem(self) -> str:
        return self._pem

    def fingerprint(self) -> str:
        return self.cert.fingerprint(hashes.SHA256()).hex(":").upper()

    def server_context(self, host: str) -> ssl.SSLContext:
        return self._context(host.lower())

    @lru_cache(maxsize=512)
    def _context(self, host: str) -> ssl.SSLContext:
        cert_pem = self._leaf(host)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # The intercept proxy speaks HTTP/1.1 to clients only.
        context.set_alpn_protocols(["http/1.1"])
        # load_cert_chain only accepts files, so write a short-lived pair.
        with tempfile.TemporaryDirectory() as temporary:
            cert_file = Path(temporary) / "leaf.pem"
            key_file = Path(temporary) / "leaf-key.pem"
            cert_file.write_bytes(cert_pem)
            key_file.write_bytes(self._leaf_key_pem)
            context.load_cert_chain(cert_file, key_file)
        return context

    def _leaf(self, host: str) -> bytes:
        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            alternative: x509.GeneralName = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            alternative = x509.DNSName(host)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host[:64])]))
            .issuer_name(self.cert.subject)
            .public_key(self.leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName([alternative]), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.cert.public_key()), critical=False)
            .sign(self.key, hashes.SHA256())
        )
        return cert.public_bytes(serialization.Encoding.PEM)

    @staticmethod
    def _create() -> tuple[bytes, bytes]:
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, CA_NAME)])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                    key_encipherment=False, data_encipherment=False, key_agreement=False,
                    encipher_only=False, decipher_only=False,
                ),
                critical=True,
            )
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256())
        )
        key_bytes = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        )
        return cert.public_bytes(serialization.Encoding.PEM), key_bytes
