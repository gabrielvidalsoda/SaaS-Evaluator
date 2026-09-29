"""Core data model: pillars, evidence collected from the target, check results and
the final evaluation. Evidence is written to evidence.json (write-only); results
are written to scores.json, which is what `report`/`compare` read back."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ── pillars ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Pillar:
    code: str
    name: str
    description: str
    opt_in: bool = False  # opt-in pillars only run when explicitly requested (e.g. --a11y)


PILLARS: dict[str, Pillar] = {
    p.code: p
    for p in [
        Pillar("SEC", "Security & Compliance", "Transport security, headers, cookies, exposure, attestations."),
        Pillar("UX", "Usability & User Experience", "How easy and pleasant the product is to use."),
        Pillar("PERF", "Performance & Scalability", "External performance and scalability signals (no load testing)."),
        Pillar("REL", "Reliability & Support", "Status transparency, SLA, support channels, community."),
        Pillar("INT", "Integration & Extensibility", "APIs, webhooks, integrations, provisioning."),
        Pillar("DATA", "Data Governance & Portability", "Privacy, DPA, residency, export, backup/DR."),
        Pillar("TRN", "Transparency, Cost & Innovation", "Pricing clarity, trial, release cadence, roadmap."),
        Pillar("A11Y", "Accessibility & Multi-device", "WCAG checks, responsive layout, tap targets, keyboard.", opt_in=True),
    ]
}


def active_pillars(a11y: bool) -> list[str]:
    return [code for code, p in PILLARS.items() if not p.opt_in or (code == "A11Y" and a11y)]


# ── evidence ─────────────────────────────────────────────────────────────────


@dataclass
class PageEvidence:
    url: str
    kind: str  # target | home | pricing | security | privacy | ...
    final_url: str = ""
    status: int | None = None
    title: str = ""
    text: str = ""
    lang: str = ""
    has_viewport_meta: bool = False
    links: list[dict[str, str]] = field(default_factory=list)
    vitals: dict[str, Any] = field(default_factory=dict)
    next_hop_protocol: str = ""
    request_count: int = 0
    transfer_bytes: int = 0
    console_errors: list[str] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)  # "<status> <url>"
    mixed_content: list[str] = field(default_factory=list)
    script_origins: list[str] = field(default_factory=list)
    static_assets: dict[str, int] = field(default_factory=dict)  # {"total": n, "long_cache": n}
    widgets: list[str] = field(default_factory=list)
    consent_banner: bool = False
    forms: dict[str, Any] = field(default_factory=dict)
    buttons: list[str] = field(default_factory=list)
    screenshot: str | None = None
    error: str | None = None
    # False = fetched as text only (outside the browser budget): usable for content
    # checks and the AI judge, but has no vitals / console / resource data.
    rendered: bool = True

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and self.status < 400


@dataclass
class Evidence:
    target_url: str
    host: str
    org_domain: str
    http: dict[str, Any] = field(default_factory=dict)
    tls: dict[str, Any] = field(default_factory=dict)
    dns: dict[str, Any] = field(default_factory=dict)
    pages: list[PageEvidence] = field(default_factory=list)
    discovered: dict[str, str] = field(default_factory=dict)  # kind -> url
    link_checks: dict[str, Any] = field(default_factory=dict)
    cookies: list[dict[str, Any]] = field(default_factory=list)
    a11y: dict[str, Any] | None = None
    login: dict[str, Any] | None = None
    ai_enabled: bool = False
    ai_verdicts: dict[str, dict[str, Any]] = field(default_factory=dict)  # check id -> verdict

    def page(self, kind: str) -> PageEvidence | None:
        for p in self.pages:
            if p.kind == kind and p.ok:
                return p
        # The discovered URL may be a page already collected under another kind —
        # e.g. pricing as a section of the home page ("/#precos").
        url = self.discovered.get(kind)
        if url:
            norm = lambda u: (u or "").split("#")[0].rstrip("/")  # noqa: E731
            for p in self.pages:
                if p.ok and norm(url) in (norm(p.url), norm(p.final_url)):
                    return p
        return None

    @property
    def target(self) -> PageEvidence | None:
        return next((p for p in self.pages if p.kind == "target"), None)

    def ok_pages(self) -> list[PageEvidence]:
        return [p for p in self.pages if p.ok]

    def rendered_pages(self) -> list[PageEvidence]:
        """Pages loaded in the browser (the ones with vitals, console and resource data)."""
        return [p for p in self.pages if p.ok and p.rendered]

    def all_links(self) -> list[dict[str, str]]:
        seen, out = set(), []
        for p in self.pages:
            for link in p.links:
                if link["href"] not in seen:
                    seen.add(link["href"])
                    out.append(link)
        return out

    def all_widgets(self) -> set[str]:
        return {w for p in self.pages for w in p.widgets}


# ── results ──────────────────────────────────────────────────────────────────

Status = Literal["pass", "partial", "fail", "na", "not_evaluated", "error"]
Method = Literal["auto", "ai", "auto+ai"]

EVALUATED = {"pass", "partial", "fail"}


@dataclass
class Outcome:
    """What a check function returns; the registry turns it into a CheckResult."""

    status: Status
    factor: float = 0.0
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    source: str = "auto"


def passed(detail: str, evidence: list[str] | None = None) -> Outcome:
    return Outcome("pass", 1.0, detail, evidence or [])


def failed(detail: str, evidence: list[str] | None = None) -> Outcome:
    return Outcome("fail", 0.0, detail, evidence or [])


def scored(factor: float, detail: str, evidence: list[str] | None = None) -> Outcome:
    """Continuous result: status derived from the factor."""
    factor = max(0.0, min(1.0, factor))
    status: Status = "pass" if factor >= 0.999 else "fail" if factor <= 0.001 else "partial"
    return Outcome(status, factor, detail, evidence or [])


def na(detail: str) -> Outcome:
    return Outcome("na", 0.0, detail)


def not_evaluated(detail: str) -> Outcome:
    return Outcome("not_evaluated", 0.0, detail)


@dataclass
class CheckResult:
    id: str
    pillar: str
    title: str
    method: str
    max_points: float
    status: str
    factor: float
    earned: float
    detail: str
    evidence: list[str]
    source: str
    critical: bool = False


@dataclass
class PillarScore:
    code: str
    name: str
    weight: float
    score: float | None
    coverage: float
    confidence: str
    earned: float
    max_evaluated: float
    max_applicable: float
    capped: bool
    checks: list[CheckResult]


@dataclass
class Evaluation:
    overall: float | None
    grade: str
    coverage: float
    confidence: str
    profile: str
    pillars: list[PillarScore]
    dealbreakers: list[str]
