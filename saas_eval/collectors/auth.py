"""Authentication and the authenticated (in-app) crawl.

Login methods, all producing a Playwright `storage_state` (cookies + localStorage)
that stays in memory — it is never written to disk:

- manual      : a visible browser window opens on the target; the user logs in by
                hand (SSO, MFA, CAPTCHA all fine) and presses Enter in the terminal.
- credentials : scripted form fill (username → optional "next" → password → submit),
                covering the usual one- and two-step forms. The pipeline falls back to
                an AI login agent when this fails and AI is enabled.

The in-app crawl then follows the app's own navigation links (same host) —
read-only: links that could log out, delete, pay, export or change settings are
never followed."""

from __future__ import annotations

import re
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse, urlunparse

from playwright.async_api import BrowserContext, Page

from ..credentials import Credentials
from .browser import DESKTOP_VIEWPORT, NAV_TIMEOUT_MS

USERNAME_SELECTORS = (
    "input[type=email]",
    "input[autocomplete=username]",
    "input[autocomplete=email]",
    "input[name*=email i]",
    "input[id*=email i]",
    "input[name*=user i]",
    "input[id*=user i]",
    "input[name*=login i]",
    "input[id*=login i]",
    "input[type=text]",
)
PASSWORD_SELECTOR = "input[type=password]"
NEXT_BUTTON_RX = re.compile(r"^(next|continue|continuar|próximo|proximo|avançar|avancar|seguinte)\b", re.I)
SUBMIT_BUTTON_RX = re.compile(r"(log ?in|sign ?in|entrar|acessar|login|submit|continue|continuar)", re.I)
# Social/SSO buttons are never clicked by the scripted login.
SOCIAL_BUTTON_RX = re.compile(r"google|microsoft|apple|github|facebook|linkedin|sso|saml|okta|azure|passkey", re.I)
LOGIN_URL_RX = re.compile(r"/(login|signin|sign-in|log-in|auth|entrar|session)(/|$|\?|#)", re.I)

# Never followed during the in-app crawl (could end the session or change data).
UNSAFE_LINK_RX = re.compile(
    r"log[- _]?out|sign[- _]?out|\bsair\b|log[- _]?off|delete|remove|excluir|apagar|destroy|cancel|unsubscribe|billing|payment|"
    r"checkout|upgrade|pagamento|assinatura|/api/|download|export|invite|convidar|reset|revoke|deactivate",
    re.I,
)
_FILE_RX = re.compile(r"\.(pdf|csv|xlsx?|zip|docx?|png|jpe?g|gif|svg|mp4|json)(\?|$)", re.I)
_ID_SEGMENT_RX = re.compile(r"^(\d+|[0-9a-f]{8,}|[0-9a-f-]{36}|[A-Za-z0-9_-]{20,})$")

Prompt = Callable[[str], Awaitable[None]]
LOGIN_WAIT_MS = 15000


async def _first_visible(page: Page, selectors: tuple[str, ...] | list[str]):
    for sel in selectors:
        loc = page.locator(sel)
        for i in range(min(await loc.count(), 5)):
            el = loc.nth(i)
            try:
                if await el.is_visible():
                    return el
            except Exception:
                continue
    return None


async def _settle(page: Page, ms: int = 1500) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:
        pass
    await page.wait_for_timeout(ms)


async def looks_logged_out(page: Page) -> bool:
    """A visible password field or a login-looking URL means we're not in the app."""
    if LOGIN_URL_RX.search(urlparse(page.url).path + "/"):
        return True
    return await _first_visible(page, (PASSWORD_SELECTOR,)) is not None


async def _wait_logged_in(page: Page, timeout_ms: int) -> bool:
    """Polls until the app is shown: many SPAs take several seconds to redirect after submit."""
    waited = 0
    while waited < timeout_ms:
        await page.wait_for_timeout(500)
        waited += 500
        try:
            if not await looks_logged_out(page):
                return True
        except Exception:  # page mid-navigation
            continue
    return False


async def _click_button(page: Page, rx: re.Pattern) -> bool:
    buttons = page.locator("button, input[type=submit], [role=button]")
    for i in range(min(await buttons.count(), 40)):
        b = buttons.nth(i)
        try:
            if not await b.is_visible():
                continue
            label = (await b.inner_text() or await b.get_attribute("value") or "").strip()
            if rx.search(label) and not SOCIAL_BUTTON_RX.search(label):
                await b.click(timeout=5000)
                return True
        except Exception:
            continue
    return False


async def scripted_login(context: BrowserContext, url: str, creds: Credentials) -> dict[str, Any]:
    """Fills the login form. Returns {"success", "note", "final_url"}."""
    page = await context.new_page()
    try:
        await page.goto(creds.login_url or url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        await _settle(page)
        if not await looks_logged_out(page):
            # Maybe the landing page links to the login page ("Log in" / "Entrar").
            if await _click_button(page, re.compile(r"^(log ?in|sign ?in|entrar|acessar)$", re.I)):
                await _settle(page)

        user_field = await _first_visible(page, USERNAME_SELECTORS)
        if user_field is None:
            return {"success": False, "note": "No username/e-mail field found on the login page.", "final_url": page.url}
        await user_field.fill(creds.username)

        pw_field = await _first_visible(page, (PASSWORD_SELECTOR,))
        if pw_field is None:  # two-step login: submit the username first
            if not await _click_button(page, NEXT_BUTTON_RX):
                await user_field.press("Enter")
            try:
                await page.locator(PASSWORD_SELECTOR).first.wait_for(state="visible", timeout=10000)
            except Exception:
                return {"success": False, "note": "Password field never appeared (SSO-only or unusual flow).", "final_url": page.url}
            pw_field = await _first_visible(page, (PASSWORD_SELECTOR,))
        await pw_field.fill(creds.password)
        await pw_field.press("Enter")

        if not await _wait_logged_in(page, LOGIN_WAIT_MS):
            # Some forms ignore Enter — try the submit button once (only if the form is still there).
            if await _first_visible(page, (PASSWORD_SELECTOR,)) is not None and await _click_button(page, SUBMIT_BUTTON_RX):
                await _wait_logged_in(page, LOGIN_WAIT_MS)
        await _settle(page, 1000)
        if await looks_logged_out(page):
            error = await page.evaluate(
                "() => { const e = document.querySelector('[role=alert], .error, .alert, [class*=error i]');"
                " return e ? e.innerText.trim().slice(0, 160) : ''; }"
            )
            return {"success": False, "note": f"Still on the login page after submitting. {error}".strip(), "final_url": page.url}
        return {"success": True, "note": "Logged in by filling the login form.", "final_url": page.url}
    finally:
        await page.close()


async def manual_login(collector, url: str, prompt: Prompt) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Opens a visible browser window; the user logs in and presses Enter.
    Returns (storage_state or None, info)."""
    browser = await collector.playwright.chromium.launch(headless=False)
    try:
        context = await browser.new_context(viewport=DESKTOP_VIEWPORT, user_agent=collector.user_agent)
        page = await context.new_page()
        await page.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        await prompt(
            "A browser window is open. Log in there (SSO/MFA are fine), wait until the app is loaded, "
            "then press [bold]Enter[/bold] here to continue…"
        )
        await _settle(page, 500)
        if await looks_logged_out(page):
            return None, {"success": False, "note": "The browser was still on a login page when Enter was pressed.", "final_url": page.url}
        state = await context.storage_state()
        return state, {"success": True, "note": "Logged in manually by the user.", "final_url": page.url}
    finally:
        await browser.close()


def token_keys_in_local_storage(state: dict[str, Any], org_domain: str) -> list[str]:
    """Names (never values) of localStorage entries that look like auth tokens."""
    keys = []
    for origin in state.get("origins", []):
        if org_domain not in origin.get("origin", ""):
            continue
        for item in origin.get("localStorage", []):
            name, value = item.get("name", ""), item.get("value", "")
            if re.search(r"token|jwt|auth|session|access|refresh|bearer", name, re.I) or re.match(r"^eyJ[\w-]+\.eyJ", value):
                keys.append(name)
    return sorted(set(keys))


def _path_pattern(url: str) -> str:
    """/projects/123/tasks → /projects/:id/tasks (visit one record per pattern)."""
    parsed = urlparse(url)
    segs = [":id" if _ID_SEGMENT_RX.match(s) else s for s in parsed.path.split("/")]
    return "/".join(segs) or "/"


def app_links(links: list[dict[str, str]], app_host: str, seen_patterns: set[str]) -> list[str]:
    """Safe, same-host, de-duplicated (by path pattern) in-app navigation links."""
    out = []
    for link in links:
        href = link.get("href") or ""
        parsed = urlparse(href)
        if parsed.scheme not in ("http", "https") or parsed.hostname != app_host:
            continue
        text = link.get("text") or ""
        if UNSAFE_LINK_RX.search(href) or UNSAFE_LINK_RX.search(text) or _FILE_RX.search(href):
            continue
        pattern = _path_pattern(href)
        if pattern in seen_patterns:
            continue
        seen_patterns.add(pattern)
        out.append(urlunparse(parsed._replace(fragment="")))
    return out
