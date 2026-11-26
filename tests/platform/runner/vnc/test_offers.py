"""U-RUN-vnc (offers): what the check for humans does while a person works on its screen by VNC (ADR-101 section 4) -
no picture goes out and the check keeps waiting - and the long-lived helper process of a login VM's or a browser VM's
screen (`workvm-vnc connect`)."""

from __future__ import annotations

import asyncio
import io
from collections.abc import Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent_runner.login import requests as login_requests
from agent_runner.login.pages import parse
from agent_runner.login.relay import LoginOptions
from agent_runner.login.sandbox import vm_sandbox
from agent_runner.vnc import requests as vnc_requests
from agent_runner.vnc.requests import VncOptions, session_offer
from agent_runner.vnc.screens import Offer

from .kit import FakeFlow


class Shell:
    """A `VmShell` whose long-lived processes are local commands (`argv` maps the VM's argv to `cat`)."""

    def argv(self, argv: Sequence[str]) -> list[str]:
        return ["cat"]

    async def __call__(self, argv: Sequence[str], **kw: Any) -> Any:
        return SimpleNamespace(returncode=0, stdout=b'{"ok":true}', truncated=False)


async def _echo(spawned: Any) -> bytes:
    spawned.stdin.write(b"RFB")
    await spawned.stdin.drain()
    spawned.stdin.close()
    data: bytes = await spawned.stdout.read()
    await spawned.wait()
    return data


def test_while_a_person_works_on_the_screen_no_picture_goes_out(monkeypatch: pytest.MonkeyPatch) -> None:
    kinds = iter(["challenge", "challenge", "challenge", "email"])
    pictures: list[int] = []

    async def classify(sandbox: Any) -> Any:
        return SimpleNamespace(kind=next(kinds))

    async def picture() -> None:
        pictures.append(1)

    async def wait_follow_up(seconds: float) -> None:
        await asyncio.sleep(0)

    progress: list[str] = []

    async def stage(name: str) -> None:
        progress.append(name)

    monkeypatch.setattr("agent_runner.login.automation.classify", classify)
    flow = SimpleNamespace(wait_follow_up=wait_follow_up, context=SimpleNamespace(log=io.StringIO()), id="r1")
    check = login_requests.HumanCheck(
        flow,  # type: ignore[arg-type]
        LoginOptions(verify_wait=0.001, verify_tick=0.001),
        extended=True,
        progress=stage,
        vnc=True,
    )
    offer = Offer("login", "r1", "alice", screen=None)  # type: ignore[arg-type]
    offer.viewers = 1
    sandbox = SimpleNamespace(picture=picture)
    asyncio.run(check.wait(sandbox, parse(""), offer))
    assert pictures == [1]  # only the first look; the person sees the screen itself
    assert progress == ["verified"]


def test_a_login_vms_screen_runs_its_helper_over_the_slots_shell() -> None:
    async def main() -> bytes:
        async def obtain_login(request_id: str) -> Any:
            return SimpleNamespace(id="wvm-1")

        async def delete(vm: Any, *, reason: str) -> None:
            return None

        workvms = SimpleNamespace(
            obtain_login=obtain_login, delete=delete, deps=SimpleNamespace(shell_for=lambda vm: Shell())
        )
        sandbox = await vm_sandbox(workvms, "r1", lambda text: None)
        return await _echo(await sandbox.screen.spawn(["sudo", "-n", "workvm-vnc", "connect"]))

    assert asyncio.run(main()) == b"RFB"


def test_a_browser_vms_screen_runs_its_helper_over_the_slots_shell(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vm = SimpleNamespace(id="wvm-1")

    class Browsers:
        workvms = SimpleNamespace(deps=SimpleNamespace(shell_for=lambda found: Shell()))

        async def running(self, session_id: str) -> Any:
            return (None, vm)

        @property
        def db(self) -> Any:
            return self

        def connection(self) -> Any:
            class Opened:
                async def __aenter__(self) -> None:
                    return None

                async def __aexit__(self, *error: object) -> None:
                    return None

            return Opened()

    async def get(conn: Any, session_id: str) -> Any:
        return SimpleNamespace(owner_id="alice")

    monkeypatch.setattr(vnc_requests, "browsers_for", lambda context: Browsers())
    monkeypatch.setattr("agent_runner.sessions.records.get", get)

    async def main() -> bytes:
        flow = FakeFlow({"kind": "session", "id": "s1"})
        made = await session_offer(flow, "s1", VncOptions())  # type: ignore[arg-type]
        assert made is not None
        offer, _watch = made
        return await _echo(await offer.screen.spawn(["sudo", "-n", "workvm-vnc", "connect"]))  # type: ignore[attr-defined]

    assert asyncio.run(main()) == b"RFB"
