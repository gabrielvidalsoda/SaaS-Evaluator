"""TRN — Transparency, Cost & Innovation."""

from __future__ import annotations

import re
from datetime import datetime

from ..models import Evidence, failed, passed, scored
from .helpers import find_dates
from .registry import check

_PRICE_RX = re.compile(r"(\$|€|£|R\$|USD|EUR|BRL)\s?\d|\d+(?:[.,]\d+)?\s?(\$|€|£|USD|EUR)|\bfree\b", re.I)
_PER_RX = re.compile(r"per (user|seat|month|member|editor)|/\s?(mo|month|user|seat)\b|billed (annually|monthly)", re.I)


@check(
    "TRN-01", "TRN", "Public, clear pricing", 30, method="auto+ai",
    rubric=(
        "Is pricing public and easy to understand (named tiers, prices, what is included, billing period)? "
        "pass = clear public prices per tier; partial = some tiers 'contact sales' or unclear limits; "
        "fail = no public pricing."
    ),
    pages=("pricing",),
)
def pricing(ev: Evidence):
    url = ev.discovered.get("pricing")
    if not url:
        return failed("No pricing page found.")
    page = ev.page("pricing")
    if page is None:
        return scored(0.5, f"Pricing page linked ({url}) but could not be read.", [url])
    prices = len(_PRICE_RX.findall(page.text))
    per = bool(_PER_RX.search(page.text))
    if prices >= 2 and per:
        return passed(f"Pricing page shows prices with billing units ({prices} price mentions).", [url])
    if prices >= 1:
        return scored(0.7, "Pricing page found; prices partly shown.", [url])
    return scored(0.4, "Pricing page found but no explicit prices (likely 'contact sales').", [url])


@check(
    "TRN-02", "TRN", "Free trial or free tier", 20, method="ai",
    rubric="Can a prospect try the product without talking to sales (free trial or free plan)? pass = yes; partial = demo only / on request; fail/not_found = no.",
    pages=("pricing", "signup", "home", "target"),
)
def trial(ev: Evidence):
    return None


@check(
    "TRN-03", "TRN", "Active release cadence (changelog < 90 days)", 30, method="auto+ai",
    rubric=(
        "Is there a public changelog/release notes, and what is the date of the most recent entry? "
        "pass = entry within the last 90 days; partial = within a year; fail = older or no changelog."
    ),
    pages=("changelog",),
)
def changelog(ev: Evidence):
    url = ev.discovered.get("changelog")
    if not url:
        return failed("No public changelog / release notes found.")
    page = ev.page("changelog")
    dates = find_dates(page.text[:20000]) if page else []
    if not dates:
        return scored(0.4, "Changelog found, but no entry dates could be read.", [url])
    latest = max(dates)
    age = (datetime.now().date() - latest).days
    if age <= 90:
        return passed(f"Latest changelog entry {latest.isoformat()} ({age} days ago).", [url])
    if age <= 365:
        return scored(0.6, f"Latest changelog entry {latest.isoformat()} ({age} days ago).", [url])
    return scored(0.4, f"Changelog is stale: latest entry {latest.isoformat()}.", [url])


@check("TRN-04", "TRN", "Public roadmap", 10)
def roadmap(ev: Evidence):
    url = ev.discovered.get("roadmap")
    return passed(f"Public roadmap: {url}.", [url]) if url else failed("No public roadmap found.")


@check(
    "TRN-05", "TRN", "Clear cancellation & renewal terms", 10, method="ai",
    rubric=(
        "Are cancellation, refund and auto-renewal terms clearly stated (e.g. cancel anytime, notice period, "
        "refund policy)? pass = clear; partial = mentioned but vague; fail/not_found = not stated."
    ),
    pages=("terms", "pricing"),
)
def cancellation(ev: Evidence):
    return None
