"""AI judgement phase: two evidence-bound Claude sessions run in parallel.

- "claims": reads collected page texts to judge rubric items like SOC 2, SLA, DPA,
  webhooks, trial, cancellation terms…
- "ux": looks at screenshots + page text to judge usability heuristics.

Plus, after scoring, a short executive summary. The AI never sets scores: it
returns verified verdicts, and the check registry / scoring engine do the math."""

from __future__ import annotations

import asyncio
import hashlib
from typing import Any, Callable

from ..checks.registry import CheckDef, ai_checks
from ..config import Settings
from ..models import Evidence
from ..storage.events import EventLog
from ..storage.files import RunPaths, atomic_write_json
from .agent import load_prompt, run_session
from .tools import SERVER_NAME, JudgeContext, build_judge_server

UX_SESSION_CHECKS = {"UX-01", "UX-02", "UX-05"}
EXPLORER_CHECKS = {"UX-07"}  # judged by the in-app explorer (--login), not here


def _checks_block(checks: list[CheckDef], ev: Evidence) -> str:
    lines = []
    for c in checks:
        suggested = [k for k in c.pages if ev.page(k)] or list(c.pages)
        lines.append(f"### {c.id} — {c.title}\nRubric: {c.rubric}\nLook first at: {', '.join(suggested) or 'any'}\n")
    return "\n".join(lines)


def _intro(ev: Evidence, checks: list[CheckDef], session: str) -> str:
    pages = "\n".join(
        f"- {p.kind}: {p.final_url or p.url} ({len(p.text)} chars{', screenshot' if p.screenshot and session == 'ux' else ''})"
        for p in ev.ok_pages()
    )
    return (
        f"Vendor under evaluation: {ev.org_domain} (target URL: {ev.target_url})\n\n"
        f"Collected pages:\n{pages}\n\n"
        f"Judge these {len(checks)} checks:\n\n{_checks_block(checks, ev)}\n"
        "Submit verdicts with submit_verdicts (batch several per call), then call finish."
    )


async def _run_judge(
    session: str,
    checks: list[CheckDef],
    ev: Evidence,
    log: EventLog,
    settings: Settings,
    system_prompt: str,
    progress: Callable[[str], None],
) -> JudgeContext:
    ctx = JudgeContext(
        session=session, ev=ev, checks={c.id: c for c in checks}, log=log, progress=progress,
        allow_screenshots=session == "ux",
    )
    if not checks:
        return ctx
    server, tool_names = build_judge_server(ctx)
    log.log("ai_start", {"session": session, "checks": list(ctx.checks), "model": settings.model})
    progress(f"AI ({session}): judging {len(checks)} checks")
    try:
        await run_session(
            system_prompt=system_prompt,
            user_prompt=_intro(ev, checks, session),
            model=settings.model,
            max_turns=40 + 3 * len(checks),
            timeout_s=900,
            mcp_server=server,
            server_name=SERVER_NAME,
            tool_names=tool_names,
            should_stop=lambda: ctx.finished,
        )
    except RuntimeError:
        raise  # setup problem (CLI missing / not logged in) — abort the run with the actionable message
    except Exception as exc:  # a judge crash must not lose the deterministic results
        log.log("ai_error", {"session": session, "error": f"{type(exc).__name__}: {exc}"})
        progress(f"AI ({session}) failed: {type(exc).__name__}")
    return ctx


async def run_ai_judges(
    ev: Evidence,
    pillars: list[str],
    paths: RunPaths,
    log: EventLog,
    settings: Settings,
    progress: Callable[[str], None],
    headless: bool = True,
    storage_state: dict[str, Any] | None = None,
) -> dict[str, Any]:
    system_prompt, _ = load_prompt(settings.prompts_dir / "judge.md")
    checks = [c for c in ai_checks(pillars) if c.id not in EXPLORER_CHECKS]
    ux = [c for c in checks if c.id in UX_SESSION_CHECKS]
    claims = [c for c in checks if c.id not in UX_SESSION_CHECKS]

    results = await asyncio.gather(
        _run_judge("claims", claims, ev, log, settings, system_prompt, progress),
        _run_judge("ux", ux, ev, log, settings, system_prompt, progress),
    )
    for ctx in results:
        ev.ai_verdicts.update(ctx.verdicts)

    explorer_meta = None
    if storage_state is not None and "UX" in pillars:
        from .explorer import run_explorer

        explorer_meta = await run_explorer(ev, storage_state, paths, log, settings, progress, headless=headless)

    atomic_write_json(paths.ai_cache_dir / "verdicts.json", ev.ai_verdicts)
    accepted = sum(1 for v in ev.ai_verdicts.values() if v.get("verified"))
    return {
        "model": settings.model,
        "prompt_hash": prompt_fingerprint(settings),
        "checks_requested": len(checks),
        "verdicts_accepted": accepted,
        "verdicts_rejected": sum(ctx.rejections.get(cid, 0) > 0 for ctx in results for cid in ctx.checks),
        "extra_pages_fetched": sum(ctx.fetched for ctx in results),
        "explorer": explorer_meta,
    }


async def write_summary(doc: dict[str, Any], settings: Settings) -> str:
    """Executive summary grounded in the computed scores (no tools, one turn)."""
    system_prompt, _ = load_prompt(settings.prompts_dir / "summary.md")
    ev = doc["evaluation"]
    lines = [f"Vendor: {doc['run']['host']} — overall {ev['grade']} ({ev['overall']}/100), coverage {ev['coverage']:.0%}."]
    for p in ev["pillars"]:
        lines.append(f"\n{p['name']}: {p['score']} (coverage {p['coverage']:.0%})")
        for c in p["checks"]:
            if c["status"] in ("pass", "partial", "fail"):
                lines.append(f"  - [{c['status']}] {c['title']}: {c['detail'][:200]}")
    if ev["dealbreakers"]:
        lines.append("\nDealbreakers: " + "; ".join(ev["dealbreakers"]))
    text = await run_session(
        system_prompt=system_prompt,
        user_prompt="\n".join(lines),
        model=settings.model,
        max_turns=1,
        timeout_s=180,
    )
    return text.strip()


def prompt_fingerprint(settings: Settings) -> str:
    h = hashlib.sha256()
    for name in ("judge.md", "summary.md", "explorer.md", "login.md"):
        path = settings.prompts_dir / name
        if path.exists():
            h.update(path.read_bytes())
    return h.hexdigest()[:12]
