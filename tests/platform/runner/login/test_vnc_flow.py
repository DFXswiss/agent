"""U-RUN-login (VNC, ADR-101): check for humans → VNC → the login continues, end to end on one device.

The login runs through the real request service in the process sandbox (headless Chromium of Playwright) against the
auth-page double with a check for humans (`Scenario(fault="challenge")`); its screen is the VNC double, whose clicks
are clicked into the sandbox's real page. With `vnc: true` the device answers `needs_human`; the owner's `vnc.open`
gets the key and the one-time password; the agent tool's bridge (`agent_platform.vnc.bridge`) listens on the loopback
and "opens the viewer" (the viewer double authenticates with the password and clicks into the box); the records go
through the hub's real relay in memory. The check passes, the connection ends (`ended`), the login goes on to `done`;
the password reaches neither the log nor the hub, and a client without `vnc` never sees `needs_human`."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_doubles.login_cli.authpage import Scenario
from agent_platform.vnc import bridge
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.login.sandbox import ProcessSandbox, load_playwright, use_browsers
from agent_runner.vnc.requests import VncOptions

from ..credentials.kit import seal_request
from ..vnc.double import DoubleScreen, VncServer, view_and_click
from ..vnc.kit import Wire
from .conftest import FAST, USER, Login, Run, cli_argv, mailbox_body, run_request

BOX = (112, 112)
EXTENDED = {"mode": "automatic", "extended": True}


def _with_screen(server: VncServer) -> Callable[..., Any]:
    """The login's sandbox: the process sandbox with the VNC double as its screen (clicks go into the real page)."""

    async def make(flow: Any, url: str, workdir: Path) -> ProcessSandbox:
        opts = flow.context.login_options
        use_browsers(flow.context.config.home)
        sandbox = await ProcessSandbox.start(load_playwright(opts.playwright), workdir / "browser", url, 10.0)

        async def click(x: int, y: int) -> None:
            await sandbox.click_at(x, y)

        server.on_click = click
        sandbox.screen = DoubleScreen(server)  # type: ignore[attr-defined]
        return sandbox

    return make


async def _bodies(run: Run, request_id: str, step: str, seconds: float = 60.0) -> list[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while not any(body["step"] == step for body in run.bodies(request_id)):
        assert loop.time() < end, run.bodies(request_id)
        await asyncio.sleep(0.05)
    return run.bodies(request_id)


def test_the_check_for_humans_is_taken_over_by_vnc_and_the_login_continues(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles(Scenario(fault="challenge", expect=("challenge_passed", "token")))
    server, wire = VncServer(), Wire()
    viewers: list[asyncio.Future[Any]] = []

    async def check(run: Run) -> None:
        await server.start()
        run.env.context.vnc_options = VncOptions(idle=30.0, pair_wait=30.0, connect=wire.connector("device"))
        bodies = await _bodies(run, run.root, "needs_human")
        assert {"step": "needs_human", "vnc_available": True} in bodies
        vnc_id = run.env.hub.add_request(seal_request("vnc.open", {"kind": "login", "id": run.root}))
        await run.service.process(vnc_id)
        offered = (await _bodies(run, vnc_id, "vnc"))[0]
        assert offered["step"] == "vnc"

        def opener(port: int, password: str) -> str:
            viewers.append(asyncio.ensure_future(view_and_click(port, password, *BOX, stay=True)))
            return "viewer-double"

        local = await bridge(
            f"ws://hub.test/v1/vnc/{vnc_id}",
            {},
            b64u_decode(offered["key"], 32),
            vnc_id,
            offered["password"],
            opener=opener,
            connect=wire.connector("client"),
        )
        assert local == "socket_closed"  # the device ended the connection once the check had passed
        assert await viewers[0] == {"width": 1280, "height": 800, "name": "agent-vnc-double"}
        await run.settle()
        assert run.bodies(vnc_id)[-1] == {"step": "done", "reason": "ended"}
        assert server.clicks == [BOX]
        assert server.password is None  # the VNC server was stopped
        login_bodies = run.bodies()
        assert {"step": "progress", "stage": "verified"} in login_bodies
        assert login_bodies[-1] == {"step": "done", "email": USER}
        assert "challenge_passed" in login.auth.state()["milestones"]
        log = run.env.log()
        assert "runner.login vnc_offered" in log
        assert "runner.vnc vnc_connected" in log
        assert "runner.vnc closed reason=ended" in log
        assert offered["password"] not in log
        assert [action for action, _ in wire.audits] == ["vnc.connect", "vnc.close"]
        assert offered["password"] not in repr(wire.audits)
        await server.stop()

    opts = dataclasses.replace(FAST, sandbox=_with_screen(server), verify_wait=60.0, verify_tick=0.2, verify_settle=0.3)
    run_request(
        store_dsn,
        tmp_path,
        "claude.login",
        {**EXTENDED, "vnc": True},
        check,
        argv=cli_argv(login),
        automation=True,
        records={"mailbox": mailbox_body(login)},
        opts=opts,
    )


def test_without_a_screen_needs_human_says_so_and_without_vnc_it_is_never_sent(
    store_dsn: str, tmp_path: Path, doubles: Callable[..., Login]
) -> None:
    login = doubles(Scenario(fault="challenge"))

    async def no_screen(run: Run) -> None:
        bodies = await _bodies(run, run.root, "verify")
        assert {"step": "needs_human", "vnc_available": False} in bodies
        vnc_id = run.env.hub.add_request(seal_request("vnc.open", {"kind": "login", "id": run.root}))
        await run.service.process(vnc_id)
        assert (await _bodies(run, vnc_id, "failed"))[-1] == {"step": "failed", "code": "no_screen"}
        await run.follow({"relay": True})
        await _bodies(run, run.root, "authorize")

    common: dict[str, Any] = {
        "argv": cli_argv(login),
        "automation": True,
        "records": {"mailbox": mailbox_body(login)},
        "opts": dataclasses.replace(FAST, verify_wait=60.0, verify_tick=0.2),
    }
    run_request(store_dsn, tmp_path, "claude.login", {**EXTENDED, "vnc": True}, no_screen, **common)

    async def without(run: Run) -> None:
        bodies = await _bodies(run, run.root, "verify")
        assert all(body["step"] != "needs_human" for body in bodies)
        await run.follow({"relay": True})
        await _bodies(run, run.root, "authorize")

    run_request(store_dsn, tmp_path, "claude.login", EXTENDED, without, **common)


def test_a_root_with_a_malformed_vnc_field_is_upstream_error(store_dsn: str, tmp_path: Path) -> None:
    async def check(run: Run) -> None:
        await run.settle()
        assert run.bodies() == [{"step": "failed", "code": "upstream_error"}]

    run_request(store_dsn, tmp_path, "claude.login", {**EXTENDED, "vnc": False}, check)
