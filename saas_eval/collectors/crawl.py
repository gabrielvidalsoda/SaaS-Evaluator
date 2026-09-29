"""Collection phase orchestrator — gathers all deterministic evidence for a target.

Network probes (HTTP/TLS/DNS) run concurrently with the first browser visits;
then discovery, the budgeted crawl of public key pages, a sampled broken-link
check, login + the authenticated in-app crawl (when a login method is given),
and (only with --a11y) the accessibility collector."""

from __future__ import annotations

import asyncio
import random
import re
from dataclasses import dataclass
from html import unescape
from typing import Any, Awaitable, Callable
from urllib import robotparser
from urllib.parse import urlparse

import httpx

from ..credentials import Credentials
from ..models import Evidence, PageEvidence
from ..storage.events import EventLog
from ..storage.files import RunPaths
from .auth import app_links, looks_logged_out, manual_login, scripted_login, token_keys_in_local_storage
from .browser import DESKTOP_VIEWPORT, MAX_TEXT_CHARS, BrowserCollector
from .discovery import CRAWL_PRIORITY, classify_links, probe_missing
from .dns_probe import probe_dns
from .http_probe import HEADERS, TIMEOUT, USER_AGENT, probe_http
from .tls_probe import probe_tls
from .urls import host_of, registered_domain, strip_fragment

LINK_CHECK_SAMPLE = 25
LOGIN_PATH_RX = re.compile(r"/(login|signin|sign-in|log-in|entrar)(/|$)", re.I)
PAGE_PAUSE_S = 1.0  # between top-level navigations (politeness)


Progress = Callable[[str], None]
# (login_url, credentials, evidence) -> (storage_state | None, info) — AI login fallback.
AiLogin = Callable[[str, Credentials, Evidence], Awaitable[tuple[dict[str, Any] | None, dict[str, Any]]]]


@dataclass
class CollectOptions:
    max_pages: int
    headless: bool
    a11y: bool
    login: str = "none"  # none | manual | credentials
    credentials: Credentials | None = None
    app_pages: int = 8
    prompt: Callable[[str], Awaitable[None]] | None = None  # asks the user (manual login)
    ai_login: AiLogin | None = None


def _robots(robots_txt: str) -> robotparser.RobotFileParser | None:
    if not robots_txt:
        return None
    parser = robotparser.RobotFileParser()
    parser.parse(robots_txt.splitlines())
    return parser


async def _check_links(urls: list[str]) -> dict[str, object]:
    """Status of a sample of same-site links. 401/403/429 are inconclusive (auth or
    bot protection), not broken."""
    results: dict[str, int | None] = {}
    sem = asyncio.Semaphore(4)
    async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:

        async def check(url: str) -> None:
            async with sem:
                try:
                    resp = await client.head(url)
                    if resp.status_code in (405, 501) or resp.status_code >= 400:
                        resp = await client.get(url)  # some servers mishandle HEAD
                    results[url] = resp.status_code
                except httpx.HTTPError:
                    results[url] = None
                await asyncio.sleep(0.2)

        await asyncio.gather(*(check(u) for u in urls))
    broken = {u: s for u, s in results.items() if s is not None and (s in (404, 410) or s >= 500)}
    inconclusive = {u: s for u, s in results.items() if s is None or s in (401, 403, 429)}
    return {"checked": len(results), "broken": broken, "inconclusive": len(inconclusive)}


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|template)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|section|article)>", "\n", html)
    text = unescape(re.sub(r"(?s)<[^>]+>", " ", html))
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n", text).strip()


async def _fetch_text_pages(targets: dict[str, str]) -> list[PageEvidence]:
    """Discovered pages that didn't fit the browser budget are still fetched (plain
    GET, no JS) so content checks and the AI judge can read them."""
    out: list[PageEvidence] = []
    async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
        for kind, url in targets.items():
            page = PageEvidence(url=url, kind=kind, rendered=False)
            try:
                resp = await client.get(url)
                page.status = resp.status_code
                page.final_url = str(resp.url)
                if "html" in resp.headers.get("content-type", ""):
                    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", resp.text)
                    page.title = unescape(m.group(1).strip()) if m else ""
                    page.text = html_to_text(resp.text)[:MAX_TEXT_CHARS]
            except httpx.HTTPError as exc:
                page.error = f"{type(exc).__name__}: {exc}"
            out.append(page)
            await asyncio.sleep(0.3)
    return out


async def _authenticate(
    collector: BrowserCollector, ev: Evidence, target_url: str, opts: CollectOptions, log: EventLog, progress: Progress
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    if opts.login == "manual":
        progress("Waiting for manual login")
        state, info = await manual_login(collector, target_url, opts.prompt)
        info["method"] = "manual"
        return state, info

    creds = opts.credentials
    progress(f"Logging in as {creds.username}")
    context = await collector.new_context(DESKTOP_VIEWPORT)
    try:
        info = await scripted_login(context, target_url, creds)
        info["method"] = "credentials (form fill)"
        state = await context.storage_state() if info["success"] else None
    finally:
        await context.close()
    if state is None and opts.ai_login is not None:
        log.log("collect", {"probe": "login", "scripted": info["note"], "fallback": "ai"}, level="WARN")
        progress("Form login failed — trying the AI login agent")
        state, info = await opts.ai_login(creds.login_url or target_url, creds, ev)
        info["method"] = "credentials (AI agent)"
    return state, info


async def _crawl_app(
    collector: BrowserCollector, ev: Evidence, start_url: str, opts: CollectOptions, paths: RunPaths,
    log: EventLog, progress: Progress,
) -> None:
    """Visits the app's own pages with the logged-in session (kind "app"), following
    only safe same-host navigation links (see auth.app_links)."""
    queue = [start_url]
    seen: set[str] = set()
    app_host = host_of(start_url)
    visited = 0
    while queue and visited < opts.app_pages:
        url = queue.pop(0)
        progress(f"In-app page {visited + 1}/{opts.app_pages}: {url}")
        page = await collector.visit(url, "app", paths.screenshots_dir / f"app-{visited:02d}.jpg")
        visited += 1
        if await looks_logged_out(collector.page):
            page.error = "session lost (redirected to login)"
            ev.pages.append(page)
            log.log("page", {"kind": "app", "url": url, "error": page.error}, level="WARN")
            ev.login["session_lost"] = True
            return
        ev.pages.append(page)
        log.log("page", {"kind": "app", "url": url, "status": page.status, "error": page.error},
                level="WARN" if page.error or (page.status or 0) >= 400 else "INFO")
        if visited == 1:
            app_host = host_of(page.final_url or url)
            seen.add(urlparse(page.final_url or url).path)
        queue += [u for u in app_links(page.links, app_host, seen) if u not in queue]
        await asyncio.sleep(PAGE_PAUSE_S)


async def collect(
    target_url: str, opts: CollectOptions, paths: RunPaths, log: EventLog, progress: Progress
) -> tuple[Evidence, dict[str, Any] | None]:
    """Returns the evidence and the authenticated session (storage_state, in memory
    only — never persisted) for the AI explorer."""
    host = host_of(target_url)
    org_domain = registered_domain(host)
    ev = Evidence(target_url=target_url, host=host, org_domain=org_domain)
    log.phase = "collect"

    progress("Probing HTTP, TLS and DNS")
    network = asyncio.gather(
        probe_http(target_url), probe_tls(host), probe_dns(org_domain), return_exceptions=True
    )

    collector = BrowserCollector(headless=opts.headless, org_domain=org_domain)
    await collector.start()
    try:
        visited: set[str] = set()

        async def visit(url: str, kind: str) -> None:
            progress(f"Visiting {kind}: {url}")
            page = await collector.visit(url, kind, paths.screenshots_dir / f"{len(ev.pages):02d}-{kind}.jpg")
            ev.pages.append(page)
            visited.add(strip_fragment(url))
            if page.final_url:
                visited.add(strip_fragment(page.final_url))
            log.log("page", {"kind": kind, "url": url, "status": page.status, "error": page.error},
                    level="WARN" if page.error or (page.status or 0) >= 400 else "INFO")
            await asyncio.sleep(PAGE_PAUSE_S)

        await visit(target_url, "target")
        # The target is often the app/login host (app.acme.com); the vendor's public
        # site lives on the organization's root domain — visit it too.
        home_url = f"https://{org_domain}/"
        target = ev.target
        target_final_host = host_of(target.final_url) if target and target.final_url else host
        if target_final_host not in (org_domain, f"www.{org_domain}"):
            await visit(home_url, "home")

        http, tls, dns = await network
        for name, value in (("http", http), ("tls", tls), ("dns", dns)):
            if isinstance(value, Exception):
                log.log("collect_error", {"probe": name, "error": f"{type(value).__name__}: {value}"})
                value = {"error": f"{type(value).__name__}: {value}"}
            setattr(ev, name, value)
        log.log("collect", {"probe": "network", "https_ok": ev.http.get("https_ok"), "cdn": ev.http.get("cdn")})

        # Discovery: links first, then conventional locations for what's missing.
        progress("Discovering key pages")
        ev.discovered = classify_links(ev.all_links(), org_domain)
        missing = [k for k in CRAWL_PRIORITY if k not in ev.discovered]
        home_page = ev.page("home") or ev.page("target")
        www_host = host_of(home_page.final_url) if home_page and home_page.final_url else org_domain
        if registered_domain(www_host) != org_domain:
            www_host = org_domain
        probed = await probe_missing(missing, org_domain, www_host)
        ev.discovered.update(probed)
        log.log("discovery", {"from_links": sorted(set(ev.discovered) - set(probed)), "from_probes": sorted(probed)})

        robots = _robots(ev.http.get("robots_txt", "")) if isinstance(ev.http, dict) else None
        target_host = urlparse(ev.http.get("final_url", target_url)).hostname if isinstance(ev.http, dict) else host
        budget = max(0, opts.max_pages - len(ev.pages))
        for kind in CRAWL_PRIORITY:
            if budget <= 0:
                break
            url = ev.discovered.get(kind)
            if not url or strip_fragment(url) in visited:
                continue
            if robots and host_of(url) == target_host and not robots.can_fetch(USER_AGENT, url):
                log.log("page", {"kind": kind, "url": url, "skipped": "disallowed by robots.txt"}, level="WARN")
                continue
            await visit(url, kind)
            budget -= 1

        leftovers = {
            k: ev.discovered[k] for k in CRAWL_PRIORITY
            if k in ev.discovered and k not in {p.kind for p in ev.pages}
            and strip_fragment(ev.discovered[k]) not in visited
        }
        if leftovers:
            progress(f"Fetching {len(leftovers)} more pages as text (outside the browser budget)")
            ev.pages += await _fetch_text_pages(leftovers)

        # Broken links: a sample of same-site links not already visited.
        candidates = sorted({
            strip_fragment(link["href"]) for link in ev.all_links()
            if link["href"].startswith("http") and registered_domain(link["href"]) == org_domain
            and strip_fragment(link["href"]) not in visited
        })
        random.Random(0).shuffle(candidates)  # deterministic sample across runs
        progress(f"Checking {min(len(candidates), LINK_CHECK_SAMPLE)} links")
        ev.link_checks = await _check_links(candidates[:LINK_CHECK_SAMPLE])

        ev.cookies = await collector.cookies()  # public (anonymous) cookies

        state: dict[str, Any] | None = None
        if opts.login != "none":
            log.phase = "auth"
            state, info = await _authenticate(collector, ev, target_url, opts, log, progress)
            ev.login = {"requested": True, **info}
            log.log("collect", {"probe": "login", "method": info.get("method"), "success": info["success"],
                                "note": info["note"]}, level="INFO" if info["success"] else "WARN")
            if state is not None:
                await collector.use_session(state)
                ev.login["token_keys_in_local_storage"] = token_keys_in_local_storage(state, org_domain)
                # The target itself (e.g. /dashboard) is the natural entry point of the app.
                start = info.get("final_url", target_url) if LOGIN_PATH_RX.search(urlparse(target_url).path) else target_url
                await _crawl_app(collector, ev, start, opts, paths, log, progress)
                ev.login["app_pages"] = sum(1 for p in ev.pages if p.kind == "app" and p.ok)
                ev.cookies += [{**c, "after_login": True} for c in await collector.cookies()]
            log.phase = "collect"

        if opts.a11y:
            from .a11y import collect_a11y

            progress("Running accessibility & multi-device checks")
            app = [p.final_url for p in ev.ok_pages() if p.kind == "app"]
            a11y_target = app[0] if app else (ev.target.final_url or target_url)
            public = [p.final_url for p in ev.ok_pages() if p.kind in ("home", "login", "pricing", "signup")]
            ev.a11y = await collect_a11y(collector, a11y_target, app[1:3] + public[:2], paths.screenshots_dir)
            log.log("collect", {"probe": "a11y", "errors": ev.a11y.get("errors")})
    finally:
        await collector.close()
    return ev, state
