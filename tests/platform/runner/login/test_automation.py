"""U-RUN-login (automation): `claude.login` mode `automatic` (05-runner.md section 11.3, ADR-099) in the process sandbox
(`LOGIN_SANDBOX=process`: headless Chromium of Playwright for Python, browsers under `PLAYWRIGHT_BROWSERS_PATH`), the
auth-page double with the real page shapes, the mail double (magic link in the Junk folder, or only the code) and the
login-CLI double: the sign-in by link or code, the registration of a new account with the owner's consents only, the
check for humans done by the owner's click into the picture, and the fall-back to the relay with
`automation:"failed"` (and `reason` for a client with `extended`) on every other state."""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import importlib.util
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_doubles.login_cli import codes
from agent_doubles.login_cli.authpage import Scenario
from agent_runner.credentials import cache

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
    "email_page": (["browser_started", "email_entered"], "human_check"),
    "no_mail": (["browser_started", "email_entered"], "mail_timeout"),
    "authorize_page": (["browser_started", "email_entered", "magic_link_received"], "unknown_page"),
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


# --- KOR-73: extended clients, code from the mail, registration, the check for humans ----------------------------


EXTENDED = {"mode": "automatic", "extended": True}
BOX = (112, 112)
"""Centre of the double's challenge checkbox in CSS pixels (`authpage.CHALLENGE_BOX`)."""


def _auto(store_dsn: str, tmp_path: Path, login: Login, body: dict[str, Any], check: Any, **kw: Any) -> None:
    kw.setdefault("records", {"mailbox": mailbox_body(login)})
    run_request(store_dsn, tmp_path, "claude.login", body, check, argv=cli_argv(login), automation=True, **kw)


def test_an_extended_client_learns_why_the_automation_gave_up_and_no_sandbox_means_relay(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    """`LOGIN_SANDBOX=vm` on a device without work VMs: `no_sandbox`, named only to a client with `extended`."""
    login = doubles()

    async def check(run: Run) -> None:
        bodies = await run.wait_bodies(1)
        assert bodies == [
            {"step": "authorize", "url": bodies[0]["url"], "automation": "failed", "reason": "no_sandbox"}
        ]
        late = await run.follow({"click": {"x": 0.5, "y": 0.5}})  # a late click of a check is passed over
        follow = await run.follow({"code": await asyncio.to_thread(code_for, login, bodies[0]["url"])})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]
        assert run.bodies(late) == []

    _auto(store_dsn, tmp_path, login, EXTENDED, check, config={"login_sandbox": "vm", "workvm_slots": 0})


def test_the_code_of_the_mail_signs_in_when_the_mail_has_no_link(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles(Scenario(link=False, expect=("code_entered", "authorized", "token")))

    async def check(run: Run) -> None:
        await run.settle()
        bodies = run.bodies()
        assert progress(bodies) == ["browser_started", "email_entered", "mail_code_received", "authorized"]
        assert bodies[-1] == {"step": "done", "email": USER}
        assert login.auth.state()["done"] is True

    _auto(store_dsn, tmp_path, login, EXTENDED, check)


def test_a_client_without_extended_never_sees_the_new_stages(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles(Scenario(link=False))

    async def check(run: Run) -> None:
        await run.settle()
        assert progress(run.bodies()) == ["browser_started", "email_entered", "authorized"]
        assert run.bodies()[-1] == {"step": "done", "email": USER}

    _auto(store_dsn, tmp_path, login, AUTOMATIC, check)


ACCOUNT = {"name": "Ada Example", "birth_date": "1990-04-01", "consents": ["terms", "age"]}


def test_a_new_address_is_registered_with_the_given_consents_only(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles(Scenario(junk=True, new_accounts=(USER,), expect=("registered", "token")))

    async def check(run: Run) -> None:
        await run.settle()
        bodies = run.bodies()
        assert progress(bodies) == [
            "browser_started",
            "email_entered",
            "magic_link_received",
            "registered",
            "authorized",
        ]
        assert bodies[-1] == {"step": "done", "email": USER}
        state = login.auth.state()
        assert state["registered"] == {USER: False}  # no marketing consent was given, none was ticked
        assert (state["done"], state["failures"]) == (True, [])
        run.secrets_absent("Ada Example", "1990-04-01")

    _auto(store_dsn, tmp_path, login, {**EXTENDED, "account": ACCOUNT}, check)


@pytest.mark.parametrize(
    ("account", "reason"),
    [
        (None, "account_missing"),
        ({"name": "Ada Example", "consents": ["terms", "age"]}, "account_missing"),
        ({"name": "Ada Example", "birth_date": "1990-04-01", "consents": ["terms", "marketing"]}, "consent_missing"),
    ],
)
def test_a_registration_without_the_data_or_a_consent_falls_back_to_the_relay(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login], account: dict[str, Any] | None, reason: str
) -> None:
    login = doubles(Scenario(new_accounts=(USER,)))

    async def check(run: Run) -> None:
        bodies = await run.wait_bodies(4, seconds=60)
        assert bodies[-1] == {"step": "authorize", "url": bodies[-1]["url"], "automation": "failed", "reason": reason}
        assert login.auth.state()["registered"] == {}

    body = dict(EXTENDED) if account is None else {**EXTENDED, "account": account}
    _auto(store_dsn, tmp_path, login, body, check)


def test_the_owner_passes_the_check_for_humans_with_a_click_into_the_picture(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles(Scenario(fault="challenge", expect=("challenge_passed", "token")))

    async def check(run: Run) -> None:
        bodies = await run.wait_bodies(2, seconds=60)
        verify = bodies[-1]
        assert verify["step"] == "verify"
        assert verify["frame"] == 1
        image = verify["image"]
        assert base64.b64decode(image["data"])[:2] == b"\xff\xd8"
        assert login.auth.state()["milestones"] == []  # nothing passes the check by itself
        await run.follow({"click": {"x": 0.01, "y": 0.01}})  # beside the box: the check stays
        await run.follow({"click": {"x": BOX[0] / image["width"], "y": BOX[1] / image["height"]}})
        await run.settle()
        done = run.bodies()
        assert "verified" in progress(done)
        assert done[-1] == {"step": "done", "email": USER}
        assert "challenge_passed" in login.auth.state()["milestones"]
        assert "runner.login verify_passed" in run.env.log()

    _auto(store_dsn, tmp_path, login, EXTENDED, check, opts=dataclasses.replace(FAST, verify_settle=0.5))


def test_the_owner_hands_the_check_over_to_the_relay(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles(Scenario(fault="challenge"))

    async def check(run: Run) -> None:
        await run.wait_bodies(2, seconds=60)
        await run.follow({"relay": True})
        bodies = await run.wait_bodies(3, seconds=60)
        assert bodies[-1] == {
            "step": "authorize",
            "url": bodies[-1]["url"],
            "automation": "failed",
            "reason": "human_check",
        }
        login.auth.set_scenario(Scenario())
        follow = await run.follow({"code": await asyncio.to_thread(code_for, login, bodies[-1]["url"])})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]

    _auto(store_dsn, tmp_path, login, EXTENDED, check)


def test_without_a_click_the_check_ends_in_the_relay(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles(Scenario(fault="challenge"))

    async def check(run: Run) -> None:
        bodies = await run.wait_bodies(3, seconds=60)
        assert [body["step"] for body in bodies] == ["progress", "verify", "authorize"]
        assert bodies[-1]["reason"] == "human_check"
        assert "automation_failed reason=human_check" in run.env.log()

    opts = dataclasses.replace(FAST, verify_wait=1.0, verify_tick=0.2)
    _auto(store_dsn, tmp_path, login, EXTENDED, check, opts=opts)
