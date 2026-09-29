"""SEC — Security & Compliance. All checks are passive observations."""

from __future__ import annotations

import re

from ..collectors.urls import registered_domain
from ..models import Evidence, failed, na, not_evaluated, passed, scored
from .helpers import lerp
from .registry import check

_SESSION_COOKIE_RX = re.compile(
    r"sess|sid$|^sid|auth|token|jwt|remember|login|logged|connect\.sid|phpsessid|jsessionid|laravel|csrf|xsrf", re.I
)


def _headers(ev: Evidence) -> dict[str, str]:
    return ev.http.get("headers") or {}


@check("SEC-01", "SEC", "HTTPS enforced (HTTP redirects to HTTPS)", 10, critical=True)
def https_enforced(ev: Evidence):
    if not ev.http.get("https_ok"):
        return failed(f"The site is not reachable over HTTPS: {ev.http.get('https_error', 'unknown error')}")
    redirect = ev.http.get("http_redirect", {})
    final = redirect.get("final_url", "")
    if redirect.get("error") and not redirect.get("chain"):
        return passed("HTTPS works; plain HTTP (port 80) is closed.")
    if final.startswith("https://"):
        hops = " → ".join(str(h["status"]) for h in redirect.get("chain", []))
        return passed(f"HTTP redirects to HTTPS ({hops}).", [final])
    return scored(0.4, f"HTTPS works, but plain HTTP is served without redirecting to HTTPS ({final}).")


@check("SEC-02", "SEC", "Modern TLS only, valid certificate", 10)
def tls_quality(ev: Evidence):
    cert = ev.tls.get("certificate") or {}
    versions = ev.tls.get("versions") or {}
    if not cert:
        return not_evaluated(f"TLS probe failed: {ev.tls.get('error', 'no data')}")
    if not cert.get("valid"):
        return failed(f"Certificate is not valid: {cert.get('error')}")
    factor, notes = 1.0, [f"cert by {cert.get('issuer')}, expires in {cert.get('days_to_expiry')} days"]
    legacy = [v for v in ("TLSv1.0", "TLSv1.1") if versions.get(v)]
    if legacy:
        factor -= 0.4
        notes.append(f"legacy protocols accepted: {', '.join(legacy)}")
    if not versions.get("TLSv1.3"):
        factor -= 0.1
        notes.append("TLS 1.3 not supported")
    days = cert.get("days_to_expiry", 999)
    if days < 14:
        factor -= 0.5
        notes.append("certificate expires in under 14 days")
    return scored(factor, "; ".join(notes) + ".")


@check("SEC-03", "SEC", "HSTS enabled with long max-age", 8)
def hsts(ev: Evidence):
    value = _headers(ev).get("strict-transport-security")
    if not value:
        return failed("No Strict-Transport-Security header.")
    m = re.search(r"max-age=(\d+)", value, re.I)
    max_age = int(m.group(1)) if m else 0
    subdomains = "includesubdomains" in value.lower()
    if max_age >= 15552000:
        return scored(1.0 if subdomains else 0.8, f"HSTS max-age={max_age}s{' + includeSubDomains' if subdomains else ''}.", [value])
    if max_age > 0:
        return scored(0.4, f"HSTS present but max-age is short ({max_age}s < 180 days).", [value])
    return failed("HSTS header present but max-age is 0 or missing.", [value])


@check("SEC-04", "SEC", "Content-Security-Policy restricts scripts", 8)
def csp(ev: Evidence):
    headers = _headers(ev)
    policy = headers.get("content-security-policy")
    if not policy:
        if headers.get("content-security-policy-report-only"):
            return scored(0.25, "CSP only in report-only mode (not enforced).")
        return failed("No Content-Security-Policy header.")
    directives = {}
    for part in policy.split(";"):
        tokens = part.strip().split()
        if tokens:
            directives[tokens[0].lower()] = [t.lower() for t in tokens[1:]]
    script = directives.get("script-src") or directives.get("default-src")
    if script is None:
        return scored(0.4, "CSP present but does not restrict scripts (no script-src/default-src).", [policy[:200]])
    has_nonce_or_hash = any(t.startswith(("'nonce-", "'sha256-", "'sha384-", "'sha512-")) for t in script)
    weak = []
    if "'unsafe-inline'" in script and not has_nonce_or_hash:
        weak.append("'unsafe-inline'")
    if "*" in script or "https:" in script or "http:" in script:
        weak.append("wildcard source")
    if "'unsafe-eval'" in script:
        weak.append("'unsafe-eval'")
    if weak:
        return scored(0.5, f"CSP restricts scripts but allows {', '.join(weak)}.", [policy[:200]])
    return passed("CSP restricts script sources without unsafe-inline/wildcards.", [policy[:200]])


@check("SEC-05", "SEC", "Security headers (nosniff, framing, referrer, permissions)", 8)
def security_headers(ev: Evidence):
    h = _headers(ev)
    csp_value = h.get("content-security-policy", "").lower()
    items = {
        "X-Content-Type-Options: nosniff": h.get("x-content-type-options", "").lower() == "nosniff",
        "clickjacking protection (X-Frame-Options / frame-ancestors)": bool(h.get("x-frame-options")) or "frame-ancestors" in csp_value,
        "Referrer-Policy": bool(h.get("referrer-policy")),
        "Permissions-Policy": bool(h.get("permissions-policy") or h.get("feature-policy")),
    }
    present = [k for k, v in items.items() if v]
    missing = [k for k, v in items.items() if not v]
    detail = f"Present: {', '.join(present) or 'none'}." + (f" Missing: {', '.join(missing)}." if missing else "")
    return scored(len(present) / len(items), detail)


@check("SEC-06", "SEC", "Public-site cookies use Secure / HttpOnly / SameSite", 8)
def cookie_flags(ev: Evidence):
    cookies = [c for c in ev.cookies if c.get("first_party") and not c.get("after_login")]
    if not cookies:
        return na("No first-party cookies were set on the public pages visited.")
    secure = sum(c["secure"] for c in cookies) / len(cookies)
    same_site = sum(1 for c in cookies if c.get("same_site") in ("Lax", "Strict") or (c.get("same_site") == "None" and c["secure"])) / len(cookies)
    session = [c for c in cookies if _SESSION_COOKIE_RX.search(c["name"])]
    parts = [secure, same_site]
    detail = [f"{len(cookies)} first-party cookies: {secure:.0%} Secure, {same_site:.0%} with SameSite"]
    if session:
        http_only = sum(c["http_only"] for c in session) / len(session)
        parts.append(http_only)
        detail.append(f"{http_only:.0%} of {len(session)} session-like cookies HttpOnly")
    insecure = [c["name"] for c in cookies if not c["secure"]][:5]
    return scored(sum(parts) / len(parts), "; ".join(detail) + ".", [f"not Secure: {', '.join(insecure)}"] if insecure else [])


@check("SEC-07", "SEC", "No mixed content, exposed files or version banners", 10)
def exposure(ev: Evidence):
    exposed = [path for path, hit in (ev.http.get("exposed_files") or {}).items() if hit]
    if exposed:
        return failed(f"Sensitive files publicly readable: {', '.join(exposed)}.", exposed)
    factor, notes = 1.0, []
    mixed = sorted({u for p in ev.ok_pages() for u in p.mixed_content})
    if mixed:
        factor -= 0.4
        notes.append(f"{len(mixed)} mixed-content (http://) resources on HTTPS pages")
    h = ev.http.get("headers") or {}
    banners = [f"{k}: {h[k]}" for k in ("server", "x-powered-by", "x-aspnet-version", "x-aspnetmvc-version")
               if k in h and re.search(r"\d+\.\d+", h[k])]
    if banners:
        factor -= 0.3
        notes.append(f"software versions disclosed ({'; '.join(banners)})")
    if not notes:
        return passed("No mixed content, no exposed .git/.env, no version banners.")
    return scored(factor, "; ".join(notes).capitalize() + ".", mixed[:3] + banners)


@check("SEC-08", "SEC", "security.txt published", 3)
def security_txt(ev: Evidence):
    info = ev.http.get("security_txt") or {}
    if info.get("valid"):
        return passed("/.well-known/security.txt present with a Contact field.")
    return failed(f"No valid /.well-known/security.txt (HTTP {info.get('status')}).")


@check("SEC-09", "SEC", "Email domain protected (SPF + enforcing DMARC)", 5)
def email_auth(ev: Evidence):
    dns = ev.dns or {}
    if not dns.get("lookup_ok"):
        return not_evaluated(f"DNS lookup for {dns.get('domain', ev.org_domain)} failed.")
    factor, notes = 0.0, []
    spf = dns.get("spf")
    if spf and not re.search(r"[+?]all\b", spf):
        factor += 0.4
        notes.append("SPF present")
    else:
        notes.append("SPF missing or permissive" if spf else "no SPF record")
    if dns.get("dmarc"):
        factor += 0.3
        policy = dns.get("dmarc_policy")
        if policy in ("quarantine", "reject"):
            factor += 0.3
        notes.append(f"DMARC p={policy}")
    else:
        notes.append("no DMARC record")
    return scored(factor, f"{dns.get('domain')}: " + ", ".join(notes) + ".")


@check(
    "SEC-10", "SEC", "MFA and SSO (SAML/OIDC) offered", 10, method="ai",
    rubric=(
        "Does the product offer multi-factor authentication (MFA/2FA) AND single sign-on for organizations "
        "(SAML, OIDC, Okta/Azure AD/Google Workspace SSO)? pass = both clearly offered; partial = only one of "
        "them, or only social login (e.g. 'Sign in with Google') without enterprise SSO; fail/not_found = neither."
    ),
    pages=("login", "security", "pricing", "docs", "signup"),
)
def mfa_sso(ev: Evidence):
    return None


@check(
    "SEC-11", "SEC", "Compliance attestations (SOC 2, ISO 27001, GDPR, …)", 15, method="ai",
    rubric=(
        "Which security/compliance attestations does the vendor publicly claim to HOLD (not 'working towards')? "
        "Consider SOC 2 (Type I/II), ISO/IEC 27001, GDPR/LGPD compliance, HIPAA, PCI DSS, and whether there is a "
        "trust center. pass = SOC 2 Type II or ISO 27001 held AND GDPR addressed; partial (factor 0.3-0.8) = some "
        "attestations or only privacy-law compliance claims; fail/not_found = no attestation claimed."
    ),
    pages=("security", "privacy", "dpa", "pricing", "home", "target"),
)
def compliance(ev: Evidence):
    return None


@check("SEC-12", "SEC", "Limited third-party script exposure", 5)
def third_party_scripts(ev: Evidence):
    # Only the vendor's own pages: a third-party-hosted status page or docs portal
    # loads its own scripts, which aren't exposure of the vendor's app.
    own = [p for p in ev.ok_pages() if registered_domain(p.final_url or p.url) == ev.org_domain]
    if not own:
        return not_evaluated("No first-party page could be loaded.")
    origins = sorted({o for p in own for o in p.script_origins})
    factor = lerp(len(origins), 3, 15)
    return scored(factor, f"{len(origins)} third-party script origins across crawled pages.", origins[:10])


@check("SEC-13", "SEC", "Authenticated session hardening", 12)
def session_hardening(ev: Evidence):
    if not ev.login:
        return na("Not applicable without login.")
    if not ev.login.get("success"):
        return not_evaluated("Login did not succeed.")
    cookies = [c for c in ev.cookies if c.get("after_login") and c.get("first_party")]
    session = [c for c in cookies if _SESSION_COOKIE_RX.search(c["name"])]
    tokens = ev.login.get("token_keys_in_local_storage") or []
    parts, notes = [], []
    if cookies:
        secure = sum(c["secure"] for c in cookies) / len(cookies)
        same_site = sum(1 for c in cookies if c.get("same_site") in ("Lax", "Strict")) / len(cookies)
        parts += [secure, same_site]
        notes.append(f"{len(cookies)} first-party cookies after login: {secure:.0%} Secure, {same_site:.0%} SameSite Lax/Strict")
    if session:
        http_only = sum(c["http_only"] for c in session) / len(session)
        parts.append(http_only)
        notes.append(f"{http_only:.0%} of session cookies HttpOnly ({', '.join(c['name'] for c in session[:4])})")
    # Tokens readable by JavaScript can be stolen by any XSS; HttpOnly cookies can't.
    parts.append(0.0 if tokens else 1.0)
    notes.append(f"auth tokens in localStorage: {', '.join(tokens)}" if tokens else "no auth tokens in localStorage")
    return scored(sum(parts) / len(parts), "; ".join(notes) + ".")
