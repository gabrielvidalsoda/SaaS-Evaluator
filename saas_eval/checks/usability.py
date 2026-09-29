"""UX — Usability & User Experience ("user friendly")."""

from __future__ import annotations

from ..models import Evidence, failed, na, not_evaluated, passed, scored
from .helpers import app_console_errors, lerp, pages
from .registry import check

_NIELSEN = (
    "visibility of system status; match with the real world (clear language); user control and freedom; "
    "consistency and standards; error prevention; recognition rather than recall; flexibility and efficiency; "
    "aesthetic and minimalist design; help users recognize and recover from errors; help and documentation"
)


@check(
    "UX-01", "UX", "Nielsen usability heuristics (visual review)", 30, method="ai",
    rubric=(
        f"Rate the visible interface against Nielsen's 10 heuristics ({_NIELSEN}) using the screenshots and page "
        "structure. Score each heuristic 1-5 mentally, then report verdict 'partial' with factor = average/5 "
        "(or 'pass' if every heuristic is 5). Quote visible UI text as evidence."
    ),
    pages=("target", "home", "pricing", "login", "signup", "docs"),
)
def nielsen(ev: Evidence):
    return None


@check(
    "UX-02", "UX", "Clear navigation, primary call-to-action and search", 10, method="ai",
    rubric=(
        "Is there a clear, consistent primary navigation, an obvious primary call-to-action (e.g. sign up / get "
        "started / log in), and a way to search content (site or docs search)? pass = all three; partial = two; "
        "fail = one or none."
    ),
    pages=("target", "home", "docs"),
)
def navigation(ev: Evidence):
    return None


@check("UX-03", "UX", "No broken links or failing pages", 15)
def broken_links(ev: Evidence):
    link = ev.link_checks or {}
    checked = int(link.get("checked", 0))
    broken = dict(link.get("broken", {}))
    for p in ev.pages:
        if p.status is not None and (p.status in (404, 410) or p.status >= 500):
            broken[p.url] = p.status
    total = checked + len(ev.pages)
    if total == 0:
        return not_evaluated("No links or pages could be checked.")
    ratio = len(broken) / total
    detail = f"{len(broken)} broken of {total} links/pages checked ({ratio:.0%})."
    if link.get("inconclusive"):
        detail += f" {link['inconclusive']} inconclusive (auth/bot protection)."
    return scored(lerp(ratio, 0.0, 0.10), detail, [f"{s} {u}" for u, s in list(broken.items())[:5]])


@check("UX-04", "UX", "No JavaScript errors on public pages", 10)
def js_errors(ev: Evidence):
    ok = [p for p in ev.rendered_pages() if p.kind != "app"]  # in-app pages: UX-08
    if not ok:
        return not_evaluated("No page could be loaded in the browser.")
    # Uncaught exceptions weigh double compared with console.error calls. Browser
    # security/network noise (sandboxed iframes, CSP refusals, failed resource loads —
    # the latter are covered by UX-03) is not an application bug.
    app_errors = [app_console_errors(p) for p in ok]
    per_page = [len(errs) + 2 * len(p.page_errors) for errs, p in zip(app_errors, ok)]
    avg = sum(per_page) / len(per_page)
    samples = [e for errs, p in zip(app_errors, ok) for e in (p.page_errors + errs)][:3]
    return scored(lerp(avg, 0.0, 5.0), f"{avg:.1f} weighted JS errors per page over {len(ok)} pages.", samples)


@check(
    "UX-05", "UX", "Login/sign-up form usability", 10, method="auto+ai",
    rubric=(
        "Looking at the login and sign-up pages: are fields clearly labelled, is the flow short, are there helpful "
        "affordances (password reveal, 'forgot password', SSO buttons, clear error/validation messaging)? "
        "pass = excellent; partial = usable with gaps; fail = confusing."
    ),
    pages=("login", "signup"),
)
def auth_forms(ev: Evidence):
    form_pages = [p for p in pages(ev, "login", "signup", "target") if p.forms.get("fields")]
    form_pages = [p for p in form_pages if any(f["type"] in ("password", "email") for f in p.forms["fields"])]
    if not form_pages:
        return not_evaluated("No login/sign-up form found on the pages visited.")
    fields = [f for p in form_pages for f in p.forms["fields"]]
    labelled = sum(1.0 if (f["label"] or f["aria"]) else 0.5 if f["placeholder"] else 0.0 for f in fields) / len(fields)
    credential = [f for f in fields if f["type"] in ("password", "email") or "user" in f["name"].lower()]
    autocomplete = (sum(1 for f in credential if f["autocomplete"] and f["autocomplete"] != "off") / len(credential)) if credential else 1.0
    has_password = any(f["type"] == "password" for f in fields)
    toggle = any(p.forms.get("password_toggle") for p in form_pages)
    parts = [labelled, autocomplete] + ([1.0 if toggle else 0.0] if has_password else [])
    detail = (
        f"{len(fields)} fields on {len(form_pages)} form page(s): {labelled:.0%} labelled, "
        f"{autocomplete:.0%} credential fields with autocomplete"
        + (f", password reveal {'present' if toggle else 'absent'}" if has_password else "") + "."
    )
    return scored(sum(parts) / len(parts), detail, [p.url for p in form_pages])


@check("UX-06", "UX", "Help & onboarding resources available", 5)
def help_available(ev: Evidence):
    docs = ev.discovered.get("docs")
    chat = sorted(w for w in ev.all_widgets() if w.startswith("chat:"))
    factor = (0.6 if docs else 0.0) + (0.4 if chat else 0.0)
    detail = f"Help center/docs: {docs or 'not found'}; in-app chat: {', '.join(c[5:] for c in chat) or 'none'}."
    return scored(factor, detail)


@check(
    "UX-07", "UX", "In-app experience (onboarding, empty states, discoverability)", 20, method="ai",
    rubric=(
        "After logging in: is there onboarding guidance, are empty states helpful, can the main tasks be found "
        "without documentation, is feedback clear after actions? pass = excellent; partial = usable with gaps; "
        "fail = confusing."
    ),
    pages=("app",),
)
def in_app(ev: Evidence):
    if not ev.login:
        return na("Not applicable without --login (only the public surface was evaluated).")
    if not ev.login.get("success"):
        return not_evaluated(f"Login was requested but did not succeed. {ev.login.get('note', '')}".strip())
    return None


def _app_pages(ev: Evidence):
    return [p for p in ev.rendered_pages() if p.kind == "app"]


@check("UX-08", "UX", "In-app stability (JS errors, failing API calls)", 15)
def in_app_stability(ev: Evidence):
    if not ev.login:
        return na("Not applicable without login.")
    app = _app_pages(ev)
    if not app:
        return not_evaluated("No in-app page could be loaded after login.")
    js = [len(app_console_errors(p)) + 2 * len(p.page_errors) for p in app]
    # Failed first-party requests (API/XHR 5xx, or 4xx other than auth probes).
    failing = []
    for p in app:
        for f in p.failed_requests:
            status, _, url = f.partition(" ")
            if ev.org_domain in url and (status.startswith("5") or status in ("400", "404", "409", "422")):
                failing.append(f)
    js_factor = lerp(sum(js) / len(app), 0.0, 5.0)
    api_factor = lerp(len(failing) / len(app), 0.0, 2.0)
    samples = [e for p in app for e in p.page_errors + app_console_errors(p)][:2] + failing[:3]
    return scored(
        0.5 * js_factor + 0.5 * api_factor,
        f"{sum(js) / len(app):.1f} weighted JS errors and {len(failing) / len(app):.1f} failing first-party requests "
        f"per page over {len(app)} in-app pages.",
        samples,
    )
