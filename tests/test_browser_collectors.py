"""Browser collectors against local fixture pages (no network, no AI): a "good"
site and a deliberately "bad" one must produce clearly different evidence and
check results."""

from __future__ import annotations

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from saas_eval.checks import registry
from saas_eval.collectors.a11y import collect_a11y
from saas_eval.collectors.browser import BrowserCollector
from saas_eval.models import Evidence

FIXTURES = Path(__file__).parent / "fixtures"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output clean
        pass


@pytest.fixture(scope="module")
def server():
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


async def _collect(url: str, tmp_path: Path) -> Evidence:
    ev = Evidence(target_url=url, host="127.0.0.1", org_domain="127.0.0.1")
    collector = BrowserCollector(headless=True, org_domain="127.0.0.1")
    await collector.start()
    try:
        ev.pages.append(await collector.visit(url, "target", tmp_path / "shot.jpg"))
        ev.a11y = await collect_a11y(collector, url, [], tmp_path)
    finally:
        await collector.close()
    return ev


def _run(check_id: str, ev: Evidence):
    registry._load_all()
    return registry.evaluate(registry.REGISTRY[check_id], ev)


async def test_good_site(server, tmp_path):
    ev = await _collect(f"{server}/good_site/index.html", tmp_path)
    page = ev.target
    assert page.ok and page.title.startswith("GoodApp")
    assert page.consent_banner
    assert page.has_viewport_meta and page.lang == "en"
    assert not page.page_errors and not page.console_errors
    assert _run("UX-04", ev).status == "pass"
    assert _run("UX-05", ev).factor == 1.0  # labelled, autocomplete, password reveal
    assert _run("DATA-06", ev).status == "pass"
    assert _run("A11Y-02", ev).status == "pass"
    assert _run("A11Y-04", ev).status == "pass"
    assert _run("A11Y-01", ev).factor >= 0.8
    assert (tmp_path / "shot.jpg").exists()


async def test_bad_site(server, tmp_path):
    ev = await _collect(f"{server}/bad_site/index.html", tmp_path)
    page = ev.target
    assert page.console_errors and page.page_errors
    assert not page.consent_banner and not page.has_viewport_meta
    assert _run("UX-04", ev).factor < 1.0
    assert _run("UX-05", ev).factor < 0.6
    assert _run("DATA-06", ev).status == "fail"
    assert _run("A11Y-02", ev).factor < 0.5  # overflow at mobile/tablet + no viewport meta
    assert _run("A11Y-01", ev).factor < 0.8
    violations = {v["id"] for v in next(iter(ev.a11y["axe"].values()))}
    assert {"image-alt", "button-name"} <= violations
