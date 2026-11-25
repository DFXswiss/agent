"""U-RUN-login (subscriptions, KOR-69, ADR-090 section 2): `claude.login` with `subscription_id` fills that further
subscription (name and note from the request, the record's own kept otherwise), never the primary record; malformed
subscription fields end `upstream_error` before the login program starts; `email` replaces the mailbox's address of
an automatic login."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from pathlib import Path

import pytest

from agent_doubles.login_cli import codes
from agent_doubles.login_cli.authpage import Scenario

from ..credentials.kit import ALICE
from ..credentials.subscription_kit import SubscriptionHub, subscription_body
from .conftest import FAST, Login, Run, cli_argv, code_for, mailbox_body, run_request

RECORD = "01930099-0000-7000-8000-0000000000b1"


def test_a_login_fills_the_named_subscription(store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]) -> None:
    login = doubles()

    async def check(run: Run) -> None:
        hub = SubscriptionHub(run.env.hub).install()
        first = await run.wait_bodies(1)
        pasted = await asyncio.to_thread(code_for, login, first[0]["url"])
        follow = await run.follow({"code": pasted})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]
        token = codes.access_token(codes.DEFAULT_SEED, 1)
        assert hub.opened(RECORD) == {
            "token": token,
            "email": None,
            "kind": "setup_token",
            "name": "Team 2",
            "note": "for the reviews",
        }
        assert (ALICE, "claude_subscription") not in run.env.hub.records  # the primary record is untouched
        run.secrets_absent(token, pasted)

    body = {"mode": "manual", "subscription_id": RECORD, "name": "Team 2", "note": "for the reviews"}
    run_request(store_dsn, tmp_path, "claude.login", body, check, argv=cli_argv(login))


def test_a_login_of_the_primary_record_through_its_id_is_refused(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles()

    async def check(run: Run) -> None:
        hub = SubscriptionHub(run.env.hub).install()
        hub.add(subscription_body(), owner=ALICE, primary=True, record_id=RECORD)
        first = await run.wait_bodies(1)
        follow = await run.follow({"code": await asyncio.to_thread(code_for, login, first[0]["url"])})
        await run.settle()
        assert run.bodies(follow) == [{"step": "failed", "code": "upstream_error"}]
        assert "store_failed reason=refused code=primary" in run.env.log()

    run_request(
        store_dsn, tmp_path, "claude.login", {"mode": "manual", "subscription_id": RECORD}, check, argv=cli_argv(login)
    )


@pytest.mark.parametrize(
    "body",
    [
        {"mode": "manual", "subscription_id": "not-a-uuid"},
        {"mode": "manual", "subscription_id": 7},
        {"mode": "manual", "name": ""},
        {"mode": "manual", "name": "a\nb"},
        {"mode": "manual", "note": "n" * 501},
        {"mode": "manual", "email": "x@example.com"},
        {"mode": "automatic", "email": "no-at-sign"},
        {"mode": "automatic", "email": 5},
    ],
)
def test_malformed_subscription_fields_end_upstream_error(
    store_dsn: str, tmp_path: Path, body: dict[str, object]
) -> None:
    async def check(run: Run) -> None:
        await run.settle()
        assert run.bodies() == [{"step": "failed", "code": "upstream_error"}]
        assert not run.tmp_dir.exists()

    run_request(store_dsn, tmp_path, "claude.login", body, check)


def test_an_automatic_login_signs_in_with_the_given_address(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles(Scenario(junk=True, fault="email_page"))

    async def check(run: Run) -> None:
        hub = SubscriptionHub(run.env.hub).install()
        bodies = await run.wait_bodies(2, seconds=60)
        assert bodies[-1]["automation"] == "failed"
        login.auth.set_scenario(Scenario())
        follow = await run.follow({"code": await asyncio.to_thread(code_for, login, bodies[-1]["url"])})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]
        assert hub.opened(RECORD)["email"] is None  # the relay finished it: no address is known

    body = {"mode": "automatic", "subscription_id": RECORD, "email": "alias+2@example.com"}
    opts = dataclasses.replace(FAST, page_timeout=1.5, mail_wait=1.0, mail_poll=0.2)
    run_request(
        store_dsn,
        tmp_path,
        "claude.login",
        body,
        check,
        argv=cli_argv(login),
        automation=True,
        records={"mailbox": mailbox_body(login)},
        opts=opts,
    )
