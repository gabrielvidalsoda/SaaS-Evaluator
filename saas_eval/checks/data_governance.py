"""DATA — Data Governance, Backup & Portability."""

from __future__ import annotations

from datetime import datetime

from ..models import Evidence, failed, passed, scored
from .helpers import find_dates, text_near
from .registry import check

_UPDATED_RX = r"(last\s+(updated|modified|revised)|effective\s+(date|as of|from)|updated\s*(on|:)|version date|date of last revision)"


@check(
    "DATA-01", "DATA", "Privacy policy published and recently updated", 20, method="auto+ai",
    rubric=(
        "Is there a privacy policy that explains what data is collected, purposes, retention and user rights, and "
        "was it updated within the last 24 months? pass = both; partial = present but stale or thin; fail = none."
    ),
    pages=("privacy",),
)
def privacy_policy(ev: Evidence):
    url = ev.discovered.get("privacy")
    page = ev.page("privacy")
    if not url:
        return failed("No privacy policy found.")
    if page is None:
        return scored(0.5, f"Privacy policy linked ({url}) but could not be read.", [url])
    dates = find_dates(text_near(page.text, _UPDATED_RX)) or find_dates(page.text[:3000])
    if not dates:
        return scored(0.5, "Privacy policy found, but no 'last updated' date detected.", [url])
    latest = max(dates)
    age_days = (datetime.now().date() - latest).days
    if age_days <= 730:
        return passed(f"Privacy policy updated {latest.isoformat()} ({age_days} days ago).", [url])
    return scored(0.6, f"Privacy policy last updated {latest.isoformat()} — over 24 months ago.", [url])


@check(
    "DATA-02", "DATA", "DPA and data-subject rights (GDPR/LGPD)", 20, method="ai",
    rubric=(
        "Does the vendor offer a Data Processing Agreement/Addendum (DPA) and describe data-subject rights "
        "(access, deletion, portability) under GDPR/LGPD/CCPA, plus a sub-processor list? pass = DPA + rights + "
        "sub-processors; partial = some; fail/not_found = none."
    ),
    pages=("dpa", "privacy", "security", "terms"),
)
def dpa(ev: Evidence):
    return None


@check(
    "DATA-03", "DATA", "Data residency / region options", 15, method="ai",
    rubric=(
        "Does the vendor state where customer data is hosted and offer region choice (e.g. EU/US data residency)? "
        "pass = region choice offered; partial = hosting location disclosed without choice; fail/not_found = not stated."
    ),
    pages=("security", "privacy", "pricing", "dpa", "docs"),
)
def residency(ev: Evidence):
    return None


@check(
    "DATA-04", "DATA", "Data export / portability", 20, method="ai",
    rubric=(
        "Can customers export their data (bulk export, CSV/JSON, API) to avoid lock-in? pass = self-service export "
        "documented; partial = export only via API or on request; fail/not_found = not mentioned."
    ),
    pages=("docs", "api", "privacy", "security", "terms"),
)
def export(ev: Evidence):
    return None


@check(
    "DATA-05", "DATA", "Backup & disaster recovery commitments", 15, method="ai",
    rubric=(
        "Does the vendor describe backups (frequency, retention, encryption) and disaster recovery (RPO/RTO, "
        "multi-region failover)? pass = backups + DR with specifics; partial = generic statements; fail/not_found = none."
    ),
    pages=("security", "docs", "terms", "status"),
)
def backups(ev: Evidence):
    return None


@check("DATA-06", "DATA", "Cookie consent on first visit", 10)
def cookie_consent(ev: Evidence):
    first = ev.pages[0] if ev.pages else None
    if first is None or not first.ok:
        return failed("First page could not be loaded.")
    cmps = sorted(w[4:] for w in first.widgets if w.startswith("cmp:"))
    if first.consent_banner or cmps:
        return passed(
            f"Consent mechanism detected on first visit ({'banner' if first.consent_banner else 'CMP script'}"
            f"{': ' + ', '.join(cmps) if cmps else ''})."
        )
    return failed(
        "No cookie consent banner or consent manager detected on first visit "
        "(banners are often geo-targeted — this reflects the evaluator's location)."
    )
