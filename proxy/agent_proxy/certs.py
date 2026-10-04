"""A local certificate authority for the HTTPS intercept proxy.

The CA is generated on first use and kept on this machine. It is never added to
the system trust store; the launcher hands it only to agent processes it starts
(through NODE_EXTRA_CA_CERTS), so nothing else on the machine trusts it.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
import tempfile
from functools import lru_cache
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


CA_NAME = "AI Coding Agent Proxy local CA"


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


class CertificateAuthority:
    def __init__(self, directory: Path):
        self.directory = directory
        self.cert_path = directory / "ca.pem"
        self.key_path = directory / "ca-key.pem"
        self.bundle_path = directory / "bundle.pem"
        if not (self.cert_path.exists() and self.key_path.exists()):
            self._create()
        write_bundle(self.cert_path.read_text(encoding="ascii"), self.bundle_path)
        self.cert = x509.load_pem_x509_certificate(self.cert_path.read_bytes())
        self.key = serialization.load_pem_private_key(self.key_path.read_bytes(), password=None)
        # One key for every leaf keeps per-host certificate creation cheap.
        self.leaf_key = ec.generate_private_key(ec.SECP256R1())
        self._leaf_key_pem = self.leaf_key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption(),
        )

    @property
    def pem(self) -> str:
        return self.cert_path.read_text(encoding="ascii")

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

    def _create(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
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
        # Create the key file private from the start rather than chmod-ing afterwards.
        descriptor = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(key_bytes)
        self.cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
