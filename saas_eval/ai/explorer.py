"""AI browser agent for the authenticated part of the evaluation. Two modes:

- `ai_login`     : fallback when the scripted form login fails (unusual flows).
                   Claude drives the login page and types credentials through
                   `type_credential` — it never sees them. Returns a storage_state.
- `run_explorer` : reuses the logged-in session and explores the product
                   read-only, then judges UX-07 with verified quotes.

Interactive elements are tagged with a sequential id in each snapshot, and the
model acts by id (no brittle CSS selectors)."""

from __future__ import annotations

import base64
import re
from typing import Any, Callable

from claude_agent_sdk import create_sdk_mcp_server, tool
from playwright.async_api import Browser, Page, Playwright, async_playwright

from .. import USER_AGENT_SUFFIX
from ..checks.registry import REGISTRY
from ..collectors.auth import UNSAFE_LINK_RX, looks_logged_out
from ..collectors.urls import registered_domain
from ..config import Settings
from ..credentials import Credentials
from ..models import Evidence, PageEvidence
from ..storage.events import EventLog
from ..storage.files import RunPaths
from .agent import load_prompt, run_session
from .tools import VERDICT_SCHEMA, JudgeContext, handle_verdicts

SERVER_NAME = "explorer"
# Buttons/links the explorer may never click: they could end the session or change
# data. ("Cancel"/"Close" stay allowed — they're how dialogs get dismissed.)
UNSAFE_CLICK_RX = re.compile(
    r"log[- _]?out|sign[- _]?out|\bsair\b|log[- _]?off|delete|remove|excluir|apagar|destroy|unsubscribe|\bpay|checkout|upgrade|"
    r"invite|convidar|\bsave\b|salvar|submit|enviar|\bcreate\b|\bcriar\b|publish|publicar|confirm|confirmar|"
    r"revoke|deactivate|desativar|archive|arquivar|export|exportar|download|baixar",
    re.I,
)
MAX_APP_PAGES = 15
MAX_ELEMENTS = 80

_INTERACTIVE = [
    "a[href]", "button", "input", "select", "textarea", "[role='button']", "[role='link']",
    "[role='checkbox']", "[role='tab']", "[role='menuitem']", "[role='combobox']", "[onclick]",
    "[contenteditable='true']",
]

# Clears stale ids, then tags visible interactive elements with a sequential id.
_SNAPSHOT_JS = """
(selectors) => {
  document.querySelectorAll('[data-se-id]').forEach((el) => el.removeAttribute('data-se-id'));
  const seen = new Set(); let counter = 0; const elements = [];
  for (const el of document.querySelectorAll(selectors.join(','))) {
    if (seen.has(el)) continue; seen.add(el);
    const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    if (!(r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none')) continue;
    counter += 1; el.setAttribute('data-se-id', String(counter));
    const tag = el.tagName.toLowerCase();
    const text = (el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('placeholder')
                  || el.getAttribute('title') || '').trim().replace(/\\s+/g, ' ').slice(0, 100);
    elements.push({ id: counter, tag, role: el.getAttribute('role') || tag, type: el.getAttribute('type') || '', text });
  }
  return { url: location.href, title: document.title, elements,
           text: (document.body ? document.body.innerText : '').slice(0, 30000) };
}
"""


class ExplorerBrowser:
    def __init__(self, headless: bool, storage_state: dict[str, Any] | None = None) -> None:
        self.headless = headless
        self.storage_state = storage_state
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self.page: Page | None = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(headless=self.headless)
        probe = await self._browser.new_page()
        ua = (await probe.evaluate("navigator.userAgent")).replace("HeadlessChrome", "Chrome")
        await probe.close()
        ctx = await self._browser.new_context(
            viewport={"width": 1366, "height": 900}, user_agent=f"{ua} {USER_AGENT_SUFFIX}", storage_state=self.storage_state
        )
        self.page = await ctx.new_page()

    async def close(self) -> None:
        try:
            if self._browser:
                await self._browser.close()
        finally:
            if self._pw:
                await self._pw.stop()

    async def snapshot(self) -> dict[str, Any]:
        try:
            await self.page.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass
        return await self.page.evaluate(_SNAPSHOT_JS, _INTERACTIVE)

    async def locator(self, element_id: int):
        loc = self.page.locator(f"[data-se-id='{element_id}']")
        if await loc.count() == 0:
            raise ValueError(f"Element [{element_id}] no longer exists — use the latest snapshot.")
        return loc.first


def _snapshot_text(snap: dict[str, Any]) -> str:
    lines = [f"URL: {snap['url']}", f"Title: {snap['title']}", "", "Interactive elements:"]
    for el in snap["elements"][:MAX_ELEMENTS]:
        bits = f"[{el['id']}] <{el['tag']} role={el['role']}{' type=' + el['type'] if el['type'] else ''}>"
        lines.append(f"{bits} \"{el['text']}\"" if el["text"] else bits)
    if len(snap["elements"]) > MAX_ELEMENTS:
        lines.append(f"… truncated at {MAX_ELEMENTS} elements")
    lines += ["", "Visible text (start):", snap["text"][:3000]]
    return "\n".join(lines)


def _text(msg: str, error: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": msg}]}
    if error:
        out["is_error"] = True
    return out


def build_explorer_server(
    ctx: JudgeContext,
    browser: ExplorerBrowser,
    paths: RunPaths,
    login_state: dict[str, Any],
    credentials: Credentials | None,
    login_mode: bool,
):
    ev = ctx.ev

    def record(snap: dict[str, Any]) -> None:
        """Keeps the visible text of each explored page so UX-07 quotes can be verified."""
        explored = [p for p in ev.pages if p.kind == "app-explored"]
        existing = next((p for p in explored if p.url == snap["url"]), None)
        if existing:
            existing.text = snap["text"]
        elif len(explored) < MAX_APP_PAGES:
            ev.pages.append(PageEvidence(url=snap["url"], kind="app-explored", final_url=snap["url"], status=200,
                                         title=snap["title"], text=snap["text"], rendered=False))

    async def act(label: str, coro) -> dict[str, Any]:
        try:
            await coro
            await browser.page.wait_for_timeout(700)
        except Exception as exc:
            ctx.log.log("tool_error", {"session": ctx.session, "tool": label, "error": str(exc)[:200]})
            # SPAs re-render constantly and element ids go stale: hand back a fresh
            # snapshot with the error so the model can retry without a wasted turn.
            try:
                fresh = _snapshot_text(await browser.snapshot())
            except Exception:
                fresh = "(snapshot unavailable)"
            first_line = (str(exc).splitlines() or [""])[0][:200]
            return _text(f"Error: {first_line}\n\nFresh snapshot (use these ids):\n{fresh}", error=True)
        snap = await browser.snapshot()
        if not login_mode:
            record(snap)
        return _text(f"{label} done.\n\n{_snapshot_text(snap)}")

    @tool("goto", "Navigate to a URL of the product (same organization domain).",
          {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]})
    async def goto(args: dict[str, Any]) -> dict[str, Any]:
        url = args["url"]
        if registered_domain(url) != ev.org_domain:
            return _text(f"Only {ev.org_domain} URLs can be opened.", error=True)
        if not login_mode and UNSAFE_LINK_RX.search(url):
            return _text("That URL could end the session or change data — not allowed.", error=True)
        ctx.log.log("action", {"session": ctx.session, "tool": "goto", "url": url})
        return await act("goto", browser.page.goto(url, wait_until="load", timeout=45000))

    @tool("click", "Click an element by its id from the latest snapshot.",
          {"type": "object", "properties": {"element_id": {"type": "integer"}}, "required": ["element_id"]})
    async def click(args: dict[str, Any]) -> dict[str, Any]:
        ctx.log.log("action", {"session": ctx.session, "tool": "click", "element_id": args["element_id"]})

        async def do():
            loc = await browser.locator(int(args["element_id"]))
            if not login_mode:
                label = f"{await loc.inner_text()} {await loc.get_attribute('aria-label') or ''} {await loc.get_attribute('href') or ''}"
                if UNSAFE_CLICK_RX.search(label):
                    raise ValueError(f"'{label.strip()[:60]}' could end the session or change data — not allowed.")
            try:
                await loc.click(timeout=5000)
            except Exception:
                # Usually an overlay/animation intercepting the pointer — retry once, forced.
                await loc.click(timeout=5000, force=True)

        return await act("click", do())

    @tool("type", "Type non-secret text into a field (e.g. a search box). NEVER use it for credentials.",
          {"type": "object", "properties": {"element_id": {"type": "integer"}, "text": {"type": "string"}},
           "required": ["element_id", "text"]})
    async def type_(args: dict[str, Any]) -> dict[str, Any]:
        ctx.log.log("action", {"session": ctx.session, "tool": "type", "element_id": args["element_id"], "text": args["text"]})

        async def do():
            loc = await browser.locator(int(args["element_id"]))
            await loc.fill(args["text"], timeout=8000)

        return await act("type", do())

    @tool("type_credential", "Type the test account's username or password into a field. The value comes from the "
          "evaluator's configuration and is never shown to you.",
          {"type": "object", "properties": {"element_id": {"type": "integer"},
                                            "field": {"type": "string", "enum": ["username", "password"]}},
           "required": ["element_id", "field"]})
    async def type_credential(args: dict[str, Any]) -> dict[str, Any]:
        value = (credentials.username if args["field"] == "username" else credentials.password) if credentials else ""
        if not value:
            return _text("No credential configured.", error=True)
        ctx.log.log("action", {"session": ctx.session, "tool": "type_credential", "field": args["field"]})

        async def do():
            loc = await browser.locator(int(args["element_id"]))
            await loc.fill(value, timeout=8000)

        return await act("type_credential", do())

    @tool("press_key", "Press a key on the focused element (e.g. 'Enter', 'Escape', 'Tab').",
          {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]})
    async def press_key(args: dict[str, Any]) -> dict[str, Any]:
        ctx.log.log("action", {"session": ctx.session, "tool": "press_key", "key": args["key"]})
        return await act("press_key", browser.page.keyboard.press(args["key"]))

    @tool("screenshot", "Look at the current screen (layout, visual clarity).", {"type": "object", "properties": {}, "required": []})
    async def screenshot(args: dict[str, Any]) -> dict[str, Any]:
        n = len(list(paths.screenshots_dir.glob("explore-*.jpg")))
        path = paths.screenshots_dir / f"explore-{n:02d}.jpg"
        await browser.page.screenshot(path=str(path), type="jpeg", quality=70)
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        return {"content": [{"type": "text", "text": f"Screenshot {path.name}"}, {"type": "image", "data": data, "mimeType": "image/jpeg"}]}

    @tool("report_login", "Report whether the login succeeded (call once, right after attempting it).",
          {"type": "object", "properties": {"success": {"type": "boolean"}, "note": {"type": "string"}}, "required": ["success"]})
    async def report_login(args: dict[str, Any]) -> dict[str, Any]:
        claimed = bool(args["success"])
        # Trust but verify: a visible password field / login URL means we're not in.
        actual = claimed and not await looks_logged_out(browser.page)
        login_state.update(success=actual, note=args.get("note", "") or ("" if actual else "still on a login page"))
        ctx.log.log("action", {"session": ctx.session, "tool": "report_login", "claimed": claimed, "success": actual},
                    level="INFO" if actual else "WARN")
        ctx.finished = True
        return _text("Noted. The session ends here.")

    @tool("submit_verdicts", "Submit the UX-07 verdict with verbatim quotes of in-app text (machine-verified).", VERDICT_SCHEMA)
    async def submit_verdicts(args: dict[str, Any]) -> dict[str, Any]:
        return handle_verdicts(ctx, args)

    @tool("finish", "End the exploration.", {"type": "object", "properties": {}, "required": []})
    async def finish(args: dict[str, Any]) -> dict[str, Any]:
        ctx.finished = True
        return _text("Exploration finished.")

    common = [goto, click, type_, press_key, screenshot]
    tools = common + ([type_credential, report_login] if login_mode else [submit_verdicts, finish])
    return create_sdk_mcp_server(name=SERVER_NAME, version="1.0.0", tools=tools), [t.name for t in tools]


async def ai_login(
    url: str, credentials: Credentials, ev: Evidence, paths: RunPaths, log: EventLog, settings: Settings,
    progress: Callable[[str], None], headless: bool = True,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """AI-driven login (fallback). Returns (storage_state | None, info)."""
    system_prompt, _ = load_prompt(settings.prompts_dir / "login.md")
    ctx = JudgeContext(session="login", ev=ev, checks={}, log=log, progress=progress)
    login_state: dict[str, Any] = {"success": False, "note": "the AI agent did not report a result"}
    browser = ExplorerBrowser(headless=headless)
    await browser.start()
    try:
        await browser.page.goto(url, wait_until="load", timeout=45000)
        snap = await browser.snapshot()
        server, tool_names = build_explorer_server(ctx, browser, paths, login_state, credentials, login_mode=True)
        log.log("ai_start", {"session": "login", "url": url})
        await run_session(
            system_prompt=system_prompt,
            user_prompt=f"Log in to {ev.org_domain}. The login page is open.\n\n{_snapshot_text(snap)}",
            model=settings.model, max_turns=25, timeout_s=300,
            mcp_server=server, server_name=SERVER_NAME, tool_names=tool_names,
            should_stop=lambda: ctx.finished,
        )
        state = await browser.page.context.storage_state() if login_state["success"] else None
        return state, {"success": state is not None, "note": login_state.get("note", ""), "final_url": browser.page.url}
    finally:
        await browser.close()


async def run_explorer(
    ev: Evidence, storage_state: dict[str, Any], paths: RunPaths, log: EventLog, settings: Settings,
    progress: Callable[[str], None], headless: bool = True,
) -> dict[str, Any]:
    """Read-only exploration of the logged-in app; judges UX-07."""
    system_prompt, _ = load_prompt(settings.prompts_dir / "explorer.md")
    ctx = JudgeContext(session="explorer", ev=ev, checks={"UX-07": REGISTRY["UX-07"]}, log=log, progress=progress,
                       allow_screenshots=True)
    browser = ExplorerBrowser(headless=headless, storage_state=storage_state)
    await browser.start()
    try:
        start = next((p.final_url for p in ev.pages if p.kind == "app" and p.ok), ev.target_url)
        await browser.page.goto(start, wait_until="load", timeout=45000)
        snap = await browser.snapshot()
        if await looks_logged_out(browser.page):
            log.log("ai_error", {"session": "explorer", "error": "session not valid in the explorer browser"})
            return {"explored": False, "note": "session not valid"}
        server, tool_names = build_explorer_server(ctx, browser, paths, {}, None, login_mode=False)
        progress("AI (explorer): exploring the app")
        log.log("ai_start", {"session": "explorer", "start": start})
        await run_session(
            system_prompt=system_prompt,
            user_prompt=f"Product: {ev.org_domain}. You are logged in.\n\nUX-07 rubric: {REGISTRY['UX-07'].rubric}\n\n{_snapshot_text(snap)}",
            model=settings.model, max_turns=60, timeout_s=900,
            mcp_server=server, server_name=SERVER_NAME, tool_names=tool_names,
            should_stop=lambda: ctx.finished,
        )
    except RuntimeError:
        raise
    except Exception as exc:
        log.log("ai_error", {"session": "explorer", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        await browser.close()
    ev.ai_verdicts.update(ctx.verdicts)
    return {"explored": True, "pages": sum(1 for p in ev.pages if p.kind == "app-explored"), "verdict": "UX-07" in ctx.verdicts}
