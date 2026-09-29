"""PERF — Performance & Scalability *signals*, observed from outside. No load
testing is done (it would be abusive against a third-party SaaS)."""

from __future__ import annotations

from ..models import Evidence, not_evaluated, passed, scored
from .helpers import lerp, median, pages
from .registry import check


@check("PERF-01", "PERF", "Server response time (TTFB)", 15)
def ttfb(ev: Evidence):
    t = ev.http.get("ttfb") or {}
    med = t.get("median_ms")
    if med is None:
        return not_evaluated("TTFB could not be sampled.")
    return scored(
        lerp(med, 200, 800),
        f"Median TTFB {med:.0f} ms over {len(t.get('warm_samples_ms', []))} warm requests "
        f"(cold first request {t.get('cold_ms')} ms). Measured from the evaluator's location.",
    )


@check("PERF-02", "PERF", "Core Web Vitals on public pages (LCP, CLS, TBT)", 30)
def web_vitals(ev: Evidence):
    ok = [p for p in pages(ev) if p.vitals and p.kind != "app"]  # in-app pages: PERF-08
    if not ok:
        return not_evaluated("No public page vitals collected.")
    lcp = median([p.vitals.get("lcp_ms") for p in ok])
    cls = median([p.vitals.get("cls") for p in ok])
    tbt = median([p.vitals.get("tbt_ms") for p in ok])
    parts, detail = [], []
    if lcp is not None:
        parts.append(lerp(lcp, 2500, 4000))
        detail.append(f"LCP {lcp / 1000:.2f}s")
    if cls is not None:
        parts.append(lerp(cls, 0.1, 0.25))
        detail.append(f"CLS {cls:.3f}")
    if tbt is not None:
        parts.append(lerp(tbt, 200, 600))
        detail.append(f"TBT {tbt:.0f}ms")
    if not parts:
        return not_evaluated("Vitals unavailable.")
    return scored(sum(parts) / len(parts), f"Median over {len(ok)} pages (lab, desktop): {', '.join(detail)}.")


@check("PERF-03", "PERF", "Page weight and request count", 10)
def page_weight(ev: Evidence):
    p = ev.target if ev.target and ev.target.ok else (pages(ev) or [None])[0]
    if p is None:
        return not_evaluated("Target page not loaded.")
    mb = p.transfer_bytes / 1_000_000
    factor = (lerp(mb, 1.5, 5.0) + lerp(p.request_count, 50, 150)) / 2
    return scored(factor, f"{mb:.2f} MB transferred in {p.request_count} requests on {p.final_url or p.url}.")


@check("PERF-04", "PERF", "Compression and modern HTTP protocol", 10)
def compression_protocol(ev: Evidence):
    enc = (ev.http.get("content_encoding") or "").lower()
    protocols = {p.next_hop_protocol for p in pages(ev) if p.next_hop_protocol}
    http_version = ev.http.get("http_version", "")
    modern = any(pr in ("h2", "h3", "h3-29") for pr in protocols) or http_version == "HTTP/2"
    compressed = any(e in enc for e in ("br", "gzip", "zstd", "deflate"))
    factor = (0.5 if compressed else 0.0) + (0.5 if modern else 0.0)
    return scored(
        factor,
        f"Document encoding: {enc or 'none'}; protocols seen: {', '.join(sorted(protocols)) or http_version or 'unknown'}.",
    )


@check("PERF-05", "PERF", "Long-lived caching of static assets", 10)
def static_caching(ev: Evidence):
    total = sum(p.static_assets.get("total", 0) for p in pages(ev))
    cached = sum(p.static_assets.get("long_cache", 0) for p in pages(ev))
    if total == 0:
        return not_evaluated("No static assets observed.")
    ratio = cached / total
    return scored(min(1.0, ratio / 0.8), f"{ratio:.0%} of {total} static assets cached ≥ 7 days (or immutable).")


@check("PERF-06", "PERF", "CDN / edge network in front of the app", 15)
def cdn(ev: Evidence):
    providers = ev.http.get("cdn") or []
    if providers:
        return passed(f"Edge/CDN detected: {', '.join(providers)}.")
    return scored(0.0, "No CDN/edge signature in the main document's response headers.")


@check("PERF-07", "PERF", "Latency stability", 10)
def stability(ev: Evidence):
    t = ev.http.get("ttfb") or {}
    cv = t.get("cv")
    if cv is None or len(t.get("warm_samples_ms", [])) < 3:
        return not_evaluated("Not enough TTFB samples.")
    return scored(lerp(cv, 0.15, 0.6), f"TTFB coefficient of variation {cv:.2f} across samples {t.get('warm_samples_ms')}.")


@check("PERF-08", "PERF", "In-app page speed (logged in)", 20)
def in_app_speed(ev: Evidence):
    if not ev.login:
        return na("Not applicable without login.")
    app = [p for p in ev.rendered_pages() if p.kind == "app" and p.vitals]
    if not app:
        return not_evaluated("No in-app page could be loaded after login.")
    lcp = median([p.vitals.get("lcp_ms") for p in app])
    tbt = median([p.vitals.get("tbt_ms") for p in app])
    load = median([p.vitals.get("load_ms") for p in app])
    parts, detail = [], []
    if lcp is not None:
        parts.append(lerp(lcp, 2500, 4000))
        detail.append(f"LCP {lcp / 1000:.2f}s")
    if tbt is not None:
        parts.append(lerp(tbt, 200, 600))
        detail.append(f"TBT {tbt:.0f}ms")
    if load is not None:
        parts.append(lerp(load, 3000, 8000))
        detail.append(f"full load {load / 1000:.2f}s")
    if not parts:
        return not_evaluated("In-app vitals unavailable.")
    slowest = max(app, key=lambda p: p.vitals.get("load_ms") or 0)
    return scored(
        sum(parts) / len(parts),
        f"Median over {len(app)} in-app pages: {', '.join(detail)}. Slowest: {slowest.final_url or slowest.url} "
        f"({(slowest.vitals.get('load_ms') or 0) / 1000:.1f}s).",
    )
