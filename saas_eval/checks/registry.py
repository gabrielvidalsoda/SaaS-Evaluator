"""Check registry: every rubric item is a function decorated with `@check(...)`.

Check functions are pure — `Evidence -> Outcome` — and know nothing about AI or
run modes. The registry applies the mode rules:

- `auto`     : the function's result is final.
- `ai`       : the function may return `na` (not applicable, e.g. in-app UX without
               login) or None; otherwise the result comes from a verified AI verdict,
               or is `not_evaluated` when AI is off (--deterministic) or gave none.
- `auto+ai`  : the function's heuristic result, replaced by a verified AI verdict
               when one exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from ..models import PILLARS, CheckResult, Evidence, Outcome, not_evaluated

CheckFunc = Callable[[Evidence], "Outcome | None"]


@dataclass(frozen=True)
class CheckDef:
    id: str
    pillar: str
    title: str
    max_points: float
    method: str  # auto | ai | auto+ai
    func: CheckFunc
    critical: bool = False
    rubric: str = ""  # what the AI judge must decide (ai / auto+ai checks)
    pages: tuple[str, ...] = field(default_factory=tuple)  # page kinds relevant to the AI judge


REGISTRY: dict[str, CheckDef] = {}


def check(
    id: str,
    pillar: str,
    title: str,
    points: float,
    method: str = "auto",
    critical: bool = False,
    rubric: str = "",
    pages: tuple[str, ...] = (),
) -> Callable[[CheckFunc], CheckFunc]:
    def deco(func: CheckFunc) -> CheckFunc:
        if id in REGISTRY:
            raise ValueError(f"duplicate check id {id}")
        REGISTRY[id] = CheckDef(id, pillar, title, points, method, func, critical, rubric, pages)
        return func

    return deco


def _load_all() -> None:
    # Importing the modules registers their checks.
    from . import (  # noqa: F401
        accessibility,
        data_governance,
        integration,
        performance,
        reliability,
        security,
        transparency,
        usability,
    )


def checks_for(pillars: list[str]) -> list[CheckDef]:
    _load_all()
    order = list(PILLARS)
    return sorted(
        (d for d in REGISTRY.values() if d.pillar in pillars),
        key=lambda d: (order.index(d.pillar), d.id),
    )


def ai_checks(pillars: list[str]) -> list[CheckDef]:
    return [d for d in checks_for(pillars) if d.method in ("ai", "auto+ai")]


def verdict_outcome(verdict: dict) -> Outcome:
    """Maps a verified AI verdict to an Outcome."""
    kind = verdict.get("verdict")
    reason = verdict.get("reason", "")
    evidence = [f'{q.get("url", "")} — "{q.get("quote", "")}"' for q in verdict.get("quotes", [])][:3]
    if kind == "pass":
        return Outcome("pass", 1.0, reason, evidence, source="ai")
    if kind == "partial":
        factor = float(verdict.get("factor") or 0.5)
        return Outcome("partial", max(0.05, min(0.95, factor)), reason, evidence, source="ai")
    if kind == "fail":
        return Outcome("fail", 0.0, reason, evidence, source="ai")
    if kind == "not_found":
        return Outcome("fail", 0.0, f"No public evidence found. {reason}".strip(), evidence, source="ai")
    return not_evaluated(f"Unusable AI verdict: {kind!r}")


def evaluate(defn: CheckDef, ev: Evidence) -> Outcome:
    try:
        heuristic = defn.func(ev)
    except Exception as exc:  # a buggy check must not kill the run
        return Outcome("error", 0.0, f"check crashed: {type(exc).__name__}: {exc}")

    if heuristic is not None and heuristic.status == "na":
        return heuristic
    if defn.method == "auto":
        return heuristic if heuristic is not None else not_evaluated("no result")

    verdict = ev.ai_verdicts.get(defn.id) if ev.ai_enabled else None
    if verdict and verdict.get("verified", False):
        return verdict_outcome(verdict)

    if defn.method == "auto+ai" and heuristic is not None:
        heuristic.source = "heuristic"
        return heuristic
    if not ev.ai_enabled:
        return not_evaluated("Requires the AI layer (run without --deterministic).")
    if verdict and not verdict.get("verified", False):
        return not_evaluated(f"AI verdict rejected: {verdict.get('rejection', 'evidence could not be verified')}")
    return not_evaluated("AI judge returned no verdict for this check.")


def run_checks(ev: Evidence, pillars: list[str]) -> list[CheckResult]:
    results = []
    for defn in checks_for(pillars):
        outcome = evaluate(defn, ev)
        earned = round(defn.max_points * outcome.factor, 2) if outcome.status in ("pass", "partial", "fail") else 0.0
        results.append(
            CheckResult(
                id=defn.id,
                pillar=defn.pillar,
                title=defn.title,
                method=defn.method,
                max_points=defn.max_points,
                status=outcome.status,
                factor=round(outcome.factor, 3),
                earned=earned,
                detail=outcome.detail,
                evidence=outcome.evidence,
                source=outcome.source,
                critical=defn.critical,
            )
        )
    return results
