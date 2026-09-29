"""REL — Reliability & Support."""

from __future__ import annotations

from ..models import Evidence, failed, passed, scored
from .helpers import any_link
from .registry import check


@check("REL-01", "REL", "Public status page", 25)
def status_page(ev: Evidence):
    url = ev.discovered.get("status")
    if url:
        return passed(f"Status page found: {url}.", [url])
    return failed("No public status page found (links, status.<domain>, /status).")


@check(
    "REL-02", "REL", "Uptime history and incident transparency", 20, method="ai",
    rubric=(
        "On the status page: is historical uptime shown (e.g. 90-day bars / percentages) and are past incidents "
        "published with explanations? pass = both; partial = only current status or only one of them; "
        "fail/not_found = no status page or no history."
    ),
    pages=("status",),
)
def uptime_history(ev: Evidence):
    return None


@check(
    "REL-03", "REL", "Uptime SLA commitment (≥ 99.9%)", 20, method="ai",
    rubric=(
        "Does the vendor publicly commit to an uptime SLA? pass = SLA of 99.9% or higher stated (any plan); "
        "partial = SLA mentioned without a figure, or below 99.9%, or only on request; fail/not_found = no SLA."
    ),
    pages=("pricing", "terms", "security", "status", "docs"),
)
def sla(ev: Evidence):
    return None


@check(
    "REL-04", "REL", "Support channels (chat, email, phone, 24×7)", 20, method="auto+ai",
    rubric=(
        "Which support channels are offered (live chat, email/ticket, phone, dedicated CSM) and hours "
        "(24/7?)? pass = 3+ channels or 24/7 support; partial = 1-2 channels; fail = no support channel visible."
    ),
    pages=("contact", "docs", "pricing"),
)
def support_channels(ev: Evidence):
    chat = sorted(w[5:] for w in ev.all_widgets() if w.startswith("chat:"))
    contact = ev.discovered.get("contact") or ev.discovered.get("docs")
    email = any_link(ev, href_rx=r"^mailto:(support|help|contact|hello|cs|customer|contato|suporte|atendimento|ajuda|soporte|contacto)")
    phone = any_link(ev, href_rx=r"^tel:|wa\.me/|api\.whatsapp\.com|whatsapp\.com/send")
    factor = (0.4 if chat else 0) + (0.3 if contact else 0) + (0.3 if (email or phone) else 0)
    detail = (
        f"Chat widget: {', '.join(chat) or 'none'}; contact/support page: {contact or 'none'}; "
        f"email/phone/WhatsApp link: {'yes' if email or phone else 'none'}."
    )
    return scored(factor, detail)


@check("REL-05", "REL", "Community / forum", 15)
def community(ev: Evidence):
    url = ev.discovered.get("community") or (any_link(ev, href_rx=r"discord\.(gg|com/invite)|community\.|forum", text_rx=r"^(community|forum)$") or {}).get("href")
    if url:
        return passed(f"Community found: {url}.", [url])
    return failed("No community or forum found.")

