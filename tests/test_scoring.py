"""Scoring engine: denominators, coverage, critical gate, grades and the
pillar/mode matrix (A11Y opt-in, deterministic vs AI)."""

from __future__ import annotations

import pytest

from saas_eval.config import settings
from saas_eval.models import CheckResult, active_pillars
from saas_eval.scoring.engine import ProfileError, grade_for, load_profile, score_evaluation, score_pillar


def result(cid: str, pillar: str, status: str, max_points: float, factor: float = 0.0, critical: bool = False) -> CheckResult:
    earned = max_points * factor if status in ("pass", "partial", "fail") else 0.0
    return CheckResult(cid, pillar, cid, "auto", max_points, status, factor, earned, "", [], "auto", critical)


def test_na_and_not_evaluated_are_excluded_from_score_but_not_evaluated_lowers_coverage():
    checks = [
        result("A", "SEC", "pass", 10, 1.0),
        result("B", "SEC", "fail", 10, 0.0),
        result("C", "SEC", "na", 30),
        result("D", "SEC", "not_evaluated", 20),
    ]
    p = score_pillar("SEC", 25, checks)
    assert p.score == 50.0  # 10 of 20 evaluated points
    assert p.max_applicable == 40  # na excluded from applicable
    assert p.coverage == 0.5  # 20 evaluated of 40 applicable
    assert p.confidence == "Medium"


def test_partial_factor():
    p = score_pillar("PERF", 15, [result("A", "PERF", "partial", 20, 0.25), result("B", "PERF", "pass", 20, 1.0)])
    assert p.score == 62.5


def test_critical_failure_caps_pillar_and_overall():
    profile = {"name": "t", "weights": {"SEC": 50, "UX": 50}}
    results = [
        result("SEC-01", "SEC", "fail", 10, critical=True),
        result("SEC-X", "SEC", "pass", 90, 1.0),
        result("UX-X", "UX", "pass", 10, 1.0),
    ]
    ev = score_evaluation(results, ["SEC", "UX"], profile)
    sec = next(p for p in ev.pillars if p.code == "SEC")
    assert sec.score == 20.0 and sec.capped
    assert ev.overall == 50.0  # (20 + 100) / 2 = 60 → capped at 50
    assert ev.dealbreakers


def test_overall_is_weighted_and_skips_pillars_without_evaluated_checks():
    profile = {"name": "t", "weights": {"SEC": 30, "UX": 10, "REL": 60}}
    results = [
        result("S", "SEC", "pass", 10, 1.0),  # 100
        result("U", "UX", "fail", 10, 0.0),  # 0
        result("R", "REL", "not_evaluated", 10),  # no score → excluded from overall
    ]
    ev = score_evaluation(results, ["SEC", "UX", "REL"], profile)
    assert ev.overall == 75.0  # (30*100 + 10*0) / 40


@pytest.mark.parametrize("score,grade", [(100, "A"), (85, "A"), (84.9, "B"), (70, "B"), (55, "C"), (40, "D"), (39.9, "F"), (None, "N/A")])
def test_grades(score, grade):
    assert grade_for(score) == grade


# ── pillar / mode matrix ─────────────────────────────────────────────────────


def test_a11y_is_opt_in():
    default = active_pillars(a11y=False)
    assert "A11Y" not in default and len(default) == 7
    with_a11y = active_pillars(a11y=True)
    assert "A11Y" in with_a11y and len(with_a11y) == 8


def test_default_profile_normalizes_over_90_without_a11y():
    profile = load_profile("default", settings.profiles_dir)
    pillars = active_pillars(a11y=False)
    # Every pillar scores 100 except SEC = 0: overall = 100 × (90 − 25) / 90.
    results = [result(f"{c}-1", c, "pass" if c != "SEC" else "fail", 10, 1.0 if c != "SEC" else 0.0) for c in pillars]
    ev = score_evaluation(results, pillars, profile)
    assert [p.code for p in ev.pillars] == pillars
    assert ev.overall == pytest.approx(100 * 65 / 90, abs=0.1)


def test_default_profile_includes_a11y_weight_10_when_active():
    profile = load_profile("default", settings.profiles_dir)
    pillars = active_pillars(a11y=True)
    results = [result(f"{c}-1", c, "pass" if c != "A11Y" else "fail", 10, 1.0 if c != "A11Y" else 0.0) for c in pillars]
    ev = score_evaluation(results, pillars, profile)
    a11y = next(p for p in ev.pillars if p.code == "A11Y")
    assert a11y.weight == 10
    assert ev.overall == pytest.approx(100 * 90 / 100, abs=0.1)


@pytest.mark.parametrize("name", ["default", "enterprise", "smb"])
def test_shipped_profiles_are_valid(name):
    profile = load_profile(name, settings.profiles_dir)
    assert set(profile["weights"]) == {"SEC", "UX", "PERF", "REL", "INT", "DATA", "TRN", "A11Y"}


def test_unknown_profile_errors():
    with pytest.raises(ProfileError):
        load_profile("does-not-exist", settings.profiles_dir)
