"""U-RUN-login (automation): `claude.login` mode `automatic` (05-runner.md section 11.3) with headless Chromium
(Playwright for Python of the `login` extra, browsers under `PLAYWRIGHT_BROWSERS_PATH`), the auth-page double, the
mail double (magic link in the Junk folder) and the login-CLI double; every unexpected page state falls back to the
relay with `automation:"failed"`."""

from __future__ import annotations

import asyncio
import dataclasses
import importlib.util
import os
import ssl
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_cli.account_login import Mailbox
from agent_doubles.login_cli import codes
from agent_doubles.login_cli.authpage import Scenario
from agent_runner.credentials import cache
from agent_runner.login import automation
from agent_runner.login.automation import AutomationFailed
from agent_runner.requests.service import RequestClosed

from ..credentials.kit import ALICE
from .conftest import FAST, USER, Login, Run, cli_argv, code_for, mailbox_body, run_request

AUTOMATIC = {"mode": "automatic"}
LONG_EMAIL = "a" * 64 + "@" + "b" * 190
STAGES = ["browser_started", "email_entered", "magic_link_received", "authorized"]


@pytest.fixture(autouse=True)
def _playwright_installed() -> None:
    if importlib.util.find_spec("playwright") is None:
        pytest.fail("the login extra (Playwright for Python) must be installed for the RUN-13 suites")


def progress(bodies: list[dict[str, Any]]) -> list[str]:
    return [b["stage"] for b in bodies if b["step"] == "progress"]


def test_automation_signs_in_through_the_junk_folder_and_stores_the_subscription(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles(Scenario(junk=True, expect=("mail_sent", "signed_in", "authorized", "code_page", "token")))

    async def check(run: Run) -> None:
        await run.settle()
        bodies = run.bodies()
        assert progress(bodies) == STAGES
        assert bodies[-1] == {"step": "done", "email": USER}
        assert run.finals(run.root) == [False] * 4 + [True]
        async with cache.opened(run.env.context, ALICE, "claude_subscription") as body:
            assert dict(body) == {
                "token": codes.access_token(codes.DEFAULT_SEED, 1),
                "email": USER,
                "kind": "setup_token",
            }
        assert login.mail.store.counts()[USER] == {"INBOX": 0, "Junk": 1}
        state = login.auth.state()
        assert (state["ok"], state["failures"]) == (True, [])
        run.secrets_absent(
            codes.access_token(codes.DEFAULT_SEED, 1),
            "mailbox-password",
            codes.oauth_state(codes.DEFAULT_SEED),
            codes.auth_code(codes.DEFAULT_SEED, 1),
            codes.magic_token(codes.DEFAULT_SEED, 1),
        )
        assert not (run.tmp_dir / f"login-{run.root}" / "browser").exists()

    run_request(
        store_dsn,
        tmp_path,
        "claude.login",
        AUTOMATIC,
        check,
        argv=cli_argv(login),
        automation=True,
        records={"mailbox": mailbox_body(login)},
    )


FAULTS = {
    "email_page": (["browser_started"], "TimeoutError"),
    "no_mail": (["browser_started", "email_entered"], "mail_timeout"),
    "authorize_page": (["browser_started", "email_entered", "magic_link_received"], "TimeoutError"),
}


@pytest.mark.parametrize("fault", sorted(FAULTS))
def test_an_unexpected_page_falls_back_to_the_relay(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login], fault: str
) -> None:
    stages, reason = FAULTS[fault]
    login = doubles(Scenario(junk=True, fault=fault))

    async def check(run: Run) -> None:
        bodies = await run.wait_bodies(len(stages) + 1, seconds=60)
        assert progress(bodies) == stages
        assert bodies[-1] == {"step": "authorize", "url": bodies[-1]["url"], "automation": "failed"}
        assert f"automation_failed reason={reason}" in run.env.log()
        login.auth.set_scenario(Scenario())
        pasted = await asyncio.to_thread(code_for, login, bodies[-1]["url"])
        follow = await run.follow({"code": pasted})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]
        async with cache.opened(run.env.context, ALICE, "claude_subscription") as body:
            assert body["email"] is None

    opts = dataclasses.replace(FAST, page_timeout=1.5, mail_wait=1.0, mail_poll=0.2)
    run_request(
        store_dsn,
        tmp_path,
        "claude.login",
        AUTOMATIC,
        check,
        argv=cli_argv(login),
        automation=True,
        records={"mailbox": mailbox_body(login)},
        opts=opts,
    )


def _fallback_without_browser(store_dsn: str, tmp_path: Path, login: Login, reason: str, **kw: Any) -> None:
    async def check(run: Run) -> None:
        bodies = await run.wait_bodies(1)
        assert bodies == [{"step": "authorize", "url": bodies[0]["url"], "automation": "failed"}]
        assert f"automation_failed reason={reason}" in run.env.log()
        follow = await run.follow({"code": await asyncio.to_thread(code_for, login, bodies[0]["url"])})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]

    run_request(store_dsn, tmp_path, "claude.login", AUTOMATIC, check, argv=cli_argv(login), **kw)


def test_an_org_device_without_automation_uses_the_relay(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles()
    _fallback_without_browser(store_dsn, tmp_path, login, "disabled", automation=False)


def test_a_missing_login_extra_falls_back_to_the_relay(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    def missing() -> Callable[[], Any]:
        raise ImportError("playwright")

    login = doubles()
    opts = dataclasses.replace(FAST, playwright=missing)
    _fallback_without_browser(
        store_dsn,
        tmp_path,
        login,
        "extra_missing",
        automation=True,
        opts=opts,
        records={"mailbox": mailbox_body(login)},
    )


@pytest.mark.parametrize("records", [{}, {"mailbox": {"imap_host": "127.0.0.1"}}])
def test_automation_without_a_usable_mailbox_credential_ends_credential_missing(
    store_dsn: str, tmp_path: Path, records: dict[str, Any]
) -> None:
    async def check(run: Run) -> None:
        await run.settle()
        assert run.bodies() == [{"step": "failed", "code": "credential_missing"}]
        assert not run.tmp_dir.exists()

    run_request(store_dsn, tmp_path, "claude.login", AUTOMATIC, check, automation=True, records=records)


# --- units ---------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"imap_host": "h", "imap_port": True, "imap_user": "u", "imap_password": "p", "email": "e@x.test"},
        {"imap_host": "h", "imap_port": 0, "imap_user": "u", "imap_password": "p", "email": "e@x.test"},
        {"imap_host": "h", "imap_port": 993, "imap_user": "", "imap_password": "p", "email": "e@x.test"},
        {"imap_host": "h", "imap_port": 993, "imap_user": "u", "imap_password": "", "email": "e@x.test"},
        {"imap_host": "h", "imap_port": 993, "imap_user": "u", "imap_password": "p", "email": ""},
        {"imap_host": "", "imap_port": 993, "imap_user": "u", "imap_password": "p", "email": "e@x.test"},
        {"imap_host": "h/x", "imap_port": 993, "imap_user": "u", "imap_password": "p", "email": "e@x.test"},
        {"imap_host": "h", "imap_port": 993, "imap_user": "u", "imap_password": "p", "email": "no-at-sign"},
        {
            "imap_host": "h",
            "imap_port": 993,
            "imap_user": "u",
            "imap_password": "p",
            "email": "a" * 64 + "@" + "b" * 190,
        },
    ],
)
def test_a_malformed_mailbox_body_is_refused(body: dict[str, Any]) -> None:
    assert automation.mailbox_fields(body) is None


def test_mailbox_fields_build_the_legacy_mailbox_without_showing_the_password() -> None:
    body = {"imap_host": "h", "imap_port": 993, "imap_user": "u", "imap_password": "pw-x", "email": "e@x.test"}
    found = automation.mailbox_fields(body)
    assert found == (Mailbox(host="h", user="u", password="pw-x", port=993), "e@x.test")
    assert "pw-x" not in repr(found)


def test_the_magic_link_pattern_follows_the_authorize_origin() -> None:
    pattern = automation.magic_pattern("https://claude.example:8443/oauth/authorize?x=1")
    text = 'href="https://claude.example:8443/magic-link#tok_en:ab=="> https://evil.example/magic-link#x'
    assert pattern.findall(text) == ["https://claude.example:8443/magic-link#tok_en:ab=="]


def test_the_server_pin_is_only_taken_with_a_private_trust_file(
    monkeypatch: pytest.MonkeyPatch, doubles: Callable[..., Login]
) -> None:
    login = doubles()
    assert automation.server_pin(login.auth.page_url) == login.ca.spki_sha256()
    assert automation.server_pin("http://127.0.0.1:9/oauth/authorize") is None
    monkeypatch.delenv("SSL_CERT_FILE")
    assert automation.server_pin(login.auth.page_url) is None


def test_a_server_that_does_not_verify_is_not_pinned(
    monkeypatch: pytest.MonkeyPatch, doubles: Callable[..., Login], tmp_path: Path
) -> None:
    login = doubles()
    other = tmp_path / "other.pem"
    other.write_text(ssl.get_server_certificate(("127.0.0.1", login.mail.imaps.port)))  # type: ignore[union-attr]
    monkeypatch.setenv("SSL_CERT_FILE", str(other))
    with pytest.raises(ssl.SSLError):
        automation.server_pin(login.auth.page_url)


def test_imap_failures_are_retried_until_the_mail_wait_ends() -> None:
    calls: list[str] = []

    def broken(*_args: object, **_kw: object) -> object:
        calls.append("connect")
        raise ConnectionRefusedError

    opts = dataclasses.replace(FAST, imap_factory=broken, mail_poll=0.05)
    box = Mailbox(host="127.0.0.1", user="u", password="p", port=9)

    async def main() -> None:
        end = asyncio.get_running_loop().time() + 0.2
        with pytest.raises(AutomationFailed) as failed:
            await automation.magic_link(box, 0.0, automation.magic_pattern("https://x.example/"), opts, end)
        assert failed.value.reason == "mail_timeout"

    asyncio.run(main())
    assert len(calls) >= 2


class FakePage:
    def __init__(self, code: str | None, fail_at: str | None = None) -> None:
        self.code = code
        self.fail_at = fail_at

    async def goto(self, *_a: object, **_k: object) -> None:
        if self.fail_at == "goto":
            raise RuntimeError("navigation failed")

    async def wait_for_selector(self, *_a: object, **_k: object) -> None:
        return None

    async def fill(self, *_a: object) -> None:
        return None

    async def click(self, *_a: object) -> None:
        return None

    async def text_content(self, *_a: object) -> str | None:
        return self.code


class FakeBrowser:
    def __init__(self, page: FakePage) -> None:
        self.page = page
        self.closed = False

    async def new_page(self) -> FakePage:
        return self.page

    async def close(self) -> None:
        self.closed = True


class FakePlaywright:
    def __init__(self, page: FakePage) -> None:
        self.browser = FakeBrowser(page)
        self.chromium = self
        self.args: list[str] = []

    async def launch_persistent_context(self, profile_dir: str, *, headless: bool, args: list[str]) -> FakeBrowser:
        assert headless
        assert profile_dir.endswith("browser")
        self.args = args
        return self.browser

    async def __aenter__(self) -> FakePlaywright:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


def _authorize(fake: FakePlaywright, progress: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch) -> str:
    async def link(*_a: object) -> str:
        return "https://x.example/magic-link#t"

    monkeypatch.setattr(automation, "magic_link", link)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    opts = dataclasses.replace(FAST, playwright=lambda: lambda: fake)
    box = Mailbox(host="h", user="u", password="p", port=993)
    return asyncio.run(
        automation.authorize(
            "https://x.example/oauth/authorize",
            box,
            "e@x.test",
            opts=opts,
            progress=progress,
            budget=10.0,
            clock=lambda: 0.0,
            profile_dir=Path("/nonexistent/browser"),
        )
    )


async def _quiet(_stage: str) -> None:
    return None


@pytest.mark.parametrize(
    ("code", "fail_at", "reason"),
    [(None, None, "code_form"), ("a b", None, "code_form"), ("x", "goto", "RuntimeError")],
)
def test_a_missing_code_or_a_browser_error_fails_the_automation(
    monkeypatch: pytest.MonkeyPatch, code: str | None, fail_at: str | None, reason: str
) -> None:
    fake = FakePlaywright(FakePage(code, fail_at))
    with pytest.raises(AutomationFailed) as failed:
        _authorize(fake, _quiet, monkeypatch)
    assert failed.value.reason == reason
    assert fake.browser.closed
    assert fake.args == []


def test_a_closed_request_is_not_mistaken_for_an_automation_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    async def closed(_stage: str) -> None:
        raise RequestClosed("forbidden")

    fake = FakePlaywright(FakePage("CODE#STATE"))
    with pytest.raises(RequestClosed):
        _authorize(fake, closed, monkeypatch)
    assert fake.browser.closed


def test_the_fake_browser_path_returns_the_code(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    async def note(stage: str) -> None:
        seen.append(stage)

    assert _authorize(FakePlaywright(FakePage(" CODE#STATE \n")), note, monkeypatch) == "CODE#STATE"
    assert seen == STAGES


def test_the_platform_chromium_below_the_runner_home_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # set before it is removed, so the undo also removes what `use_browsers` sets (a bare `delenv` of an absent
    # variable records nothing, and the later tests of the worker would see this test's empty directory)
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "unset")
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH")
    automation.use_browsers(tmp_path)
    assert "PLAYWRIGHT_BROWSERS_PATH" not in os.environ
    (tmp_path / "ms-playwright").mkdir()
    automation.use_browsers(tmp_path)
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(tmp_path / "ms-playwright")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/elsewhere")
    automation.use_browsers(tmp_path)
    assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == "/elsewhere"
