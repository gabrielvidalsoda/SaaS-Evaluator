"""Accessibility & multi-device collector — only runs with `--a11y`.

- axe-core (vendored, injected via `evaluate`, so page CSP doesn't block it) on
  the target page plus a few key pages;
- responsive layout: horizontal overflow at 375 / 768 / 1366 px (mobile contexts
  use touch + mobile UA flags so sites serve their real mobile layout);
- mobile tap-target sizes and small-font ratio at 375 px;
- keyboard focus visibility on the first tab stops (desktop).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from playwright.async_api import Page

from .browser import DESKTOP_VIEWPORT, NAV_TIMEOUT_MS, BrowserCollector, run_relative

_AXE_SOURCE = (Path(__file__).resolve().parent.parent / "vendor" / "axe.min.js").read_text(encoding="utf-8")

VIEWPORTS = {
    "mobile": ({"width": 375, "height": 812}, True),
    "tablet": ({"width": 768, "height": 1024}, True),
    "desktop": (DESKTOP_VIEWPORT, False),
}
TAB_STOPS = 12

_AXE_RUN_JS = """
async () => {
  const r = await axe.run(document, {
    runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa'] },
    resultTypes: ['violations'],
  });
  return r.violations.map(v => ({ id: v.id, impact: v.impact, help: v.help, nodes: v.nodes.length }));
}
"""

_OVERFLOW_JS = """
() => ({ scroll_width: document.documentElement.scrollWidth, viewport_width: window.innerWidth,
         overflow: document.documentElement.scrollWidth > window.innerWidth + 2 })
"""

_TAP_TARGETS_JS = """
() => {
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [onclick]';
  let total = 0, small = 0; const examples = [];
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (!r.width || !r.height || st.visibility === 'hidden' || st.display === 'none') continue;
    if (r.bottom < 0 || r.top > window.innerHeight * 3) continue;
    // Inline links inside running text are exempt (WCAG 2.5.8).
    if (el.tagName === 'A' && st.display === 'inline' && el.closest('p, li')) continue;
    total++;
    if (r.width < 24 || r.height < 24) {
      small++;
      if (examples.length < 5) examples.push(((el.innerText || el.getAttribute('aria-label') || el.tagName) + '').trim().slice(0, 40));
    }
  }
  let measured = 0, smallFont = 0;
  for (const e of [...document.querySelectorAll('p, li, span, a, td, label')].slice(0, 600)) {
    if (!(e.innerText || '').trim() || e.innerText.trim().length < 3) continue;
    const r = e.getBoundingClientRect(); if (!r.width) continue;
    measured++;
    if (parseFloat(getComputedStyle(e).fontSize) < 12) smallFont++;
  }
  return { total, small, examples, font_measured: measured, font_small: smallFont };
}
"""

_SETTLE_MS = 350  # lets CSS transitions on focus/blur finish before measuring

# Focused vs blurred computed styles of the active element: any visual change
# (outline, shadow, border, background, underline, color) = visible focus indicator.
# Split in steps (with a settle delay between them) so transitions don't hide the change.
_FOCUS_SNAPSHOT_JS = """
() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return null;
  el.setAttribute('data-saas-eval-focus', '1');
  const s = getComputedStyle(el);
  return { tag: el.tagName.toLowerCase(),
           text: ((el.innerText || el.getAttribute('aria-label') || '') + '').trim().slice(0, 40),
           outline: s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0,
           style: [s.outlineStyle, s.outlineWidth, s.outlineColor, s.boxShadow, s.borderColor,
                   s.backgroundColor, s.textDecorationLine, s.color].join('|') };
}
"""
_FOCUS_BLUR_JS = "() => { const el = document.activeElement; if (el) el.blur(); }"
_FOCUS_BLURRED_JS = """
() => {
  const el = document.querySelector('[data-saas-eval-focus]');
  if (!el) return null;
  const s = getComputedStyle(el);
  return [s.outlineStyle, s.outlineWidth, s.outlineColor, s.boxShadow, s.borderColor,
          s.backgroundColor, s.textDecorationLine, s.color].join('|');
}
"""
_FOCUS_RESTORE_JS = """
() => { const el = document.querySelector('[data-saas-eval-focus]');
        if (el) { el.removeAttribute('data-saas-eval-focus'); el.focus({ preventScroll: true }); } }
"""


async def _run_axe(page: Page) -> list[dict[str, Any]]:
    await page.evaluate(_AXE_SOURCE + "\n;true")
    return await page.evaluate(_AXE_RUN_JS)


async def _focus_check(page: Page) -> dict[str, Any]:
    await page.evaluate("() => { document.activeElement && document.activeElement.blur(); window.scrollTo(0, 0); }")
    stops: list[dict[str, Any]] = []
    for _ in range(TAB_STOPS):
        await page.keyboard.press("Tab")
        await page.wait_for_timeout(_SETTLE_MS)
        info = await page.evaluate(_FOCUS_SNAPSHOT_JS)
        if not info:
            continue
        await page.evaluate(_FOCUS_BLUR_JS)
        await page.wait_for_timeout(_SETTLE_MS)
        blurred = await page.evaluate(_FOCUS_BLURRED_JS)
        await page.evaluate(_FOCUS_RESTORE_JS)
        stops.append({"tag": info["tag"], "text": info["text"],
                      "visible": info["outline"] or (blurred is not None and blurred != info["style"])})
    return {"stops": len(stops), "visible": sum(1 for s in stops if s["visible"]),
            "missing": [s for s in stops if not s["visible"]][:5]}


async def collect_a11y(
    collector: BrowserCollector, target_url: str, extra_urls: list[str], screenshots_dir: Path
) -> dict[str, Any]:
    out: dict[str, Any] = {"axe": {}, "viewports": {}, "tap_targets": None, "focus": None, "errors": []}

    # axe-core on the target + key pages, reusing the main (desktop) page.
    for url in [target_url, *extra_urls]:
        try:
            await collector.page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            await collector.page.wait_for_timeout(1000)
            out["axe"][url] = await _run_axe(collector.page)
        except Exception as exc:
            out["errors"].append(f"axe {url}: {type(exc).__name__}: {str(exc).splitlines()[0][:150]}")

    # Keyboard focus on the target page (desktop).
    try:
        await collector.page.goto(target_url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        await collector.page.wait_for_timeout(800)
        out["focus"] = await _focus_check(collector.page)
    except Exception as exc:
        out["errors"].append(f"focus: {type(exc).__name__}: {str(exc).splitlines()[0][:150]}")

    # Responsive layout per viewport, each in its own context.
    for name, (viewport, mobile) in VIEWPORTS.items():
        ctx = await collector.new_context(viewport, mobile=mobile)
        try:
            page = await ctx.new_page()
            await page.goto(target_url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            await page.wait_for_timeout(1200)
            result = await page.evaluate(_OVERFLOW_JS)
            shot = screenshots_dir / f"viewport-{name}.jpg"
            await page.screenshot(path=str(shot), type="jpeg", quality=70)
            result["screenshot"] = run_relative(shot)
            if name == "mobile":
                out["tap_targets"] = await page.evaluate(_TAP_TARGETS_JS)
            out["viewports"][name] = result
        except Exception as exc:
            out["errors"].append(f"viewport {name}: {type(exc).__name__}: {str(exc).splitlines()[0][:150]}")
        finally:
            await ctx.close()
    return out
