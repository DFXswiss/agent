"""U-RUN-login (subscriptions, KOR-69, ADR-090 section 2; KOR-71, ADR-092 section 1): `claude.login` with
`subscription_id` runs the full login (`SUBSCRIPTION_LOGIN_ARGV`, the double's `auth login`) and fills that further
subscription with the access and refresh token, expiry, scopes and plan the program stored in its file (name and note
from the request, the record's own kept otherwise), never the primary record; the keychain is refused through the
private `security`; a login without `user:profile` ends `scope_missing`; the record's address is handed to the program
as `--email`; malformed subscription fields end `upstream_error` before the login program starts; `email` replaces the
mailbox's address of an automatic login."""

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
from ..sync.fakehub import SCHEMAS
from .conftest import FAST, USER, Login, Run, code_for, full_argv, mailbox_body, run_request

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
        token, refresh = codes.access_token(codes.DEFAULT_SEED, 1), codes.refresh_token(codes.DEFAULT_SEED, 1)
        stored = hub.opened(RECORD)
        expires = stored.pop("expires_at")
        assert stored == {
            "token": token,
            "refresh_token": refresh,
            "scopes": sorted(codes.FULL_SCOPE.split()),
            "subscription_type": "max",
            "rate_limit_tier": "default_claude_max_20x",
            "email": USER,
            "kind": "oauth_login",
            "name": "Team 2",
            "note": "for the reviews",
        }
        assert expires > "2026"
        assert SCHEMAS.errors("credentials/claude_subscription.json", {**stored, "expires_at": expires}) == []
        assert (ALICE, "claude_subscription") not in run.env.hub.records  # the primary record is untouched
        assert "login_hint" not in first[0]["url"]  # no address known yet
        run.secrets_absent(token, refresh, pasted)

    body = {"mode": "manual", "subscription_id": RECORD, "name": "Team 2", "note": "for the reviews"}
    run_request(store_dsn, tmp_path, "claude.login", body, check, config={"subscription_login_argv": full_argv(login)})


def test_a_new_login_signs_in_with_the_address_the_record_holds_and_needs_the_profile_scope(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles()

    async def check(run: Run) -> None:
        hub = SubscriptionHub(run.env.hub).install()
        hub.add(subscription_body(email="team@example.com", name="Team"), owner=ALICE, record_id=RECORD)
        first = await run.wait_bodies(1)
        assert "login_hint=team%40example.com" in first[0]["url"]
        follow = await run.follow({"code": await asyncio.to_thread(code_for, login, first[0]["url"])})
        await run.settle()
        assert run.bodies(follow) == [{"step": "failed", "code": "scope_missing"}]
        assert hub.puts == []  # the record stays as it was

    argv = full_argv(login, "--variant", "no-profile")
    body = {"mode": "manual", "subscription_id": RECORD}
    run_request(store_dsn, tmp_path, "claude.login", body, check, config={"subscription_login_argv": argv})


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
        store_dsn,
        tmp_path,
        "claude.login",
        {"mode": "manual", "subscription_id": RECORD},
        check,
        config={"subscription_login_argv": full_argv(login)},
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
        {"mode": "manual", "account": {"name": "Ada"}},
        {"mode": "automatic", "account": "Ada"},
        {"mode": "automatic", "account": {"name": ""}},
        {"mode": "automatic", "account": {"name": "Ada", "phone": "1"}},
        {"mode": "automatic", "account": {"name": "Ada", "consents": ["payment"]}},
        {"mode": "automatic", "account": {"name": "Ada", "consents": ["age", "age"]}},
        {"mode": "automatic", "account": {"name": "Ada", "consents": "age"}},
        {"mode": "automatic", "account": {"name": "Ada", "birth_date": "1990-13-01"}},
        {"mode": "automatic", "account": {"name": "Ada", "birth_date": 1990}},
        {"mode": "automatic", "extended": False},
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
        bodies = await run.wait_bodies(3, seconds=60)
        assert bodies[-1]["automation"] == "failed"
        login.auth.set_scenario(Scenario())
        follow = await run.follow({"code": await asyncio.to_thread(code_for, login, bodies[-1]["url"])})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]
        assert hub.opened(RECORD)["email"] == USER  # the relay finished it: the address the program reports

    body = {"mode": "automatic", "subscription_id": RECORD, "email": "alias+2@example.com"}
    opts = dataclasses.replace(FAST, page_timeout=1.5, mail_wait=1.0, mail_poll=0.2)
    run_request(
        store_dsn,
        tmp_path,
        "claude.login",
        body,
        check,
        config={"subscription_login_argv": full_argv(login)},
        automation=True,
        records={"mailbox": mailbox_body(login)},
        opts=opts,
    )


@pytest.mark.parametrize("unreadable", [True, False])
def test_no_address_is_handed_on_when_the_record_cannot_be_read_or_holds_none(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login], unreadable: bool
) -> None:
    login = doubles()

    async def check(run: Run) -> None:
        hub = SubscriptionHub(run.env.hub).install()
        if unreadable:
            hub.refusals[RECORD] = (503, {"error": "unavailable"})
        else:
            hub.add(subscription_body(email=None), owner=ALICE, record_id=RECORD)
        first = await run.wait_bodies(1)
        assert "login_hint" not in first[0]["url"]
        hub.refusals.clear()
        follow = await run.follow({"code": await asyncio.to_thread(code_for, login, first[0]["url"])})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]
        assert hub.opened(RECORD)["email"] == USER

    body = {"mode": "manual", "subscription_id": RECORD}
    run_request(store_dsn, tmp_path, "claude.login", body, check, config={"subscription_login_argv": full_argv(login)})
