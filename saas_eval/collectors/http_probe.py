"""Passive HTTP probes (httpx): redirects, response headers, cookies, compression,
protocol, TTFB samples, exposed-file probes, security.txt, robots.txt.

Only GET requests, a small fixed list of paths, sequential with a pause between
samples — never payloads, fuzzing or form submissions."""

from __future__ import annotations

import asyncio
import statistics
import time
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from .. import USER_AGENT_SUFFIX

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    f"Chrome/126.0 Safari/537.36 {USER_AGENT_SUFFIX}"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "br, gzip, deflate",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = httpx.Timeout(20.0, connect=10.0)
TTFB_SAMPLES = 5

# Header names that reveal a CDN / edge network, mapped to the provider name.
CDN_HEADER_HINTS = {
    "cf-ray": "Cloudflare",
    "x-amz-cf-id": "Amazon CloudFront",
    "x-amz-cf-pop": "Amazon CloudFront",
    "x-fastly-request-id": "Fastly",
    "x-served-by": "Fastly/Varnish",
    "x-akamai-transformed": "Akamai",
    "akamai-grn": "Akamai",
    "x-vercel-id": "Vercel",
    "x-nf-request-id": "Netlify",
    "x-azure-ref": "Azure Front Door",
    "fly-request-id": "Fly.io",
    "x-edge-location": "Edge network",
    "cdn-cache": "CDN",
    "x-cdn": "CDN",
}
CDN_SERVER_HINTS = {
    "cloudflare": "Cloudflare",
    "akamaighost": "Akamai",
    "cloudfront": "Amazon CloudFront",
    "google frontend": "Google Cloud",
    "gws": "Google",
    "vercel": "Vercel",
    "netlify": "Netlify",
    "fastly": "Fastly",
    "bunnycdn": "BunnyCDN",
}

EXPOSED_FILE_PROBES = {
    "/.git/HEAD": lambda body: body.lstrip().startswith("ref:"),
    "/.env": lambda body: "<html" not in body.lower()
    and sum(1 for line in body.splitlines() if "=" in line and not line.startswith("#")) >= 2,
}


def detect_cdn(headers: dict[str, str]) -> list[str]:
    found = set()
    lower = {k.lower(): v for k, v in headers.items()}
    for name, provider in CDN_HEADER_HINTS.items():
        if name in lower:
            found.add(provider)
    server = lower.get("server", "").lower()
    for hint, provider in CDN_SERVER_HINTS.items():
        if hint in server:
            found.add(provider)
    via = lower.get("via", "").lower()
    if any(h in via for h in ("cloudfront", "varnish", "google", "akamai", "fastly")):
        found.add(f"via: {lower['via'][:60]}")
    if "x-cache" in lower and ("hit" in lower["x-cache"].lower() or "miss" in lower["x-cache"].lower()):
        found.add("Caching proxy (x-cache)")
    return sorted(found)


def _strip_cookie_value(set_cookie: str) -> str:
    """Keeps the cookie name and attributes, drops the value (may be a session token)."""
    first, _, attrs = set_cookie.partition(";")
    name = first.split("=", 1)[0].strip()
    return f"{name}=<value>;{attrs}" if attrs else f"{name}=<value>"


def _client(**kwargs: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, http2=True, **kwargs)


async def _redirect_chain(url: str) -> dict[str, Any]:
    """Follows redirects manually to record every hop."""
    chain: list[dict[str, Any]] = []
    current = url
    async with _client(follow_redirects=False, verify=False) as client:
        for _ in range(10):
            try:
                resp = await client.get(current)
            except httpx.HTTPError as exc:
                return {"chain": chain, "error": f"{type(exc).__name__}: {exc}", "final_url": current}
            chain.append({"url": current, "status": resp.status_code})
            location = resp.headers.get("location")
            if resp.is_redirect and location:
                current = urljoin(current, location)
                continue
            return {"chain": chain, "final_url": current, "status": resp.status_code}
    return {"chain": chain, "final_url": current, "error": "too many redirects"}


async def _ttfb_samples(url: str) -> dict[str, Any]:
    """TTFB over a warm connection (server time + ~1 RTT), sampled sequentially.
    The first request (cold: DNS+TCP+TLS) is reported separately."""
    samples: list[float] = []
    cold_ms = None
    async with _client(follow_redirects=True) as client:
        for i in range(TTFB_SAMPLES + 1):
            start = time.perf_counter()
            try:
                async with client.stream("GET", url) as resp:
                    elapsed = (time.perf_counter() - start) * 1000
                    await resp.aread()
            except httpx.HTTPError:
                continue
            if i == 0:
                cold_ms = round(elapsed, 1)
            else:
                samples.append(round(elapsed, 1))
            await asyncio.sleep(0.5)
    out: dict[str, Any] = {"cold_ms": cold_ms, "warm_samples_ms": samples}
    if samples:
        out["median_ms"] = round(statistics.median(samples), 1)
        mean = statistics.mean(samples)
        out["cv"] = round(statistics.pstdev(samples) / mean, 3) if mean else 0.0
    return out


async def _fetch_text(client: httpx.AsyncClient, url: str, limit: int = 20000) -> tuple[int | None, str, str]:
    try:
        resp = await client.get(url)
    except httpx.HTTPError:
        return None, "", ""
    ctype = resp.headers.get("content-type", "")
    try:
        text = resp.text[:limit]
    except Exception:
        text = ""
    return resp.status_code, ctype, text


async def probe_http(target_url: str) -> dict[str, Any]:
    parsed = urlparse(target_url)
    host = parsed.hostname or ""
    https_url = target_url.replace("http://", "https://", 1)
    http_url = f"http://{host}{parsed.path or '/'}"

    result: dict[str, Any] = {"https_url": https_url}

    # 1) HTTP → HTTPS redirect behaviour and the HTTPS chain itself.
    result["http_redirect"] = await _redirect_chain(http_url)
    result["https_redirect"] = await _redirect_chain(https_url)

    # 2) Main HTTPS response (headers, cookies, protocol, compression).
    try:
        async with _client(follow_redirects=True) as client:
            resp = await client.get(https_url)
            result["https_ok"] = True
            result["final_url"] = str(resp.url)
            result["status"] = resp.status_code
            result["http_version"] = resp.http_version
            result["headers"] = {
                k.lower(): v for k, v in resp.headers.items() if k.lower() != "set-cookie"
            }
            result["set_cookie"] = [_strip_cookie_value(c) for c in resp.headers.get_list("set-cookie")]
            result["content_encoding"] = resp.headers.get("content-encoding", "")
            result["body_bytes"] = len(resp.content)
    except httpx.HTTPError as exc:
        result["https_ok"] = False
        result["https_error"] = f"{type(exc).__name__}: {exc}"
        return result

    result["cdn"] = detect_cdn(result["headers"])
    result["ttfb"] = await _ttfb_samples(result["final_url"])

    # 3) Well-known files and exposure probes on the final origin.
    final = urlparse(result["final_url"])
    origin = f"{final.scheme}://{final.netloc}"
    async with _client(follow_redirects=True) as client:
        status, ctype, body = await _fetch_text(client, origin + "/.well-known/security.txt")
        result["security_txt"] = {
            "status": status,
            "valid": status == 200 and "contact:" in body.lower() and "<html" not in body.lower(),
        }
        exposed = {}
        for path, looks_exposed in EXPOSED_FILE_PROBES.items():
            status, ctype, body = await _fetch_text(client, origin + path, limit=4000)
            exposed[path] = bool(status == 200 and "html" not in ctype and looks_exposed(body))
            await asyncio.sleep(0.3)
        result["exposed_files"] = exposed
        status, _, body = await _fetch_text(client, origin + "/robots.txt", limit=100000)
        result["robots_txt"] = body if status == 200 else ""

    return result
