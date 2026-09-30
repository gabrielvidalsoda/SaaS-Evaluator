"""Browser collector (Playwright, async). Instead of acting on the page it
*measures* each visited page — Web Vitals, resources, caching, console errors, third-party
scripts, widgets, forms — and returns a `PageEvidence`.

Scripts are injected with `add_init_script`/`evaluate` (CDP), which the page's
Content-Security-Policy does not block."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Response, async_playwright

from .. import USER_AGENT_SUFFIX
from ..models import PageEvidence
from .urls import registered_domain

DESKTOP_VIEWPORT = {"width": 1366, "height": 900}
NAV_TIMEOUT_MS = 45000
MAX_TEXT_CHARS = 60000

# Observers registered before any page script runs; read back by _VITALS_COLLECT_JS.
_VITALS_INIT_JS = """
(() => {
  const s = window.__saasEval = { lcp: null, cls: 0, longTasks: [], fcp: null };
  const obs = (type, cb) => { try { new PerformanceObserver(l => l.getEntries().forEach(cb)).observe({ type, buffered: true }); } catch (e) {} };
  obs('largest-contentful-paint', e => { s.lcp = e.renderTime || e.loadTime || e.startTime; });
  obs('layout-shift', e => { if (!e.hadRecentInput) s.cls += e.value; });
  obs('longtask', e => { s.longTasks.push([e.startTime, e.duration]); });
  obs('paint', e => { if (e.name === 'first-contentful-paint') s.fcp = e.startTime; });
})();
"""

_VITALS_COLLECT_JS = """
() => {
  const s = window.__saasEval || {};
  const nav = performance.getEntriesByType('navigation')[0];
  const res = performance.getEntriesByType('resource');
  let bytes = nav ? (nav.transferSize || nav.encodedBodySize || 0) : 0;
  for (const r of res) bytes += (r.transferSize || r.encodedBodySize || 0);
  const fcp = s.fcp || 0;
  const tbt = (s.longTasks || []).filter(([st]) => st >= fcp)
    .reduce((acc, [, d]) => acc + Math.max(0, d - 50), 0);
  return {
    lcp_ms: s.lcp, cls: s.cls, tbt_ms: tbt, fcp_ms: s.fcp,
    ttfb_ms: nav ? nav.responseStart - nav.startTime : null,
    dom_content_loaded_ms: nav ? nav.domContentLoadedEventEnd - nav.startTime : null,
    load_ms: nav ? nav.loadEventEnd - nav.startTime : null,
    next_hop_protocol: nav ? nav.nextHopProtocol : '',
    request_count: res.length + 1,
    transfer_bytes: bytes,
  };
}
"""

_PAGE_INFO_JS = """
(maxText) => {
  const clean = (t) => (t || '').trim().replace(/\\s+/g, ' ');
  const abs = (h) => { try { return new URL(h, location.href).href; } catch (e) { return null; } };
  const visible = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const links = [...document.querySelectorAll('a[href]')].map(a => ({
    href: abs(a.getAttribute('href')),
    text: clean(a.innerText || a.getAttribute('aria-label') || a.title).slice(0, 80),
  })).filter(l => l.href);
  const fields = [...document.querySelectorAll('input, select, textarea')]
    .filter(el => !['hidden', 'submit', 'button', 'image', 'reset'].includes((el.type || '').toLowerCase()))
    .filter(visible)
    .map(el => {
      let label = false;
      if (el.id) { try { label = !!document.querySelector(`label[for="${CSS.escape(el.id)}"]`); } catch (e) {} }
      if (!label) label = !!el.closest('label');
      const aria = !!(el.getAttribute('aria-label') || el.getAttribute('aria-labelledby'));
      return { type: (el.type || el.tagName).toLowerCase(), name: el.name || el.id || '',
               label, aria, placeholder: !!el.placeholder, autocomplete: el.getAttribute('autocomplete') || '' };
    });
  const pwToggle = !!document.querySelector(
    'button[aria-label*="password" i], button[aria-label*="show" i], [role=button][aria-label*="password" i], '
    + 'button[title*="password" i], button[title*="show" i], [data-testid*="password" i][role=button], '
    + '[class*="password-toggle" i], [class*="toggle-password" i], [class*="show-password" i]');
  const buttons = [...document.querySelectorAll('button, [role=button], input[type=submit], a[class*="btn" i], a[class*="button" i]')]
    .filter(visible).map(b => clean(b.innerText || b.value || b.getAttribute('aria-label'))).filter(Boolean).slice(0, 80);
  const vp = document.querySelector('meta[name="viewport"]');
  return {
    title: document.title, lang: document.documentElement.lang || '',
    has_viewport_meta: !!(vp && /width\\s*=\\s*device-width/i.test(vp.content || '')),
    text: (document.body ? document.body.innerText : '').slice(0, maxText),
    links, forms: { fields, password_toggle: pwToggle }, buttons,
  };
}
"""

# Visible fixed/sticky/dialog element mentioning cookies/consent with an accept/reject button.
_CONSENT_JS = """
() => {
  const textRx = /cookie|consent|gdpr|lgpd/i;
  const btnRx = /accept|agree|allow|got it|\\bok\\b|reject|decline|manage|preferences|aceitar|concordo|entendi|rejeitar/i;
  const roots = [document];
  document.querySelectorAll('*').forEach(el => { if (el.shadowRoot) roots.push(el.shadowRoot); });
  for (const root of roots) {
    for (const el of root.querySelectorAll('div, section, aside, dialog, form, [role=dialog], [role=region]')) {
      const r = el.getBoundingClientRect();
      if (r.width < 200 || r.height < 30) continue;
      const st = getComputedStyle(el);
      if (st.display === 'none' || st.visibility === 'hidden') continue;
      const marker = String(el.id) + ' ' + String(el.className);
      if (!(st.position === 'fixed' || st.position === 'sticky' || el.getAttribute('role') === 'dialog'
            || /cookie|consent/i.test(marker))) continue;
      const t = (el.innerText || '').slice(0, 3000);
      if (!textRx.test(t) || t.length > 3000) continue;
      if ([...el.querySelectorAll('button, a, [role=button]')].some(b => btnRx.test(b.innerText || ''))) return true;
    }
  }
  return false;
}
"""

_WIDGET_GLOBALS_JS = """
() => {
  const g = { Intercom: 'chat:Intercom', zE: 'chat:Zendesk', zEmbed: 'chat:Zendesk', drift: 'chat:Drift',
    HubSpotConversations: 'chat:HubSpot', $crisp: 'chat:Crisp', Tawk_API: 'chat:Tawk.to', fcWidget: 'chat:Freshchat',
    LiveChatWidget: 'chat:LiveChat', olark: 'chat:Olark', Beacon: 'chat:Help Scout', FrontChat: 'chat:Front',
    OneTrust: 'cmp:OneTrust', Cookiebot: 'cmp:Cookiebot', Didomi: 'cmp:Didomi', UC_UI: 'cmp:Usercentrics',
    __tcfapi: 'cmp:IAB TCF' };
  return Object.keys(g).filter(k => typeof window[k] !== 'undefined').map(k => g[k]);
}
"""

# Script URL patterns → widget label. "chat:" = support chat, "cmp:" = cookie consent manager.
WIDGET_URL_PATTERNS = {
    r"intercom(cdn)?\.(io|com)|widget\.intercom": "chat:Intercom",
    r"zdassets\.com|zendesk\.com/embeddable|zopim": "chat:Zendesk",
    r"drift\.com|driftt\.com": "chat:Drift",
    r"usemessages\.com|hs-scripts\.com|js\.hs-analytics": "chat:HubSpot",
    r"client\.crisp\.chat": "chat:Crisp",
    r"embed\.tawk\.to": "chat:Tawk.to",
    r"wchat\.freshchat|freshchat\.com|freshworks\.com/.*widget": "chat:Freshchat",
    r"livechatinc\.com": "chat:LiveChat",
    r"olark\.com": "chat:Olark",
    r"beacon-v2\.helpscout|helpscout\.net": "chat:Help Scout",
    r"chat\.frontapp|front\.com/chat": "chat:Front",
    r"gorgias": "chat:Gorgias",
    r"embeddedservice|salesforceliveagent": "chat:Salesforce",
    r"chatwoot": "chat:Chatwoot",
    r"qualified\.com": "chat:Qualified",
    r"usepylon\.com": "chat:Pylon",
    r"onetrust|cookielaw\.org": "cmp:OneTrust",
    r"cookiebot": "cmp:Cookiebot",
    r"didomi": "cmp:Didomi",
    r"osano\.com": "cmp:Osano",
    r"termly\.io": "cmp:Termly",
    r"usercentrics": "cmp:Usercentrics",
    r"cookieyes": "cmp:CookieYes",
    r"iubenda": "cmp:iubenda",
    r"klaro": "cmp:Klaro",
    r"consentmanager\.net": "cmp:consentmanager",
    r"trustarc|truste\.com": "cmp:TrustArc",
    r"quantcast\.mgr|choice\.quantcast": "cmp:Quantcast",
    r"cookie-script\.com": "cmp:CookieScript",
    r"ketchcdn|ketch\.com": "cmp:Ketch",
    r"transcend-cdn|transcend\.io": "cmp:Transcend",
}

_STATIC_TYPES = {"script", "stylesheet", "font", "image"}
_MAX_AGE_RX = re.compile(r"max-age=(\d+)")
LONG_CACHE_SECONDS = 7 * 24 * 3600


def run_relative(path: Path) -> str:
    """'screenshots/x.jpg' — stored relative to the run folder (portable across machines/OSes)."""
    return f"{path.parent.name}/{path.name}"


def _is_long_cache(headers: dict[str, str]) -> bool:
    cc = headers.get("cache-control", "").lower()
    if "no-store" in cc or "no-cache" in cc:
        return False
    if "immutable" in cc:
        return True
    m = _MAX_AGE_RX.search(cc)
    return bool(m and int(m.group(1)) >= LONG_CACHE_SECONDS)


class BrowserCollector:
    def __init__(self, headless: bool, org_domain: str) -> None:
        self.headless = headless
        self.org_domain = org_domain
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.user_agent = ""
        self._responses: list[tuple[str, int, str, dict[str, str]]] = []
        self._console_errors: list[str] = []
        self._page_errors: list[str] = []
        self._visits = 0
        # Authenticated session (cookies + localStorage), in memory only — once set,
        # every page and context opened by this collector is logged in.
        self.storage_state: dict[str, Any] | None = None

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self.headless)
        # Headless Chromium advertises "HeadlessChrome", which some sites block —
        # present the regular UA plus an honest tool suffix.
        probe = await self._browser.new_page()
        ua = await probe.evaluate("navigator.userAgent")
        await probe.close()
        self.user_agent = ua.replace("HeadlessChrome", "Chrome") + f" {USER_AGENT_SUFFIX}"
        self.context = await self.new_context(DESKTOP_VIEWPORT)
        self.page = await self.context.new_page()
        self._attach(self.page)

    async def new_context(self, viewport: dict[str, int], mobile: bool = False) -> BrowserContext:
        ctx = await self._browser.new_context(
            viewport=viewport,
            user_agent=self.user_agent,
            is_mobile=mobile,
            has_touch=mobile,
            device_scale_factor=2 if mobile else 1,
            ignore_https_errors=True,
            locale="en-US",
            storage_state=self.storage_state,
        )
        await ctx.add_init_script(_VITALS_INIT_JS)
        return ctx

    async def use_session(self, storage_state: dict[str, Any]) -> None:
        """Switches the main page to a fresh context carrying the logged-in session."""
        self.storage_state = storage_state
        old = self.context
        self.context = await self.new_context(DESKTOP_VIEWPORT)
        self.page = await self.context.new_page()
        self._attach(self.page)
        if old is not None:
            await old.close()

    @property
    def browser(self) -> Browser:
        return self._browser

    @property
    def playwright(self) -> Playwright:
        return self._playwright

    def _attach(self, page: Page) -> None:
        page.on("console", lambda msg: self._console_errors.append(msg.text[:300]) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: self._page_errors.append(str(exc)[:300]))
        page.on("response", self._on_response)

    def _on_response(self, resp: Response) -> None:
        try:
            self._responses.append((resp.url, resp.status, resp.request.resource_type, resp.headers))
        except Exception:
            pass

    async def close(self) -> None:
        try:
            if self._browser is not None:
                await self._browser.close()
        finally:
            if self._playwright is not None:
                await self._playwright.stop()

    async def cookies(self) -> list[dict[str, Any]]:
        """First- and third-party cookies set during the crawl — values dropped."""
        return [
            {
                "name": c["name"],
                "domain": c["domain"],
                "secure": c.get("secure", False),
                "http_only": c.get("httpOnly", False),
                "same_site": c.get("sameSite", ""),
                "first_party": registered_domain(c["domain"].lstrip(".")) == self.org_domain,
            }
            for c in await self.context.cookies()
        ]

    async def visit(self, url: str, kind: str, screenshot_path: Path | None) -> PageEvidence:
        ev = PageEvidence(url=url, kind=kind)
        self._responses, self._console_errors, self._page_errors = [], [], []
        first_visit = self._visits == 0
        self._visits += 1
        page = self.page
        try:
            resp = await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
            ev.status = resp.status if resp else None
        except Exception as exc:  # timeout, DNS, TLS…
            ev.error = f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
            return ev
        try:
            await page.wait_for_load_state("networkidle", timeout=6000)
        except Exception:
            pass
        await page.wait_for_timeout(1500)  # let LCP / late layout shifts settle

        ev.final_url = page.url
        try:
            info = await page.evaluate(_PAGE_INFO_JS, MAX_TEXT_CHARS)
            vitals = await page.evaluate(_VITALS_COLLECT_JS)
            globals_found = await page.evaluate(_WIDGET_GLOBALS_JS)
            if first_visit:
                ev.consent_banner = bool(await page.evaluate(_CONSENT_JS))
        except Exception as exc:
            ev.error = f"page evaluation failed: {exc}"
            return ev

        ev.title = info["title"]
        ev.lang = info["lang"]
        ev.has_viewport_meta = info["has_viewport_meta"]
        ev.text = info["text"]
        ev.links = info["links"]
        ev.forms = info["forms"]
        ev.buttons = info["buttons"]
        ev.next_hop_protocol = vitals.pop("next_hop_protocol", "") or ""
        ev.request_count = int(vitals.pop("request_count", 0))
        ev.transfer_bytes = int(vitals.pop("transfer_bytes", 0))
        ev.vitals = vitals
        ev.console_errors = list(self._console_errors)
        ev.page_errors = list(self._page_errors)
        self._analyze_responses(ev, set(globals_found))

        if screenshot_path is not None:
            try:
                await page.screenshot(path=str(screenshot_path), type="jpeg", quality=70)
                ev.screenshot = run_relative(screenshot_path)
            except Exception:
                pass
        return ev

    def _analyze_responses(self, ev: PageEvidence, widgets: set[str]) -> None:
        page_is_https = ev.final_url.startswith("https://")
        script_origins: set[str] = set()
        static_total = static_long = 0
        header_bytes = 0
        for url, status, rtype, headers in self._responses:
            try:
                header_bytes += int(headers.get("content-length", 0))
            except ValueError:
                pass
            if status >= 400:
                ev.failed_requests.append(f"{status} {url[:200]}")
            if page_is_https and url.startswith("http://"):
                ev.mixed_content.append(url[:200])
            if rtype == "script":
                domain = registered_domain(url)
                if domain and domain != self.org_domain:
                    script_origins.add(domain)
                for pattern, label in WIDGET_URL_PATTERNS.items():
                    if re.search(pattern, url, re.I):
                        widgets.add(label)
            if rtype in _STATIC_TYPES and 200 <= status < 300:
                static_total += 1
                static_long += _is_long_cache(headers)
        # Resource Timing reports 0 bytes for cross-origin resources without
        # Timing-Allow-Origin; content-length covers those (compressed size).
        ev.transfer_bytes = max(ev.transfer_bytes, header_bytes)
        ev.script_origins = sorted(script_origins)
        ev.static_assets = {"total": static_total, "long_cache": static_long}
        ev.widgets = sorted(widgets)
