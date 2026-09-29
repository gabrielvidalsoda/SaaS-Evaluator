"""Page discovery: finds the pages that matter for the evaluation (pricing, trust,
privacy, status, docs, API, changelog…) — first from links on the pages already
visited (URL + link-text heuristics), then by probing a few conventional locations
(`status.<domain>`, `/pricing`…) for kinds still missing.

Probes are plain GETs, sequential-ish (small semaphore). A random path is fetched
once per origin to detect "soft 404s" (SPAs answering 200 for any path), so those
don't count as discovered pages."""

from __future__ import annotations

import asyncio
import re
import secrets
from typing import Any
from urllib.parse import urlparse

import httpx

from .http_probe import HEADERS, TIMEOUT
from .urls import registered_domain, strip_fragment

# kind -> (URL regex, link-text regex). A URL match weighs more than a text match.
# kind -> (URL regex, link-text regex). A URL match weighs more than a text match.
# English + Portuguese + Spanish wording (URLs are matched without accents).
KIND_RULES: dict[str, tuple[str, str]] = {
    "pricing": (
        r"/(pricing|plans|price|precos|planos|precios|preco)(/|$|\?)",
        r"^(pricing|plans|plans (&|and) pricing|prices?|preços|precos|planos|planos e preços|precios|planes)$",
    ),
    "security": (
        r"(^https?://(trust|security)\.)|/(security|trust|trust-center|compliance|seguranca|seguridad|confianca)(/|$|\?)",
        r"^(security|trust|trust center|trust & security|security & compliance|compliance|segurança|seguridad|conformidade)$",
    ),
    "privacy": (r"privacy|privacidade|privacidad", r"privacy|privacidade|privacidad"),
    "terms": (
        r"/(terms|tos|legal/terms|terms-of-service|terms-of-use|terms-and-conditions|termos|termos-de-uso|terminos|condiciones)(/|$|\?|\.)",
        r"^(terms|termos|términos|terminos|condições de uso|condiciones)",
    ),
    "dpa": (r"/(dpa|data-processing)", r"\bdpa\b|data processing|tratamento de dados|encarregado"),
    "status": (
        r"(^https?://status\.)|/status(/|$)|statuspage\.io|status\.io|instatus\.com|betteruptime\.com|status\.[a-z]+\.[a-z]+",
        r"^(status|system status|service status|platform status|status do sistema|estado del servicio)$",
    ),
    "docs": (
        r"(^https?://(docs|help|support|learn|guide|ajuda|suporte)\.)|/(docs|help|help-center|support|kb|knowledge-base|guides|learn|ajuda|suporte|central-de-ajuda|documentacao|ayuda|soporte)(/|$|\?)",
        r"^(docs|documentation|help|help center|help centre|knowledge base|support|guides|support center|ajuda|central de ajuda|suporte|documentação|ayuda|centro de ayuda|soporte)$",
    ),
    "api": (
        r"(^https?://(developers?|api-docs|dev)\.)|/(api|developers?|reference|api-docs|api-reference|desenvolvedores)(/|$|\?)",
        r"^(api|apis|developers?|api docs|api reference|developer docs|developer portal|desenvolvedores|desarrolladores)$",
    ),
    "changelog": (
        r"changelog|release-notes|releases|whats-new|what-s-new|/updates(/|$)|novidades|atualizacoes|novedades",
        r"^(changelog|release notes|what's new|whats new|product updates|updates|novidades|atualizações|novedades)$",
    ),
    "roadmap": (r"roadmap|roteiro", r"roadmap|roteiro"),
    "integrations": (
        r"/(integrations?|marketplace|apps|app-directory|app-marketplace|integracoes|integraciones)(/|$|\?)",
        r"^(integrations?|marketplace|app directory|apps|app marketplace|integrações|integraciones)$",
    ),
    "community": (
        r"(^https?://(community|forum|forums|comunidade)\.)|/(community|forum|forums|comunidade|comunidad)(/|$)|discord\.(gg|com/invite)",
        r"^(community|forum|forums|discord|join our community|comunidade|comunidad)$",
    ),
    "contact": (
        r"/(contact|contact-us|contact-sales|support/contact|contato|fale-conosco|contacto)(/|$|\?)",
        r"^(contact( us| sales| support)?|contato|fale conosco|fale com (a gente|nossa equipe)|contacto|contáctanos)$",
    ),
    "login": (
        r"/(login|log-in|signin|sign-in|auth/login|session/new|users/sign_in|entrar)(/|$|\?)",
        r"^(log ?in|sign ?in|entrar|acessar|iniciar sesión)$",
    ),
    "signup": (
        r"/(signup|sign-up|register|join|get-started|start|trial|free-trial|users/sign_up|cadastro|cadastre-se|criar-conta|registro)(/|$|\?)",
        r"^(sign ?up|get started|start (for )?free|start (your )?free trial|try (it )?(for )?free|create (an )?account|register"
        r"|cadastre-se|cadastrar|criar conta|comece grátis|teste grátis|experimente grátis|regístrate|prueba gratis)",
    ),
}

# Hosts that are third-party but legitimately host a vendor's page of this kind.
_THIRD_PARTY_OK = {
    "status": ("statuspage.io", "status.io", "instatus.com", "betteruptime.com", "atlassian.net", "hund.io"),
    "community": ("discord.gg", "discord.com", "discourse.group", "circle.so", "slack.com"),
    "docs": ("gitbook.io", "readme.io", "zendesk.com", "intercom.help", "helpscoutdocs.com", "freshdesk.com"),
    "api": ("readme.io", "postman.com", "stoplight.io", "swaggerhub.com"),
    "roadmap": ("canny.io", "productboard.com", "featurebase.app", "trello.com", "github.com"),
    "changelog": ("headwayapp.co", "beamer.com", "canny.io", "featurebase.app", "github.com"),
}

# Probed (in order) for kinds still missing after link classification.
PROBE_TEMPLATES: dict[str, list[str]] = {
    "status": ["https://status.{org}", "https://{www}/status"],
    "security": ["https://trust.{org}", "https://{www}/security", "https://{www}/trust"],
    "pricing": ["https://{www}/pricing", "https://{www}/precos", "https://{www}/planos"],
    "privacy": ["https://{www}/privacy", "https://{www}/privacy-policy", "https://{www}/legal/privacy", "https://{www}/privacidade"],
    "terms": ["https://{www}/terms", "https://{www}/legal/terms", "https://{www}/terms-of-service", "https://{www}/termos"],
    "docs": ["https://docs.{org}", "https://help.{org}", "https://support.{org}", "https://{www}/docs", "https://{www}/help"],
    "api": ["https://developers.{org}", "https://developer.{org}", "https://{www}/developers", "https://{www}/api", "https://docs.{org}/api"],
    "changelog": ["https://{www}/changelog", "https://{www}/release-notes", "https://{www}/whats-new"],
    "roadmap": ["https://{www}/roadmap"],
    "integrations": ["https://{www}/integrations", "https://{www}/marketplace"],
    "community": ["https://community.{org}", "https://{www}/community", "https://forum.{org}"],
    "dpa": ["https://{www}/dpa", "https://{www}/legal/dpa"],
}

# Order in which discovered kinds are visited in the browser (crawl budget).
CRAWL_PRIORITY = [
    "login", "pricing", "security", "privacy", "status", "docs", "api", "signup",
    "changelog", "terms", "integrations", "dpa", "roadmap", "community", "contact",
]


def classify_links(links: list[dict[str, str]], org_domain: str) -> dict[str, str]:
    """Best link per kind, from (href, text) pairs."""
    best: dict[str, tuple[int, str]] = {}
    for link in links:
        href = strip_fragment(link.get("href") or "")
        if not href.startswith(("http://", "https://")):
            continue
        text = (link.get("text") or "").strip().lower()
        domain = registered_domain(href)
        for kind, (url_rx, text_rx) in KIND_RULES.items():
            first_party = domain == org_domain
            allowed_3p = any(h in href for h in _THIRD_PARTY_OK.get(kind, ()))
            if not (first_party or allowed_3p):
                continue
            score = 0
            if re.search(url_rx, href, re.I):
                score += 2
            if text and len(text) <= 40 and re.search(text_rx, text, re.I):
                score += 1
            if score == 0:
                continue
            # Prefer stronger matches, then shorter URLs (the section root, not a sub-article).
            candidate = (score * 1000 - len(href), href)
            if kind not in best or candidate > best[kind]:
                best[kind] = candidate
    return {kind: href for kind, (_, href) in best.items()}


class _Prober:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self.client = client
        self.sem = asyncio.Semaphore(3)
        self._soft404: dict[str, dict[str, Any] | None] = {}

    async def _get(self, url: str) -> httpx.Response | None:
        async with self.sem:
            try:
                resp = await self.client.get(url)
            except httpx.HTTPError:
                return None
            await asyncio.sleep(0.2)
            return resp

    async def _soft404_signature(self, origin: str) -> dict[str, Any] | None:
        if origin not in self._soft404:
            resp = await self._get(f"{origin}/saas-eval-{secrets.token_hex(4)}-not-found")
            self._soft404[origin] = (
                {"final": str(resp.url), "title": _title(resp.text)} if resp is not None and resp.status_code == 200 else None
            )
        return self._soft404[origin]

    async def exists(self, url: str) -> str | None:
        """Final URL if `url` is a real HTML page, else None."""
        resp = await self._get(url)
        if resp is None or resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
            return None
        final = str(resp.url)
        parsed = urlparse(final)
        requested_path = urlparse(url).path
        # Redirected to the site root = soft 404 (unless the root itself was asked for).
        if parsed.path in ("", "/") and requested_path not in ("", "/"):
            return None
        sig = await self._soft404_signature(f"{parsed.scheme}://{parsed.netloc}")
        if sig and (sig["final"] == final or (sig["title"] and sig["title"] == _title(resp.text))):
            return None
        return final


def _title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    return m.group(1).strip() if m else ""


async def probe_missing(kinds: list[str], org_domain: str, www_host: str) -> dict[str, str]:
    """Tries conventional locations for each missing kind; returns kind -> url found."""
    found: dict[str, str] = {}
    async with httpx.AsyncClient(headers=HEADERS, timeout=TIMEOUT, follow_redirects=True) as client:
        prober = _Prober(client)

        async def probe_kind(kind: str) -> None:
            for template in PROBE_TEMPLATES.get(kind, []):
                url = template.format(org=org_domain, www=www_host)
                final = await prober.exists(url)
                if final:
                    found[kind] = final
                    return

        await asyncio.gather(*(probe_kind(k) for k in kinds if k in PROBE_TEMPLATES))
    return found
