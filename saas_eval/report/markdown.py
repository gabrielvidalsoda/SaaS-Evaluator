"""Renders report.md from the scores.json document (so `saas-eval report` can
re-render without re-collecting or re-querying the AI)."""

from __future__ import annotations

from typing import Any

ICONS = {"pass": "✅", "partial": "🟡", "fail": "❌", "na": "➖", "not_evaluated": "⏸️", "error": "⚠️"}
STATUS_LABEL = {
    "pass": "pass", "partial": "partial", "fail": "fail",
    "na": "n/a", "not_evaluated": "not evaluated", "error": "error",
}


def _cell(text: str, limit: int = 220) -> str:
    text = (text or "").replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt_score(score: float | None) -> str:
    return "—" if score is None else f"{score:.1f}"


def top_issues(evaluation: dict[str, Any], limit: int = 8) -> list[dict[str, Any]]:
    """Failed/partial checks ranked by points lost × the pillar's weight share."""
    pillars = evaluation["pillars"]
    total_w = sum(p["weight"] for p in pillars if p["score"] is not None) or 1.0
    issues = []
    for p in pillars:
        if p["score"] is None:
            continue
        pillar_max = p["max_evaluated"] or 1.0
        for c in p["checks"]:
            if c["status"] in ("fail", "partial"):
                lost = c["max_points"] - c["earned"]
                impact = (lost / pillar_max) * (p["weight"] / total_w) * 100  # overall points lost
                issues.append({**c, "pillar_name": p["name"], "impact": round(impact, 2)})
    return sorted(issues, key=lambda i: -i["impact"])[:limit]


def render_markdown(doc: dict[str, Any]) -> str:
    run, ev = doc["run"], doc["evaluation"]
    pillars = ev["pillars"]
    total_w = sum(p["weight"] for p in pillars if p["score"] is not None) or 1.0
    a11y_note = "A11Y active" if "A11Y" in run["pillars"] else "A11Y not active — add `--a11y` to include it"
    lines = [
        f"# SaaS Evaluation — {run['host']}",
        "",
        "| | |",
        "|---|---|",
        f"| **Target** | {run['target_url']} |",
        f"| **Date** | {run['started_at'][:19].replace('T', ' ')} UTC |",
        f"| **Mode** | {run['mode_label']} |",
        f"| **Pillars** | {len(run['pillars'])} ({a11y_note}) |",
        f"| **Profile** | {ev['profile']} |",
        f"| **Run id** | `{run['run_id']}` |",
    ]
    if run.get("ai"):
        lines.append(f"| **AI** | model `{run['ai'].get('model')}`, prompts `{run['ai'].get('prompt_hash')}` |")
    lines += [
        "",
        f"## Overall: **{ev['grade']} ({_fmt_score(ev['overall'])}/100)** · coverage {ev['coverage']:.0%} · {ev['confidence']} confidence",
        "",
    ]
    if ev["dealbreakers"]:
        lines += ["> ⛔ **Dealbreakers** (overall score capped at 50):"]
        lines += [f"> - {_cell(d, 300)}" for d in ev["dealbreakers"]]
        lines.append("")
    if doc.get("summary"):
        lines += ["## Executive summary", "", doc["summary"].strip(), ""]

    lines += [
        "## Pillar scores",
        "",
        "| Pillar | Weight | Score | Grade | Coverage | Confidence |",
        "|---|---:|---:|:---:|---:|---|",
    ]
    for p in pillars:
        share = f"{p['weight'] / total_w:.0%}" if p["score"] is not None else "—"
        cap = " ⛔" if p["capped"] else ""
        lines.append(
            f"| {p['name']} ({p['code']}) | {share} | {_fmt_score(p['score'])}{cap} | {p.get('grade', '')} | "
            f"{p['coverage']:.0%} | {p['confidence']} |"
        )
    lines.append("")

    issues = top_issues(ev)
    if issues:
        lines += ["## Top issues", "", "Ranked by overall points lost.", ""]
        for i in issues:
            lines.append(
                f"1. **{i['id']} {i['title']}** ({i['pillar_name']}, −{i['impact']:.1f} pts) — {_cell(i['detail'], 300)}"
            )
        lines.append("")

    lines += ["## Details by pillar", ""]
    for p in pillars:
        lines += [
            f"### {p['name']} ({p['code']}) — {_fmt_score(p['score'])} · coverage {p['coverage']:.0%}",
            "",
            "| Check | Method | Result | Points | Detail |",
            "|---|---|---|---:|---|",
        ]
        for c in p["checks"]:
            source = f" ({c['source']})" if c["method"] == "auto+ai" and c["source"] in ("ai", "heuristic") else ""
            points = f"{c['earned']:g}/{c['max_points']:g}" if c["status"] in ("pass", "partial", "fail") else f"–/{c['max_points']:g}"
            lines.append(
                f"| **{c['id']}** {_cell(c['title'], 80)} | {c['method']}{source} | "
                f"{ICONS.get(c['status'], '')} {STATUS_LABEL.get(c['status'], c['status'])} | {points} | {_cell(c['detail'])} |"
            )
        evidence_rows = [(c["id"], e) for c in p["checks"] for e in c.get("evidence", []) if e]
        if evidence_rows:
            lines += ["", "<details><summary>Evidence</summary>", ""]
            lines += [f"- **{cid}**: {_cell(e, 300)}" for cid, e in evidence_rows]
            lines += ["", "</details>"]
        lines.append("")

    login = doc.get("login")
    if login:
        ok = "✅ logged in" if login.get("success") else "❌ login failed"
        lines += ["## Authenticated session", "",
                  f"- **Login**: {ok} — {login.get('method', '?')}. {_cell(login.get('note', ''), 200)}"]
        if login.get("session_lost"):
            lines.append("- ⚠️ The session was lost during the in-app crawl (redirected to login).")
        if doc.get("app_pages"):
            lines += ["", "| In-app page | Status | Load |", "|---|---:|---:|"]
            for p in doc["app_pages"]:
                load = f"{p['load_ms'] / 1000:.1f}s" if p.get("load_ms") else "—"
                lines.append(f"| {_cell(p.get('title') or '', 60)} — {p['url']} | {p.get('error') or p.get('status')} | {load} |")
        lines.append("")

    discovered = doc.get("discovered") or {}
    if discovered:
        lines += ["## Pages discovered", ""]
        lines += [f"- **{kind}**: {url}" for kind, url in sorted(discovered.items())]
        lines.append("")

    lines += [
        "## Methodology & limitations",
        "",
        "- **Passive only**: GET requests, public pages, a small fixed list of well-known paths. No attacks, fuzzing or load testing.",
        "- **Logged-in evaluation** is read-only: only same-host navigation links are followed, never links that could log out, "
        "delete, pay, export or change settings; the session is kept in memory and never written to disk.",
        "- **Scores**: each check earns `max_points × factor` (pass = 1, fail = 0, partial / continuous metrics in between). "
        "Pillar score = earned / max of *evaluated* checks. Checks marked *n/a* or *not evaluated* are excluded, which is what **coverage** measures.",
        "- **Overall** = weighted average of pillar scores (profile weights, re-normalized over the pillars that ran). "
        "A failed critical check (no HTTPS) caps the overall score at 50.",
        "- **Performance** figures are *external signals* measured from the evaluator's machine and network (lab data, not real-user data).",
        "- **Public evidence**: claims (SOC 2, SLA, DPA…) are credited only when stated on pages reachable by an anonymous visitor.",
    ]
    if run.get("ai"):
        lines.append(
            "- **AI judgements** return a verdict per rubric item with verbatim quotes; a verdict whose quote is not found "
            "in the collected page text is rejected (the check becomes *not evaluated*). The AI never sets scores directly."
        )
    else:
        lines.append("- **Deterministic mode**: AI-judged checks were skipped (*not evaluated*); coverage is lower accordingly.")
    lines += ["", f"_Evidence, screenshots and the event log are in `data/runs/{run['run_id']}/`._", ""]
    return "\n".join(lines)
