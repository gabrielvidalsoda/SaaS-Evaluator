"""URL helpers: normalization, registered-domain logic (first- vs third-party)."""

from __future__ import annotations

from urllib.parse import urlparse, urlunparse

import tldextract

# Offline extractor: uses the public-suffix snapshot bundled with tldextract, so it
# never makes a network call at runtime.
_extract = tldextract.TLDExtract(suffix_list_urls=())


def normalize_target(url: str) -> str:
    url = url.strip()
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"Not a valid http(s) URL: {url!r}")
    return urlunparse(parsed._replace(fragment="", path=parsed.path or "/"))


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def registered_domain(url_or_host: str) -> str:
    host = host_of(url_or_host) if "://" in url_or_host else url_or_host.lower()
    ext = _extract(host)
    return ext.top_domain_under_public_suffix or host


def is_first_party(url: str, org_domain: str) -> bool:
    return registered_domain(url) == org_domain


def strip_fragment(url: str) -> str:
    return urlunparse(urlparse(url)._replace(fragment=""))
