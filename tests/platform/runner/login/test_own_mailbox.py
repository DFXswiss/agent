"""U-RUN-login (KOR-73, ADR-098 sections 1 and 3): the login VM path of `LOGIN_SANDBOX=vm` (a device with work VMs;
the sandbox is closed on every path, a failing close is only logged) and `mailbox.check {subscription_id}` against the
subscription's own mailbox."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agent_runner.login import requests
from agent_runner.login.sandbox import Picture, SandboxError

from ..credentials.kit import ALICE
from ..credentials.subscription_kit import SubscriptionHub, subscription_body
from .conftest import FAST, Login, Run, cli_argv, code_for, mailbox_body, run_request

RECORD = "01930099-0000-7000-8000-0000000000c1"


class Blank:
    """A login VM whose page is never one the automation knows; its close fails."""

    def __init__(self, error: Exception) -> None:
        self.closed = False
        self.error = error

    async def goto(self, url: str) -> None:
        return None

    async def evaluate(self, script: str) -> str:
        return json.dumps({"kind": "unknown"})

    async def type(self, target: str, text: str) -> None:
        return None

    async def click(self, target: str) -> None:
        return None

    async def click_at(self, x: float, y: float) -> None:
        return None

    async def picture(self) -> Picture | None:
        return None

    async def close(self) -> None:
        self.closed = True
        raise self.error


@pytest.mark.parametrize(
    "case", [(SandboxError("delete_failed"), "delete_failed"), (RuntimeError("db"), "RuntimeError")]
)
def test_a_device_with_work_vms_drives_the_login_in_a_login_vm(
    store_dsn: str,
    tmp_path: Path,
    doubles: Callable[..., Login],
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[Exception, str],
) -> None:
    error, reason = case
    login = doubles()
    made: list[tuple[Any, str]] = []
    blank = Blank(error)

    async def vm_sandbox(workvms: Any, request_id: str, log: Callable[[str], None]) -> Blank:
        made.append((workvms, request_id))
        return blank

    workvms = object()
    monkeypatch.setattr(requests, "workvms_for", lambda _context: workvms)
    monkeypatch.setattr(requests, "vm_sandbox", vm_sandbox)

    async def check(run: Run) -> None:
        bodies = await run.wait_bodies(2, seconds=30)
        assert bodies[-1]["reason"] == "unknown_page"
        assert made == [(workvms, run.root)]
        assert blank.closed
        assert f"runner.login sandbox_close_failed reason={reason}" in run.env.log()
        follow = await run.follow({"code": await asyncio.to_thread(code_for, login, bodies[-1]["url"])})
        await run.settle()
        assert run.bodies(follow) == [{"step": "done"}]

    run_request(
        store_dsn,
        tmp_path,
        "claude.login",
        {"mode": "automatic", "extended": True},
        check,
        argv=cli_argv(login),
        automation=True,
        records={"mailbox": mailbox_body(login)},
        opts=dataclasses.replace(FAST, page_timeout=0.5),
        config={"login_sandbox": "vm"},
    )


@pytest.mark.parametrize(
    "case",
    [
        ({"subscription_id": RECORD}, True, {"step": "done", "valid": True}),
        ({"subscription_id": RECORD}, False, {"step": "failed", "code": "credential_missing"}),
        ({"subscription_id": "nope"}, True, {"step": "failed", "code": "upstream_error"}),
        ({"folder": "INBOX"}, True, {"step": "failed", "code": "upstream_error"}),
    ],
)
def test_the_own_mailbox_of_a_subscription_is_checked(
    store_dsn: str,
    tmp_path: Path,
    doubles: Callable[..., Login],
    case: tuple[dict[str, Any], bool, dict[str, Any]],
) -> None:
    body, own, expected = case
    login = doubles()

    async def check(run: Run) -> None:
        await run.settle()
        assert run.bodies() == [expected]

    def install(env: Any) -> None:
        hub = SubscriptionHub(env.hub).install()
        record = subscription_body(mailbox=mailbox_body(login)) if own else subscription_body()
        hub.add(record, owner=ALICE, record_id=RECORD)

    run_request(store_dsn, tmp_path, "mailbox.check", body, check, before=install)
