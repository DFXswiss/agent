"""U-DBL-login: headless-browser smoke of the auth pages (Playwright for Python, installed with the ``login`` extra).

The whole subscription-login chain runs hermetically: the login-CLI double in a PTY prints the authorize URL (captured
through BROWSER), headless Chromium enters the e-mail, the legacy IMAP poll finds the magic link in the Junk folder of
the mail double, Chromium opens it (the page's script posts the fragment), clicks "Authorize" and reads the code, which
is typed into the PTY; the CLI prints the token. Chromium trusts the auth pages only through the SPKI pin of the
runtime CA's server key. Without the ``login`` extra the same chain is covered on the HTTP level
(`test_cli.test_setup_token_relay_in_a_pty`).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_doubles.login_cli import codes
from agent_doubles.login_cli.authpage import Scenario
from agent_doubles.login_cli.cli import PROMPT, authorize_url

from .conftest import USER, Login, Terminal

sync_api = pytest.importorskip("playwright.sync_api", reason="Playwright for Python comes with the login extra")


def test_mailbox_automation_chain_in_headless_chromium(
    start_login: Callable[..., Login], terminal: Callable[..., Terminal], tmp_path: Path
) -> None:
    login = start_login(
        scenario=Scenario(junk=True, expect=("mail_sent", "signed_in", "authorized", "code_page", "token"))
    )
    assert login.ca is not None
    term = terminal("setup-token", "--auth-url", login.auth.page_url, "--ca-file", str(login.ca.ca_file), "--seed", "b")
    term.read_until_shown(PROMPT)
    url = (tmp_path / "url.txt").read_text()
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=[f"--ignore-certificate-errors-spki-list={login.ca.spki_sha256()}"]
        )
        try:
            page = browser.new_page()
            since = login.auth.clock()
            page.goto(url)
            assert page.locator("body").get_attribute("data-page") == "email"
            page.fill("#email", USER)
            page.click("#continue")
            page.wait_for_selector("body[data-page='check-email']")
            link = None
            deadline = time.monotonic() + 10
            while link is None and time.monotonic() < deadline:
                link = login.magic_link(since)
                if link is None:
                    time.sleep(0.2)
            assert link is not None
            page.goto(link)
            page.wait_for_selector("body[data-page='authorize']")
            page.click("#authorize")
            page.wait_for_selector("body[data-page='code']")
            pasted = page.text_content("#authorization-code")
        finally:
            browser.close()
    assert pasted is not None
    assert login.mail.store.counts()[USER] == {"INBOX": 0, "Junk": 1}
    term.type(pasted)
    assert term.finish() == 0
    assert codes.access_token(codes.DEFAULT_SEED, 1) in "".join(term.lines)
    state = login.auth.state()
    assert (state["ok"], state["failures"]) == (True, [])


def test_unexpected_page_state_is_visible_to_a_browser(start_login: Callable[..., Login]) -> None:
    login = start_login(scenario=Scenario(fault="email_page"))
    assert login.ca is not None
    with sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True, args=[f"--ignore-certificate-errors-spki-list={login.ca.spki_sha256()}"]
        )
        try:
            page = browser.new_page()
            page.goto(authorize_url(login.auth.page_url, "b"))
            page.fill("#email", USER)
            page.click("#continue")
            page.wait_for_selector("body[data-page='challenge']")
        finally:
            browser.close()
    assert login.mail.store.counts()[USER] == {"INBOX": 0, "Junk": 0}
