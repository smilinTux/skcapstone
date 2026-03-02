"""
TLS helpers for the skcapstone daemon.

When ``SKCAPSTONE_TLS=true`` is set the daemon generates (or reuses)
a self-signed X.509 certificate + RSA private key stored under
``~/.skcapstone/tls/``.  The SHA-256 fingerprint of the certificate
is logged at startup so operators can pin it in clients.

Requirements
------------
- ``cryptography >= 3.0``   (``pip install cryptography``)
- Python's built-in ``ssl`` module (wraps the stdlib HTTP server socket)
"""

from __future__ import annotations

import datetime
import hashlib
import ipaddress
import logging
import ssl
from pathlib import Path

logger = logging.getLogger("skcapstone.tls")

_CERT_FILENAME = "daemon.crt"
_KEY_FILENAME = "daemon.key"
_CERT_VALID_DAYS = 3650  # ~10 years


def _ensure_tls_dir(tls_dir: Path) -> None:
    tls_dir.mkdir(parents=True, exist_ok=True)
    tls_dir.chmod(0o700)


def _generate_self_signed(cert_path: Path, key_path: Path) -> None:
    """Generate an RSA-2048 self-signed certificate and write both files."""
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "The 'cryptography' package is required for TLS support. "
            "Install it with: pip install cryptography"
        ) from exc

    # Private key
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    # Subject / Issuer
    subject = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "skcapstone-daemon"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "SKCapstone Sovereign Agent"),
        ]
    )

    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + datetime.timedelta(days=_CERT_VALID_DAYS))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    x509.IPAddress(ipaddress.IPv6Address("::1")),
                ]
            ),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .sign(private_key, hashes.SHA256())
    )

    # Write key (0600) then cert (0644)
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)

    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    cert_path.chmod(0o644)

    logger.info("TLS: generated self-signed certificate → %s", cert_path)


def cert_fingerprint_sha256(cert_path: Path) -> str:
    """Return the colon-delimited SHA-256 fingerprint of a PEM certificate.

    Example: ``AA:BB:CC:...``
    """
    pem_data = cert_path.read_bytes()
    # Strip PEM headers and decode the DER body
    import base64

    lines = pem_data.decode().splitlines()
    der_b64 = "".join(
        ln for ln in lines if not ln.startswith("-----")
    )
    der = base64.b64decode(der_b64)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def ensure_tls_cert(tls_dir: Path) -> tuple[Path, Path]:
    """Return (cert_path, key_path), generating them if they don't exist.

    Args:
        tls_dir: Directory to store ``daemon.crt`` and ``daemon.key``.

    Returns:
        ``(cert_path, key_path)`` as absolute :class:`~pathlib.Path` objects.
    """
    _ensure_tls_dir(tls_dir)
    cert_path = tls_dir / _CERT_FILENAME
    key_path = tls_dir / _KEY_FILENAME

    if cert_path.exists() and key_path.exists():
        logger.debug("TLS: reusing existing certificate %s", cert_path)
    else:
        logger.info("TLS: no certificate found — generating self-signed cert in %s", tls_dir)
        _generate_self_signed(cert_path, key_path)

    return cert_path, key_path


def build_ssl_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """Build a server-side :class:`ssl.SSLContext` from cert + key files.

    Uses TLS 1.2+ and disables deprecated protocol versions.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile=str(cert_path), keyfile=str(key_path))
    return ctx
