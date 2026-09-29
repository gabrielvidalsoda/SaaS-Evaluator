"""A11Y — Accessibility & Multi-device. Opt-in pillar: only runs with --a11y."""

from __future__ import annotations

from ..models import Evidence, failed, na, not_evaluated, passed, scored
from .helpers import lerp
from .registry import check

_IMPACT_WEIGHT = {"critical": 10, "serious": 5, "moderate": 2, "minor": 1}


def _a11y(ev: Evidence) -> dict | None:
    return ev.a11y


@check("A11Y-01", "A11Y", "Automated WCAG 2.x checks (axe-core)", 40)
def axe(ev: Evidence):
    data = _a11y(ev)
    if not data or not data.get("axe"):
        return not_evaluated("axe-core did not run.")
    penalties, worst = [], []
    for url, violations in data["axe"].items():
        penalties.append(sum(_IMPACT_WEIGHT.get(v.get("impact") or "minor", 1) for v in violations))
        worst += [f"[{v.get('impact')}] {v['id']}: {v['help']} ({v['nodes']} nodes)" for v in violations
                  if v.get("impact") in ("critical", "serious")]
    avg = sum(penalties) / len(penalties)
    n = sum(len(v) for v in data["axe"].values())
    return scored(
        lerp(avg, 0, 50),
        f"{n} WCAG rule violations over {len(penalties)} pages (impact-weighted penalty {avg:.0f}/page; 50+ scores 0).",
        sorted(set(worst))[:6],
    )


@check("A11Y-02", "A11Y", "Responsive layout (no horizontal overflow)", 25)
def responsive(ev: Evidence):
    data = _a11y(ev)
    if not data or not data.get("viewports"):
        return not_evaluated("Viewport checks did not run.")
    target = ev.target
    parts, notes = [], []
    for name, vp in data["viewports"].items():
        ok = not vp.get("overflow")
        parts.append(1.0 if ok else 0.0)
        if not ok:
            notes.append(f"{name}: content {vp['scroll_width']}px wide in a {vp['viewport_width']}px viewport")
    parts.append(1.0 if target and target.has_viewport_meta else 0.0)
    if not (target and target.has_viewport_meta):
        notes.append("no `width=device-width` viewport meta")
    return scored(sum(parts) / len(parts), "; ".join(notes) or "No horizontal overflow at 375/768/1366 px; viewport meta set.",
                  [vp.get("screenshot", "") for vp in data["viewports"].values()])


@check("A11Y-03", "A11Y", "Mobile tap targets and font sizes", 15)
def tap_targets(ev: Evidence):
    data = _a11y(ev) or {}
    t = data.get("tap_targets")
    if not t or not t.get("total"):
        return not_evaluated("Mobile tap-target check did not run.")
    small_ratio = t["small"] / t["total"]
    font_ratio = (t["font_small"] / t["font_measured"]) if t.get("font_measured") else 0.0
    factor = 0.7 * lerp(small_ratio, 0.05, 0.30) + 0.3 * lerp(font_ratio, 0.05, 0.30)
    return scored(
        factor,
        f"{small_ratio:.0%} of {t['total']} tap targets under 24×24px; {font_ratio:.0%} of text under 12px (375px viewport).",
        t.get("examples", []),
    )


@check("A11Y-04", "A11Y", "Visible keyboard focus", 15)
def keyboard_focus(ev: Evidence):
    data = _a11y(ev) or {}
    f = data.get("focus")
    if not f:
        return not_evaluated("Keyboard focus check did not run.")
    if not f.get("stops"):
        return failed("Tab key did not move focus to any element.")
    ratio = f["visible"] / f["stops"]
    missing = [f"<{m['tag']}> {m['text']}" for m in f.get("missing", [])]
    return scored(lerp(ratio, 1.0, 0.5), f"{f['visible']}/{f['stops']} tab stops show a visible focus indicator.", missing)


@check("A11Y-05", "A11Y", "Native mobile apps available", 5)
def mobile_apps(ev: Evidence):
    stores = {
        "App Store": any("apps.apple.com" in l["href"] or "itunes.apple.com" in l["href"] for l in ev.all_links()),
        "Google Play": any("play.google.com" in l["href"] for l in ev.all_links()),
    }
    found = [k for k, v in stores.items() if v]
    if not ev.ok_pages():
        return not_evaluated("No page could be loaded.")
    return scored(len(found) / 2, f"Store links found: {', '.join(found) or 'none'}.")
