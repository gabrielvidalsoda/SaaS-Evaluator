"""DNS probe (dnspython): SPF and DMARC records of the organization's domain.

Distinguishes "record absent" (NXDOMAIN / no TXT answer — a real finding) from
"lookup failed" (timeouts — unknown, so the check is not evaluated rather than failed).
Falls back to public resolvers when the system resolver times out, which happens on
some home routers with large TXT answers."""

from __future__ import annotations

import asyncio
from typing import Any

import dns.exception
import dns.resolver

_FALLBACK_NAMESERVERS = ["1.1.1.1", "8.8.8.8"]


def _resolvers() -> list[dns.resolver.Resolver]:
    out = []
    try:
        out.append(dns.resolver.Resolver())
    except dns.resolver.NoResolverConfiguration:
        pass
    public = dns.resolver.Resolver(configure=False)
    public.nameservers = _FALLBACK_NAMESERVERS
    out.append(public)
    return out


def _txt(name: str) -> list[str] | None:
    """TXT strings for `name`; [] if definitively absent, None if the lookup failed."""
    for resolver in _resolvers():
        try:
            answers = resolver.resolve(name, "TXT", lifetime=6)
        except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
            return []
        except (dns.exception.DNSException, OSError):
            continue
        return [b"".join(r.strings).decode("utf-8", errors="replace") for r in answers]
    return None


def _probe(domain: str) -> dict[str, Any]:
    root = _txt(domain)
    dmarc_txt = _txt(f"_dmarc.{domain}")
    spf = next((r for r in root or [] if r.lower().startswith("v=spf1")), None)
    dmarc = next((r for r in dmarc_txt or [] if r.lower().startswith("v=dmarc1")), None)
    policy = None
    if dmarc:
        for part in dmarc.split(";"):
            key, _, value = part.strip().partition("=")
            if key.strip().lower() == "p":
                policy = value.strip().lower()
    return {
        "domain": domain,
        "lookup_ok": root is not None and dmarc_txt is not None,
        "spf": spf,
        "dmarc": dmarc,
        "dmarc_policy": policy,
    }


async def probe_dns(domain: str) -> dict[str, Any]:
    return await asyncio.to_thread(_probe, domain)
