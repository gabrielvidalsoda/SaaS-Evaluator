"""Scoring engine — turns check results into pillar scores and an overall grade.

- Check factor: pass=1, fail=0, partial/continuous in between. `na`,
  `not_evaluated` and `error` are excluded from the denominator.
- Pillar score    = 100 × Σ earned / Σ max_points(evaluated)
- Pillar coverage = Σ max(evaluated) / Σ max(applicable = everything except `na`)
- Overall         = Σ(weight × pillar score) / Σ weight, over active pillars with coverage > 0
                    (so leaving A11Y out re-normalizes the other weights automatically)
- Critical gate   : a failed critical check (SEC-01, no HTTPS) caps its pillar at 20
                    and the overall score at 50.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ..models import EVALUATED, PILLARS, CheckResult, Evaluation, PillarScore

PILLAR_CAP_ON_CRITICAL = 20.0
OVERALL_CAP_ON_CRITICAL = 50.0

GRADES = [(85, "A"), (70, "B"), (55, "C"), (40, "D"), (0, "F")]


class ProfileError(ValueError):
    pass


def load_profile(name_or_path: str, profiles_dir: Path) -> dict[str, Any]:
    path = Path(name_or_path)
    if not path.suffix:
        path = profiles_dir / f"{name_or_path}.yaml"
    if not path.exists():
        available = ", ".join(sorted(p.stem for p in profiles_dir.glob("*.yaml")))
        raise ProfileError(f"Profile '{name_or_path}' not found. Available: {available}.")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    weights = data.get("weights") or {}
    unknown = set(weights) - set(PILLARS)
    if unknown:
        raise ProfileError(f"Profile {path.name}: unknown pillar(s) {sorted(unknown)}.")
    if any(not isinstance(w, (int, float)) or w < 0 for w in weights.values()):
        raise ProfileError(f"Profile {path.name}: weights must be non-negative numbers.")
    data.setdefault("name", path.stem)
    return data


def grade_for(score: float | None) -> str:
    if score is None:
        return "N/A"
    return next(letter for threshold, letter in GRADES if score >= threshold)


def confidence_for(coverage: float) -> str:
    return "High" if coverage >= 0.8 else "Medium" if coverage >= 0.5 else "Low"


def score_pillar(code: str, weight: float, checks: list[CheckResult]) -> PillarScore:
    applicable = [c for c in checks if c.status != "na"]
    evaluated = [c for c in applicable if c.status in EVALUATED]
    max_applicable = sum(c.max_points for c in applicable)
    max_evaluated = sum(c.max_points for c in evaluated)
    earned = sum(c.earned for c in evaluated)
    score = round(100 * earned / max_evaluated, 1) if max_evaluated else None
    coverage = round(max_evaluated / max_applicable, 3) if max_applicable else 0.0

    capped = any(c.critical and c.status == "fail" for c in checks)
    if capped and score is not None:
        score = min(score, PILLAR_CAP_ON_CRITICAL)

    return PillarScore(
        code=code,
        name=PILLARS[code].name,
        weight=weight,
        score=score,
        coverage=coverage,
        confidence=confidence_for(coverage),
        earned=round(earned, 2),
        max_evaluated=max_evaluated,
        max_applicable=max_applicable,
        capped=capped,
        checks=checks,
    )


def score_evaluation(results: list[CheckResult], pillars: list[str], profile: dict[str, Any]) -> Evaluation:
    weights = profile.get("weights") or {}
    pillar_scores = []
    for code in pillars:
        checks = [r for r in results if r.pillar == code]
        pillar_scores.append(score_pillar(code, float(weights.get(code, 0)), checks))

    scored_pillars = [p for p in pillar_scores if p.score is not None and p.weight > 0]
    total_weight = sum(p.weight for p in scored_pillars)
    overall = round(sum(p.weight * p.score for p in scored_pillars) / total_weight, 1) if total_weight else None

    # Overall coverage: weight-averaged pillar coverage (a low-weight pillar with no
    # data matters less than an unevaluated security pillar).
    all_weight = sum(p.weight for p in pillar_scores) or 1.0
    coverage = round(sum(p.weight * p.coverage for p in pillar_scores) / all_weight, 3)

    dealbreakers = [f"{r.id} {r.title}: {r.detail}" for r in results if r.critical and r.status == "fail"]
    if dealbreakers and overall is not None:
        overall = min(overall, OVERALL_CAP_ON_CRITICAL)

    return Evaluation(
        overall=overall,
        grade=grade_for(overall),
        coverage=coverage,
        confidence=confidence_for(coverage),
        profile=profile.get("name", "custom"),
        pillars=pillar_scores,
        dealbreakers=dealbreakers,
    )
