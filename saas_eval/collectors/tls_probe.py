"""TLS probe (stdlib ssl): which protocol versions the server accepts and the
certificate's validity/expiry. One handshake per version — no ciphers enumeration."""

from __future__ import annotations

import asyncio
import socket
import ssl
from datetime import datetime, timezone
from typing import Any

_VERSIONS = {
    "TLSv1.0": ssl.TLSVersion.TLSv1,
    "TLSv1.1": ssl.TLSVersion.TLSv1_1,
    "TLSv1.2": ssl.TLSVersion.TLSv1_2,
    "TLSv1.3": ssl.TLSVersion.TLSv1_3,
}


def _try_version(host: str, port: int, version: ssl.TLSVersion) -> bool | None:
    """True = accepted, False = refused, None = could not test (local OpenSSL can't
    speak that version even at security level 0)."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        ctx.set_ciphers("ALL:@SECLEVEL=0")
        ctx.minimum_version = version
        ctx.maximum_version = version
    except (ValueError, ssl.SSLError):
        return None
    try:
        with socket.create_connection((host, port), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=host):
                return True
    except ssl.SSLError as exc:
        # "no protocols available" = our local OpenSSL can't offer it -> untestable.
        if "NO_PROTOCOLS_AVAILABLE" in str(exc).upper():
            return None
        return False
    except OSError:
        return False


def _certificate(host: str, port: int) -> dict[str, Any]:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
                negotiated = tls.version()
    except ssl.SSLCertVerificationError as exc:
        return {"valid": False, "error": exc.verify_message or str(exc)}
    except (ssl.SSLError, OSError) as exc:
        return {"valid": False, "error": f"{type(exc).__name__}: {exc}"}

    not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=timezone.utc)
    issuer = dict(x[0] for x in cert.get("issuer", []))
    return {
        "valid": True,
        "negotiated": negotiated,
        "not_after": not_after.isoformat(),
        "days_to_expiry": (not_after - datetime.now(timezone.utc)).days,
        "issuer": issuer.get("organizationName") or issuer.get("commonName", ""),
    }


def _probe(host: str, port: int) -> dict[str, Any]:
    return {
        "host": host,
        "certificate": _certificate(host, port),
        "versions": {name: _try_version(host, port, v) for name, v in _VERSIONS.items()},
    }


async def probe_tls(host: str, port: int = 443) -> dict[str, Any]:
    return await asyncio.to_thread(_probe, host, port)
