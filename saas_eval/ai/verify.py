"""Anti-hallucination guardrail for AI verdicts.

A verdict that *awards* points (pass / partial) must cite at least one verbatim
quote that really exists in the text collected from the cited page (or, failing an
exact URL match, any collected page). Negative verdicts (fail / not_found) need no
quote — absence can't be quoted, and they award no points."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..models import Evidence

MIN_QUOTE_CHARS = 4
_QUOTE_MAP = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-", " ": " "})


class Quote(BaseModel):
    url: str = ""
    quote: str


class Verdict(BaseModel):
    check_id: str
    verdict: str = Field(pattern=r"^(pass|partial|fail|not_found)$")
    factor: float | None = Field(default=None, ge=0.0, le=1.0)
    reason: str = ""
    quotes: list[Quote] = Field(default_factory=list)

    @field_validator("check_id")
    @classmethod
    def upper(cls, v: str) -> str:
        return v.strip().upper()


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(_QUOTE_MAP).lower()
    text = text.replace("…", "...")
    return re.sub(r"\s+", " ", text).strip()


def _same_url(a: str, b: str) -> bool:
    strip = lambda u: re.sub(r"^https?://(www\.)?", "", (u or "").split("#")[0]).rstrip("/")  # noqa: E731
    return strip(a) == strip(b)


def verify(verdict: Verdict, ev: Evidence) -> tuple[bool, str, list[dict[str, Any]]]:
    """Returns (accepted, rejection reason, quotes annotated with `verified`)."""
    annotated = []
    corpus = [(p.final_url or p.url, p.url, normalize(p.title + "\n" + p.text)) for p in ev.ok_pages()]
    for q in verdict.quotes:
        needle = normalize(q.quote).strip(" .\"'")
        found = False
        if len(needle) >= MIN_QUOTE_CHARS:
            same_page = [text for final, url, text in corpus if _same_url(q.url, final) or _same_url(q.url, url)]
            found = any(needle in text for text in same_page) or any(needle in text for _, _, text in corpus)
        annotated.append({"url": q.url, "quote": q.quote[:300], "verified": found})

    if verdict.verdict in ("fail", "not_found"):
        return True, "", annotated
    if not verdict.quotes:
        return False, "a pass/partial verdict must include at least one verbatim quote", annotated
    if not any(q["verified"] for q in annotated):
        return False, "none of the quotes was found verbatim in the collected page text", annotated
    return True, "", [q for q in annotated if q["verified"]]
