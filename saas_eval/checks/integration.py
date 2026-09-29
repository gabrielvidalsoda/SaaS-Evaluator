"""INT — Integration & Extensibility."""

from __future__ import annotations

from ..models import Evidence, failed, passed, scored
from .helpers import any_link
from .registry import check


@check("INT-01", "INT", "Public API documentation", 30)
def api_docs(ev: Evidence):
    url = ev.discovered.get("api")
    if url:
        return passed(f"API documentation found: {url}.", [url])
    return failed("No public API documentation found (developers.*, /api, /developers, /reference).")


@check(
    "INT-02", "INT", "API standards (OpenAPI/GraphQL, versioning, auth)", 20, method="ai",
    rubric=(
        "Is the API well-specified: REST with an OpenAPI/Swagger spec or GraphQL schema, documented "
        "authentication (API keys/OAuth), versioning and rate limits? pass = spec + auth + versioning; "
        "partial = documented but missing some; fail/not_found = no public API."
    ),
    pages=("api", "docs"),
)
def api_standards(ev: Evidence):
    return None


@check(
    "INT-03", "INT", "Webhooks / event notifications", 15, method="ai",
    rubric="Does the product offer webhooks or an event subscription mechanism? pass = yes, documented; fail/not_found = no.",
    pages=("api", "docs", "integrations"),
)
def webhooks(ev: Evidence):
    return None


@check(
    "INT-04", "INT", "Integrations marketplace / connectors", 20, method="auto+ai",
    rubric=(
        "How rich is the integration ecosystem (native integrations list/marketplace, Zapier/Make support)? "
        "pass = marketplace or 20+ native integrations; partial = a handful; fail = none."
    ),
    pages=("integrations", "home", "target"),
)
def integrations(ev: Evidence):
    page = ev.discovered.get("integrations")
    automation = any_link(ev, href_rx=r"zapier\.com|make\.com|integromat|n8n\.io|workato|tray\.io")
    factor = (0.6 if page else 0.0) + (0.4 if automation else 0.0)
    return scored(
        factor,
        f"Integrations page: {page or 'not found'}; automation platform link: {automation['href'] if automation else 'none'}.",
    )


@check(
    "INT-05", "INT", "User provisioning & configurability (SCIM, roles, custom fields)", 15, method="ai",
    rubric=(
        "Does the product support SCIM/automated user provisioning, role-based access control with custom roles, "
        "and custom fields/configuration without code? pass = SCIM + RBAC; partial = RBAC or custom fields only; "
        "fail/not_found = none mentioned."
    ),
    pages=("pricing", "security", "docs"),
)
def provisioning(ev: Evidence):
    return None
