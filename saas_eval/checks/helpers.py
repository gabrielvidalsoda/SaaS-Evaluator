"""Shared helpers for check functions."""

from __future__ import annotations

import re
import statistics
from datetime import date, datetime

from ..models import Evidence, PageEvidence


# Browser security/network console noise (sandboxed iframes, CSP refusals, failed
# resource loads) — not application bugs.
BROWSER_NOISE = re.compile(
    r"Failed to load resource|Blocked script execution|Content Security Policy|Refused to (load|execute|connect|frame|apply)"
    r"|net::ERR_|third-party cookie|Permissions policy|was preloaded using link preload|downloadable font",
    re.I,
)


def app_console_errors(page: PageEvidence) -> list[str]:
    return [e for e in page.console_errors if not BROWSER_NOISE.search(e)]


def lerp(value: float, good: float, poor: float) -> float:
    """1.0 at/better than `good`, 0.0 at/worse than `poor`, linear in between.
    Works for both "lower is better" (good < poor) and "higher is better"."""
    if good == poor:
        return 1.0 if value == good else 0.0
    t = (value - good) / (poor - good)
    return max(0.0, min(1.0, 1.0 - t))


def median(values: list[float]) -> float | None:
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


_MONTHS = {
    m: i + 1
    for i, names in enumerate([
        ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",),
        ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"),
        ("oct", "october"), ("nov", "november"), ("dec", "december"),
    ])
    for m in names
}
_MONTH_RX = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_PATTERNS = [
    (re.compile(r"\b(20\d\d)-(\d\d)-(\d\d)\b"), lambda m: (int(m[1]), int(m[2]), int(m[3]))),
    (re.compile(rf"\b{_MONTH_RX}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(20\d\d)\b", re.I),
     lambda m: (int(m[3]), _MONTHS[m[1].lower()], int(m[2]))),
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_RX}\.?,?\s+(20\d\d)\b", re.I),
     lambda m: (int(m[3]), _MONTHS[m[2].lower()], int(m[1]))),
    (re.compile(rf"\b{_MONTH_RX}\.?\s+(20\d\d)\b", re.I), lambda m: (int(m[2]), _MONTHS[m[1].lower()], 1)),
]


def find_dates(text: str, today: date | None = None) -> list[date]:
    """All plausible dates in `text` that are not in the future."""
    today = today or datetime.now().date()
    found = set()
    for rx, build in _DATE_PATTERNS:
        for m in rx.finditer(text or ""):
            try:
                d = date(*build(m))
            except (ValueError, KeyError):
                continue
            if date(2000, 1, 1) <= d <= today:
                found.add(d)
    return sorted(found)


def text_near(text: str, keyword_rx: str, window: int = 200) -> str:
    """Concatenated text windows around keyword matches (e.g. "last updated")."""
    chunks = []
    for m in re.finditer(keyword_rx, text or "", re.I):
        chunks.append(text[max(0, m.start() - 20): m.end() + window])
    return "\n".join(chunks)


def pages(ev: Evidence, *kinds: str) -> list[PageEvidence]:
    return [p for p in ev.ok_pages() if not kinds or p.kind in kinds]


def discovered(ev: Evidence, kind: str) -> str | None:
    return ev.discovered.get(kind)


def any_link(ev: Evidence, href_rx: str = "", text_rx: str = "") -> dict[str, str] | None:
    for link in ev.all_links():
        if href_rx and re.search(href_rx, link["href"], re.I):
            return link
        if text_rx and re.search(text_rx, link.get("text", ""), re.I):
            return link
    return None
