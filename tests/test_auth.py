"""Scripted login (two-step form), token detection, safe in-app link selection and
credentials loading — against a local fixture app (no network, no AI)."""

from __future__ import annotations

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.async_api import async_playwright

from saas_eval.collectors.auth import app_links, scripted_login, token_keys_in_local_storage
from saas_eval.credentials import Credentials, CredentialsError, load_credentials_file

FIXTURES = Path(__file__).parent / "fixtures"


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(FIXTURES)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


async def _login(url: str, creds: Credentials):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        info = await scripted_login(context, url, creds)
        state = await context.storage_state()
        await browser.close()
    return info, state


async def test_two_step_scripted_login_succeeds(server):
    info, state = await _login(f"{server}/app_site/login.html", Credentials("qa@fixture.test", "s3cret!"))
    assert info["success"], info
    assert info["final_url"].endswith("/app_site/app.html")
    assert any(c["name"] == "app_session" for c in state["cookies"])
    assert token_keys_in_local_storage(state, "127.0.0.1") == ["access_token"]


async def test_wrong_password_is_reported(server):
    info, _ = await _login(f"{server}/app_site/login.html", Credentials("qa@fixture.test", "wrong"))
    assert not info["success"]
    assert "Invalid credentials" in info["note"]


def test_app_links_are_safe_same_host_and_deduplicated():
    links = [
        {"href": "https://app.acme.com/dashboard", "text": "Dashboard"},
        {"href": "https://app.acme.com/projects/123", "text": "Project 123"},
        {"href": "https://app.acme.com/projects/456", "text": "Project 456"},  # same pattern as 123
        {"href": "https://app.acme.com/settings/delete-account", "text": "Delete account"},
        {"href": "https://app.acme.com/logout", "text": "Sair"},
        {"href": "https://app.acme.com/reports", "text": "Sign out"},
        {"href": "https://app.acme.com/files/report.pdf", "text": "Report"},
        {"href": "https://acme.com/pricing", "text": "Pricing"},  # other host
    ]
    seen: set[str] = set()
    assert app_links(links, "app.acme.com", seen) == [
        "https://app.acme.com/dashboard",
        "https://app.acme.com/projects/123",
    ]


def test_credentials_file(tmp_path):
    f = tmp_path / "c.yaml"
    f.write_text("login: me@x.com\npass: 'p@ss:word'\n", encoding="utf-8")
    creds = load_credentials_file(f)
    assert creds.username == "me@x.com" and creds.password == "p@ss:word"
    assert "p@ss" not in repr(creds)
    (tmp_path / "bad.yaml").write_text("username: only-user\n", encoding="utf-8")
    with pytest.raises(CredentialsError):
        load_credentials_file(tmp_path / "bad.yaml")
