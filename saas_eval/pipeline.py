"""End-to-end evaluation of one target: collect → (AI judge) → checks → score → report."""

from __future__ import annotations

import asyncio
import gc
from dataclasses import asdict, dataclass
from typing import Any

from rich.console import Console

from .checks.registry import run_checks
from .collectors.crawl import CollectOptions, collect
from .collectors.urls import host_of, normalize_target
from .config import Settings
from .credentials import Credentials
from .models import active_pillars
from .report.markdown import render_markdown
from .scoring.engine import grade_for, load_profile, score_evaluation
from .storage.events import EventLog
from .storage.files import RunPaths, atomic_write_json, create_run, finalize_run, read_json, slugify
from .terminal import Reporter


@dataclass
class EvaluateOptions:
    deterministic: bool = False
    a11y: bool = False
    profile: str = "default"
    max_pages: int = 12
    mode: str = "background"  # background (headless) | visible (headed browser + detailed log)
    login: str = "none"  # none | manual | credentials
    credentials: Credentials | None = None
    app_pages: int = 8


def mode_label(opts: EvaluateOptions) -> str:
    return "deterministic only" if opts.deterministic else "deterministic + AI"


def evaluate_target(url: str, opts: EvaluateOptions, settings: Settings, console: Console) -> dict[str, Any]:
    target = normalize_target(url)
    profile = load_profile(opts.profile, settings.profiles_dir)
    pillars = active_pillars(opts.a11y)
    host = host_of(target)

    paths = create_run(
        settings.data_dir,
        target,
        slugify(host),
        {
            "host": host,
            "mode": "deterministic" if opts.deterministic else "deterministic+ai",
            "mode_label": mode_label(opts),
            "pillars": pillars,
            "a11y": opts.a11y,
            "profile": profile["name"],
            "max_pages": opts.max_pages,
            "login": opts.login,
            "login_user": opts.credentials.username if opts.credentials else None,
        },
    )
    log = EventLog(paths)
    log.log("run_start", {"target_url": target, "mode": mode_label(opts), "pillars": pillars, "profile": profile["name"]})
    reporter = Reporter(console, verbose=opts.mode == "visible")
    reporter.start(target, mode_label(opts), pillars, paths.run_id)

    try:
        doc = asyncio.run(_run(target, opts, settings, profile, pillars, paths, log, reporter))
    except KeyboardInterrupt:
        log.log("fatal_error", {"error": "interrupted by user (Ctrl+C)"})
        finalize_run(paths, status="interrupted")
        raise
    except Exception as exc:
        log.log("fatal_error", {"error": f"{type(exc).__name__}: {exc}"})
        finalize_run(paths, status="failed", error=str(exc))
        raise
    reporter.result(doc, str(paths.report_md))
    return doc


async def _run(
    target: str,
    opts: EvaluateOptions,
    settings: Settings,
    profile: dict[str, Any],
    pillars: list[str],
    paths: RunPaths,
    log: EventLog,
    reporter: Reporter,
) -> dict[str, Any]:
    """All async work in ONE event loop: on Windows, claude CLI subprocess transports
    finalized after their loop closed print "unclosed transport" noise at exit."""
    try:
        return await _pipeline(target, opts, settings, profile, pillars, paths, log, reporter)
    finally:
        gc.collect()
        await asyncio.sleep(0.25)  # let finalized subprocess transports close inside the loop


async def _pipeline(
    target: str,
    opts: EvaluateOptions,
    settings: Settings,
    profile: dict[str, Any],
    pillars: list[str],
    paths: RunPaths,
    log: EventLog,
    reporter: Reporter,
) -> dict[str, Any]:
    headless = opts.mode != "visible"
    collect_opts = CollectOptions(
        max_pages=opts.max_pages,
        headless=headless,
        a11y=opts.a11y,
        login=opts.login,
        credentials=opts.credentials,
        app_pages=opts.app_pages,
        prompt=reporter.ask,
    )
    if opts.login == "credentials" and not opts.deterministic:
        from .ai.explorer import ai_login

        async def _ai_login(url: str, creds: Credentials, ev_: Any):
            return await ai_login(url, creds, ev_, paths, log, settings, reporter.progress, headless=headless)

        collect_opts.ai_login = _ai_login

    with reporter.phase("Collecting evidence"):
        ev, session = await collect(target, collect_opts, paths, log, reporter.progress)

    ai_meta = None
    ev.ai_enabled = not opts.deterministic
    if ev.ai_enabled:
        from .ai.judges import run_ai_judges

        log.phase = "ai"
        with reporter.phase("AI judgement"):
            ai_meta = await run_ai_judges(
                ev, pillars, paths, log, settings, reporter.progress, headless=headless, storage_state=session
            )
    del session  # the logged-in session never outlives the run and is never written to disk

    atomic_write_json(paths.evidence_json, asdict(ev))

    log.phase = "score"
    results = run_checks(ev, pillars)
    for r in results:
        log.log("check", {"id": r.id, "status": r.status, "earned": r.earned, "max": r.max_points})
    evaluation = score_evaluation(results, pillars, profile)
    log.log("score", {"overall": evaluation.overall, "grade": evaluation.grade, "coverage": evaluation.coverage})

    evaluation_doc = asdict(evaluation)
    for p in evaluation_doc["pillars"]:
        p["grade"] = grade_for(p["score"])

    doc: dict[str, Any] = {
        "run": {**read_json(paths.run_json), "ai": ai_meta},
        "evaluation": evaluation_doc,
        "discovered": ev.discovered,
        "login": ev.login,
        "app_pages": [
            {"url": p.final_url or p.url, "title": p.title, "status": p.status, "error": p.error,
             "load_ms": (p.vitals or {}).get("load_ms"), "screenshot": p.screenshot}
            for p in ev.pages if p.kind == "app"
        ],
        "summary": None,
    }
    if ev.ai_enabled:
        from .ai.judges import write_summary

        with reporter.phase("Writing executive summary"):
            try:
                doc["summary"] = await write_summary(doc, settings)
            except RuntimeError:
                raise
            except Exception as exc:  # the summary is optional; never lose the scores over it
                log.log("ai_error", {"session": "summary", "error": f"{type(exc).__name__}: {exc}"})

    atomic_write_json(paths.scores_json, doc)
    paths.report_md.write_text(render_markdown(doc), encoding="utf-8")
    finalize_run(paths, overall=evaluation.overall, grade=evaluation.grade, coverage=evaluation.coverage, ai=ai_meta)
    log.log("finish", {"overall": evaluation.overall, "grade": evaluation.grade})
    return doc
