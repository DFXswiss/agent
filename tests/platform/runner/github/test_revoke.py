"""U-RUN-github: "GitHub trennen" revokes a token the platform minted (KOR-40, `agent_runner.github.revoke`), and the
before-purge hook of the credential cache that hands it the decrypted record."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_runner.credentials import cache
from agent_runner.github import revoke
from agent_runner.github.client import GitHubOptions
from agent_runner.github.reconcile import STEPS
from tests.platform.runner.credentials.kit import ALICE, Env, github_body, kit_env, make_record

Body = Callable[[Env], Awaitable[None]]


class GitHub:
    """A mock transport: records the requests and answers with `status`."""

    def __init__(self, status: int = 202) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, json={})


def run(dsn: str, body: Body, github: GitHub) -> None:
    async def main() -> None:
        async with kit_env(dsn) as env:
            env.context.github_options = GitHubOptions(wall=lambda: 0.0, transport=httpx.MockTransport(github))
            await body(env)

    asyncio.run(main())


async def disconnect(env: Env, token: str, *, minted: bool = True) -> None:
    """The record is cached (minted by this device unless `minted` is false), then deleted at the hub: the next fetch
    purges it."""
    env.hub.records[(ALICE, "github")] = make_record(github_body(token=token))
    await env.add_ck()
    await cache.fetch(env.context, ALICE, "github")
    if minted:
        await revoke.remember(env.context, ALICE, token)
        await revoke.remember(env.context, ALICE, token)  # once
    revoke.subscribe(env.context)
    revoke.subscribe(env.context)  # once per daemon
    del env.hub.records[(ALICE, "github")]
    assert await cache.fetch(env.context, ALICE, "github") is None


def test_a_minted_token_is_revoked_without_credentials(store_dsn: str) -> None:
    github = GitHub()

    async def body(env: Env) -> None:
        await disconnect(env, "ghu_minted0123456789")
        (request,) = github.requests
        assert (request.method, request.url.path) == ("POST", "/credentials/revoke")
        assert json.loads(request.content) == {"credentials": ["ghu_minted0123456789"]}
        assert "authorization" not in request.headers
        assert f"runner.github github_revoke outcome=revoked owner={ALICE}" in env.log()
        assert "ghu_minted0123456789" not in env.log()
        assert await env.cache_rows() == []

    run(store_dsn, body, github)


@pytest.mark.parametrize("token", ["gho_from_the_cli_0123", "ghu_another_app_0123", "ghu_the_operators_0123"])
def test_a_token_this_device_did_not_mint_is_never_revoked(store_dsn: str, token: str) -> None:
    github = GitHub()

    async def body(env: Env) -> None:
        await disconnect(env, token, minted=False)
        assert github.requests == []
        assert "github_revoke outcome=skipped reason=not_minted" in env.log()

    run(store_dsn, body, github)


def test_the_operators_token_is_never_revoked(store_dsn: str, tmp_path: Path) -> None:
    github = GitHub()
    folder = tmp_path / "operator"
    folder.mkdir(mode=0o700)
    token_file = folder / "github.token"
    token_file.write_text("ghu_operator0123456789\n", encoding="utf-8")
    os.chmod(token_file, 0o600)

    async def body(env: Env) -> None:
        env.context.config.operator_github_token_file = token_file
        await disconnect(env, "ghu_operator0123456789")
        assert github.requests == []
        assert "github_revoke outcome=skipped reason=operator" in env.log()

    run(store_dsn, body, github)


def test_an_unusable_operator_file_does_not_stop_the_revocation(store_dsn: str, tmp_path: Path) -> None:
    github = GitHub()

    async def body(env: Env) -> None:
        env.context.config.operator_github_token_file = tmp_path / "missing" / "github.token"
        await disconnect(env, "ghu_minted0123456789")
        assert len(github.requests) == 1
        assert "operator_github file_unusable reason=missing" in env.log()

    run(store_dsn, body, github)


def test_a_failed_revocation_is_logged_and_the_record_purged_anyway(store_dsn: str) -> None:
    github = GitHub(status=500)

    async def body(env: Env) -> None:
        await disconnect(env, "ghu_minted0123456789")
        assert "github_revoke outcome=failed reason=server_error" in env.log()
        assert await env.cache_rows() == []

    run(store_dsn, body, github)


def test_other_kinds_are_ignored(store_dsn: str) -> None:
    github = GitHub()

    async def body(env: Env) -> None:
        assert await revoke.revoke(env.context, ALICE, "mailbox", {"token": "ghu_x"}) == "ignored"
        assert await revoke.revoke(env.context, ALICE, "github", {"login": "octo"}) == "skipped"
        assert github.requests == []

    run(store_dsn, body, github)


def test_a_device_that_lost_its_eligibility_does_not_revoke(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Review H1: a device removed from the owner's organizations also gets `404` and purges, but the user did not
    disconnect."""
    github = GitHub()

    async def body(env: Env) -> None:
        assert cache.eligible(env.context, ALICE) is True
        monkeypatch.setattr(cache, "eligible", lambda _context, _owner: False)
        await disconnect(env, "ghu_minted0123456789")
        assert github.requests == []
        assert "github_revoke outcome=skipped reason=not_eligible" in env.log()

    run(store_dsn, body, github)


def test_the_reconcile_step_registers_the_revocation(store_dsn: str) -> None:
    github = GitHub()

    async def body(env: Env) -> None:
        (step,) = STEPS
        assert (step.order, step.name) == (6, "github_revoke")
        await step.run(type("Run", (), {"context": env.context})())
        assert getattr(env.context, revoke.ATTRIBUTE) is True

    run(store_dsn, body, github)


# ---------------------------------------------------------------- the before-purge hook of the cache


def test_the_hook_gets_the_body_once_and_failures_change_nothing(store_dsn: str) -> None:
    seen: list[dict[str, Any]] = []

    async def listener(owner: str, kind: str, body: dict[str, Any]) -> None:
        seen.append(dict(body))

    async def broken(_owner: str, _kind: str, _body: dict[str, Any]) -> None:
        raise RuntimeError("hook")

    async def body(env: Env) -> None:
        env.hub.records[(ALICE, "github")] = make_record(github_body())
        await env.add_ck()
        await cache.fetch(env.context, ALICE, "github")
        cache.subscribe_before_purge(env.context, frozenset({"github"}), broken)
        cache.subscribe_before_purge(env.context, frozenset({"github"}), listener)
        assert await cache.purge(env.context, ALICE, "github") is True
        assert [item["token"] for item in seen] == ["double-user-token"]
        assert "before_purge_failed error=RuntimeError" in env.log()
        assert await cache.purge(env.context, ALICE, "github") is False  # nothing cached: no hook
        assert len(seen) == 1

    run(store_dsn, body, GitHub())


def test_a_record_that_does_not_open_is_purged_without_the_hook(store_dsn: str) -> None:
    seen: list[str] = []

    async def listener(owner: str, kind: str, body: dict[str, Any]) -> None:
        seen.append(kind)

    async def body(env: Env) -> None:
        env.hub.records[(ALICE, "github")] = make_record(github_body())
        await cache.fetch(env.context, ALICE, "github")  # no CK of the record's epoch on this device
        cache.subscribe_before_purge(env.context, frozenset({"github"}), listener)
        assert await cache.purge(env.context, ALICE, "github") is True
        assert seen == []
        assert f"before_purge_skipped owner={ALICE} kind=github reason=key_missing" in env.log()

    run(store_dsn, body, GitHub())
