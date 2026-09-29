"""Check functions against hand-built evidence, the registry's AI/deterministic
rules, discovery heuristics, date parsing and the AI quote guardrail."""

from __future__ import annotations

from datetime import date

from saas_eval.ai.verify import Verdict, verify
from saas_eval.checks import registry
from saas_eval.checks.helpers import find_dates, lerp
from saas_eval.collectors.discovery import classify_links
from saas_eval.models import Evidence, PageEvidence, active_pillars


def evidence(**http) -> Evidence:
    ev = Evidence(target_url="https://app.acme.com/", host="app.acme.com", org_domain="acme.com")
    ev.http = {"https_ok": True, "headers": {}, **http}
    ev.pages = [PageEvidence(url="https://app.acme.com/", kind="target", final_url="https://app.acme.com/", status=200)]
    return ev


def run(check_id: str, ev: Evidence):
    registry._load_all()
    return registry.evaluate(registry.REGISTRY[check_id], ev)


# ── security ─────────────────────────────────────────────────────────────────


def test_sec01_fails_without_https_and_is_critical():
    ev = evidence(https_ok=False, https_error="ConnectError")
    out = run("SEC-01", ev)
    assert out.status == "fail"
    assert registry.REGISTRY["SEC-01"].critical


def test_sec01_passes_on_redirect_and_partial_without():
    ok = run("SEC-01", evidence(http_redirect={"chain": [{"status": 301}], "final_url": "https://acme.com/"}))
    assert ok.status == "pass"
    weak = run("SEC-01", evidence(http_redirect={"chain": [{"status": 200}], "final_url": "http://acme.com/"}))
    assert weak.status == "partial"


def test_sec03_hsts_levels():
    assert run("SEC-03", evidence(headers={"strict-transport-security": "max-age=63072000; includeSubDomains"})).factor == 1.0
    assert run("SEC-03", evidence(headers={"strict-transport-security": "max-age=31536000"})).factor == 0.8
    assert run("SEC-03", evidence(headers={"strict-transport-security": "max-age=600"})).factor == 0.4
    assert run("SEC-03", evidence(headers={})).status == "fail"


def test_sec04_csp_strength():
    strong = run("SEC-04", evidence(headers={"content-security-policy": "default-src 'self'; script-src 'self' 'nonce-abc'"}))
    assert strong.status == "pass"
    weak = run("SEC-04", evidence(headers={"content-security-policy": "script-src 'self' 'unsafe-inline'"}))
    assert weak.factor == 0.5
    frame_only = run("SEC-04", evidence(headers={"content-security-policy": "frame-ancestors 'none'"}))
    assert frame_only.factor == 0.4
    assert run("SEC-04", evidence(headers={})).status == "fail"


def test_sec06_is_na_without_first_party_cookies():
    ev = evidence()
    ev.cookies = [{"name": "_ga", "secure": False, "http_only": False, "same_site": "", "first_party": False}]
    assert run("SEC-06", ev).status == "na"


def test_sec07_exposed_git_is_a_hard_fail():
    out = run("SEC-07", evidence(exposed_files={"/.git/HEAD": True, "/.env": False}))
    assert out.status == "fail" and "/.git/HEAD" in out.detail


def test_sec09_dmarc_not_evaluated_when_lookup_failed():
    ev = evidence()
    ev.dns = {"domain": "acme.com", "lookup_ok": False}
    assert run("SEC-09", ev).status == "not_evaluated"
    ev.dns = {"domain": "acme.com", "lookup_ok": True, "spf": "v=spf1 include:x ~all", "dmarc": "v=DMARC1; p=reject", "dmarc_policy": "reject"}
    assert run("SEC-09", ev).status == "pass"


# ── registry rules (AI vs deterministic) ─────────────────────────────────────


def test_ai_check_is_not_evaluated_in_deterministic_mode():
    ev = evidence()
    ev.ai_enabled = False
    out = run("SEC-11", ev)
    assert out.status == "not_evaluated" and "--deterministic" in out.detail


def test_ai_check_uses_verified_verdict_only():
    ev = evidence()
    ev.ai_enabled = True
    ev.ai_verdicts["SEC-11"] = {"verdict": "pass", "reason": "SOC 2", "quotes": [], "verified": True}
    assert run("SEC-11", ev).status == "pass"
    ev.ai_verdicts["SEC-11"] = {"verdict": "pass", "verified": False, "rejection": "quote not found"}
    out = run("SEC-11", ev)
    assert out.status == "not_evaluated" and "rejected" in out.detail


def test_auto_ai_check_falls_back_to_heuristic_without_ai():
    ev = evidence()
    ev.discovered["pricing"] = "https://acme.com/pricing"
    ev.pages.append(PageEvidence(url="https://acme.com/pricing", kind="pricing", status=200,
                                 text="Starter $10 per user / month. Pro $25 per user / month."))
    out = run("TRN-01", ev)
    assert out.status == "pass" and out.source == "heuristic"


def test_in_app_check_is_na_without_login():
    assert run("UX-07", evidence()).status == "na"


def test_a11y_checks_only_registered_for_a11y_pillar():
    registry._load_all()
    default_ids = {c.id for c in registry.checks_for(active_pillars(False))}
    assert not any(cid.startswith("A11Y") for cid in default_ids)
    assert any(c.id.startswith("A11Y") for c in registry.checks_for(active_pillars(True)))


def test_every_ai_check_has_a_rubric():
    registry._load_all()
    for c in registry.REGISTRY.values():
        if c.method in ("ai", "auto+ai"):
            assert c.rubric, c.id


# ── helpers / discovery / guardrail ──────────────────────────────────────────


def test_lerp_both_directions():
    assert lerp(100, 200, 800) == 1.0
    assert lerp(500, 200, 800) == 0.5
    assert lerp(900, 200, 800) == 0.0


def test_find_dates_formats_and_future_filter():
    text = "Last updated: March 17, 2025. Effective 2024-01-05. 3rd of June 2023. Planned: December 1, 2099."
    found = find_dates(text, today=date(2026, 9, 28))
    assert date(2025, 3, 17) in found and date(2024, 1, 5) in found and date(2023, 6, 3) in found
    assert all(d.year < 2099 for d in found)


def test_classify_links():
    links = [
        {"href": "https://acme.com/pricing", "text": "Pricing"},
        {"href": "https://status.acme.com/", "text": "Status"},
        {"href": "https://acme.statuspage.io/", "text": ""},
        {"href": "https://developers.acme.com/", "text": "API"},
        {"href": "https://acme.com/legal/privacy", "text": "Privacy Policy"},
        {"href": "https://twitter.com/acme", "text": "Twitter"},
        {"href": "https://acme.com/login", "text": "Log in"},
    ]
    found = classify_links(links, "acme.com")
    assert found["pricing"] == "https://acme.com/pricing"
    assert found["status"] == "https://status.acme.com/"
    assert found["api"] == "https://developers.acme.com/"
    assert found["privacy"] == "https://acme.com/legal/privacy"
    assert found["login"] == "https://acme.com/login"


def test_guardrail_rejects_fabricated_quote_and_accepts_real_one():
    ev = evidence()
    ev.pages.append(PageEvidence(url="https://acme.com/security", kind="security", status=200,
                                 text="Acme is SOC 2 Type II certified.\nWe encrypt data at rest."))
    real = Verdict(check_id="SEC-11", verdict="pass", reason="", quotes=[{"url": "https://acme.com/security", "quote": "SOC 2  Type II certified"}])
    assert verify(real, ev)[0]
    fake = Verdict(check_id="SEC-11", verdict="pass", reason="", quotes=[{"url": "https://acme.com/security", "quote": "ISO 27001 certified"}])
    ok, why, _ = verify(fake, ev)
    assert not ok and "verbatim" in why
    no_quotes = Verdict(check_id="SEC-11", verdict="partial", factor=0.5, reason="", quotes=[])
    assert not verify(no_quotes, ev)[0]
    negative = Verdict(check_id="SEC-11", verdict="not_found", reason="", quotes=[])
    assert verify(negative, ev)[0]


def test_classify_links_portuguese_and_in_page_sections():
    links = [
        {"href": "https://acme.com.br/#precos", "text": "Preços"},
        {"href": "https://acme.com.br/privacidade", "text": "Política de Privacidade"},
        {"href": "https://acme.com.br/termos", "text": "Termos de Uso"},
        {"href": "https://acme.com.br/#contato", "text": "Fale com nossa equipe"},
        {"href": "https://app.acme.com.br/", "text": "Entrar"},
    ]
    found = classify_links(links, "acme.com.br")
    assert found["privacy"] == "https://acme.com.br/privacidade"
    assert found["terms"] == "https://acme.com.br/termos"
    assert found["pricing"] == "https://acme.com.br/"
    assert found["contact"] == "https://acme.com.br/"
    assert found["login"] == "https://app.acme.com.br/"


def test_page_lookup_falls_back_to_discovered_url():
    ev = evidence()
    ev.pages.append(PageEvidence(url="https://acme.com/", kind="home", final_url="https://acme.com/", status=200, text="Plano Pro R$ 99 por mês"))
    ev.discovered["pricing"] = "https://acme.com/#pricing"
    assert ev.page("pricing").kind == "home"
