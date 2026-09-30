"""In-process MCP tools for the AI judge (claude-agent-sdk). The judge can only
read evidence already collected (plus a few extra first-party pages) and submit
verdicts; every verdict goes through `verify.verify` before it is stored."""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx
from claude_agent_sdk import create_sdk_mcp_server, tool
from pydantic import ValidationError

from ..checks.registry import CheckDef
from ..collectors.crawl import html_to_text
from ..collectors.http_probe import HEADERS, TIMEOUT
from ..collectors.urls import registered_domain
from ..models import Evidence, PageEvidence
from ..storage.events import EventLog
from .verify import Verdict, verify

SERVER_NAME = "judge"
READ_CHUNK = 15000
MAX_EXTRA_FETCHES = 8
MAX_REJECTIONS_PER_CHECK = 3


@dataclass
class JudgeContext:
    session: str
    ev: Evidence
    checks: dict[str, CheckDef]
    log: EventLog
    progress: Callable[[str], None]
    allow_screenshots: bool = False
    verdicts: dict[str, dict[str, Any]] = field(default_factory=dict)
    rejections: dict[str, int] = field(default_factory=dict)
    fetched: int = 0
    finished: bool = False

    @property
    def pending(self) -> list[str]:
        return [cid for cid in self.checks if cid not in self.verdicts]


def _ok(text: str, *extra: dict[str, Any]) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}, *extra]}


def _err(text: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def _find_page(ev: Evidence, ref: str) -> PageEvidence | None:
    ref = (ref or "").strip()
    pages = ev.ok_pages()
    for p in pages:
        if p.kind == ref:
            return p
    norm = lambda u: re.sub(r"^https?://(www\.)?", "", u.split("#")[0]).rstrip("/")  # noqa: E731
    for p in pages:
        if norm(ref) in (norm(p.url), norm(p.final_url or p.url)):
            return p
    return None


VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "check_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["pass", "partial", "fail", "not_found"]},
                    "factor": {"type": "number", "description": "0-1, only for 'partial' (how much of the rubric is met)"},
                    "reason": {"type": "string", "description": "one or two sentences"},
                    "quotes": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"url": {"type": "string"}, "quote": {"type": "string"}},
                                  "required": ["url", "quote"]},
                        "description": "verbatim excerpts copied exactly from read_page/search_pages output",
                    },
                },
                "required": ["check_id", "verdict", "reason", "quotes"],
            },
        }
    },
    "required": ["verdicts"],
}


def handle_verdicts(ctx: JudgeContext, args: dict[str, Any]) -> dict[str, Any]:
    """Validates, verifies (quote guardrail) and stores submitted verdicts.
    Shared by the judge and the in-app explorer tool servers."""
    ev = ctx.ev
    lines = []
    for raw in args.get("verdicts") or []:
        try:
            v = Verdict.model_validate(raw)
        except ValidationError as exc:
            lines.append(f"- INVALID {raw.get('check_id', '?')}: {exc.errors()[0]['msg']}")
            continue
        if v.check_id not in ctx.checks:
            lines.append(f"- {v.check_id}: not a check of this session ({', '.join(ctx.checks)}).")
            continue
        accepted, why, quotes = verify(v, ev)
        record = {**v.model_dump(), "quotes": quotes, "verified": accepted, "session": ctx.session}
        if accepted:
            ctx.verdicts[v.check_id] = record
            ctx.log.log("verdict", {"session": ctx.session, "id": v.check_id, "verdict": v.verdict, "factor": v.factor})
            ctx.progress(f"AI ({ctx.session}): {v.check_id} → {v.verdict}")
            lines.append(f"- {v.check_id}: accepted ({v.verdict}).")
        else:
            ctx.rejections[v.check_id] = ctx.rejections.get(v.check_id, 0) + 1
            ctx.log.log("verdict_rejected", {"session": ctx.session, "id": v.check_id, "reason": why, "quotes": quotes})
            if ctx.rejections[v.check_id] >= MAX_REJECTIONS_PER_CHECK:
                ctx.verdicts[v.check_id] = {**record, "rejection": why}
                lines.append(f"- {v.check_id}: REJECTED ({why}). No retries left for this check.")
            else:
                lines.append(f"- {v.check_id}: REJECTED ({why}). Re-read the page and copy the exact text, or submit fail/not_found.")
    pending = ctx.pending
    lines.append(f"\nStill pending: {', '.join(pending)}" if pending else "\nAll checks judged — call finish.")
    return _ok("\n".join(lines))


def build_judge_server(ctx: JudgeContext):
    ev = ctx.ev

    @tool("list_pages", "List the pages collected for this evaluation (kind, URL, title, size, screenshot).",
          {"type": "object", "properties": {}, "required": []})
    async def list_pages(args: dict[str, Any]) -> dict[str, Any]:
        rows = []
        for p in ev.ok_pages():
            shot = " [screenshot]" if (p.screenshot and ctx.allow_screenshots) else ""
            rows.append(f"- {p.kind}: {p.final_url or p.url} — \"{p.title[:80]}\" ({len(p.text)} chars){shot}")
        return _ok("Collected pages:\n" + "\n".join(rows))

    @tool("read_page", "Read the visible text of a collected page, by kind (e.g. 'pricing') or URL. "
          "Long pages are paginated: pass `offset` to continue.",
          {"type": "object", "properties": {"page": {"type": "string"}, "offset": {"type": "integer"}},
           "required": ["page"]})
    async def read_page(args: dict[str, Any]) -> dict[str, Any]:
        page = _find_page(ev, args.get("page", ""))
        if page is None:
            return _err(f"No collected page matches {args.get('page')!r}. Use list_pages, or fetch_page for a new URL.")
        offset = max(0, int(args.get("offset") or 0))
        chunk = page.text[offset: offset + READ_CHUNK]
        more = offset + READ_CHUNK < len(page.text)
        tail = f"\n\n[… {len(page.text) - offset - READ_CHUNK} more chars — call read_page with offset={offset + READ_CHUNK}]" if more else ""
        return _ok(f"URL: {page.final_url or page.url}\nTitle: {page.title}\n\n{chunk}{tail}")

    @tool("search_pages", "Case-insensitive search across ALL collected page texts. Returns matching snippets "
          "with their URL. `pattern` is a regular expression (e.g. 'SOC ?2|ISO ?27001').",
          {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]})
    async def search_pages(args: dict[str, Any]) -> dict[str, Any]:
        try:
            rx = re.compile(args["pattern"], re.I)
        except re.error as exc:
            return _err(f"Invalid regex: {exc}")
        hits = []
        for p in ev.ok_pages():
            for m in rx.finditer(p.text):
                start, end = max(0, m.start() - 160), min(len(p.text), m.end() + 160)
                snippet = p.text[start:end].replace("\n", " ")
                hits.append(f"- {p.final_url or p.url}: …{snippet}…")
                if len(hits) >= 25:
                    break
            if len(hits) >= 25:
                break
        return _ok("\n".join(hits) if hits else "No matches in the collected pages.")

    @tool("fetch_page", f"Fetch an additional page of the vendor (same organization domain only, max "
          f"{MAX_EXTRA_FETCHES} per session) — e.g. a SOC 2 or SLA page linked from a collected page.",
          {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
    async def fetch_page(args: dict[str, Any]) -> dict[str, Any]:
        url = (args.get("url") or "").strip()
        existing = _find_page(ev, url)
        if existing is not None:
            return await read_page({"page": url})
        if not url.startswith(("http://", "https://")):
            return _err("Provide an absolute http(s) URL.")
        allowed_hosts = {registered_domain(u) for u in ev.discovered.values()} | {ev.org_domain}
        if registered_domain(url) not in allowed_hosts:
            return _err(f"Only pages of {ev.org_domain} (or its discovered status/docs hosts) can be fetched.")
        if ctx.fetched >= MAX_EXTRA_FETCHES:
            return _err("Fetch budget exhausted — judge with what was collected.")
        ctx.fetched += 1
        ctx.progress(f"AI ({ctx.session}): fetching {url}")
        try:
            async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(url)
        except httpx.HTTPError as exc:
            return _err(f"Fetch failed: {type(exc).__name__}")
        if resp.status_code >= 400 or "html" not in resp.headers.get("content-type", ""):
            return _err(f"HTTP {resp.status_code} ({resp.headers.get('content-type', '?')}) — not a readable page.")
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", resp.text)
        page = PageEvidence(url=url, kind="extra", final_url=str(resp.url), status=resp.status_code,
                            title=m.group(1).strip() if m else "", text=html_to_text(resp.text)[:60000], rendered=False)
        ev.pages.append(page)
        ctx.log.log("action", {"session": ctx.session, "tool": "fetch_page", "url": url, "status": resp.status_code})
        return _ok(f"URL: {page.final_url}\nTitle: {page.title}\n\n{page.text[:READ_CHUNK]}")

    @tool("view_screenshot", "View the screenshot of a collected page (by kind or URL) to judge its visual design.",
          {"type": "object", "properties": {"page": {"type": "string"}}, "required": ["page"]})
    async def view_screenshot(args: dict[str, Any]) -> dict[str, Any]:
        if not ctx.allow_screenshots:
            return _err("Screenshots are not available in this session.")
        ref = args.get("page", "")
        page = _find_page(ev, ref)
        shot = page.screenshot if page else None
        if shot is None and ev.a11y:  # viewport shots from --a11y (mobile/tablet/desktop)
            vp = (ev.a11y.get("viewports") or {}).get(ref.replace("viewport-", ""))
            shot = vp.get("screenshot") if vp else None
        shot_path = ctx.log.paths.run_dir / shot if shot else None
        if shot_path is None or not shot_path.exists():
            return _err(f"No screenshot for {ref!r}.")
        data = base64.b64encode(shot_path.read_bytes()).decode("ascii")
        return _ok(f"Screenshot of {page.final_url if page else ref}:", {"type": "image", "data": data, "mimeType": "image/jpeg"})

    @tool("submit_verdicts", "Submit one or more verdicts. pass/partial verdicts MUST include verbatim quotes "
          "copied from the page text (they are machine-verified; unverifiable verdicts are rejected).", VERDICT_SCHEMA)
    async def submit_verdicts(args: dict[str, Any]) -> dict[str, Any]:
        return handle_verdicts(ctx, args)

    @tool("finish", "End the session once every check has a verdict.", {"type": "object", "properties": {}, "required": []})
    async def finish(args: dict[str, Any]) -> dict[str, Any]:
        ctx.finished = True
        return _ok("Session finished." + (f" Unjudged: {', '.join(ctx.pending)}" if ctx.pending else ""))

    tools = [list_pages, read_page, search_pages, fetch_page, submit_verdicts, finish]
    if ctx.allow_screenshots:
        tools.append(view_screenshot)
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools), [t.name for t in tools]
