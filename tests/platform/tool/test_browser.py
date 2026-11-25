"""The platform browser of a session (ADR-085 section 7, KOR-44): `browser list|show|frames` against the hub fake with
`browser.state` and `browser.action` events written by the device, the body checks of 13-work-vm.md section 13.5, and
`frames --follow` over the stream fake with live frames sealed like the runner's (04-crypto.md section 4.13)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from agent_platform import browser
from agent_runner.crypto import browserframe
from agent_runner.crypto.encoding import b64u_encode
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import WG

from .conftest import Tool
from .fakes.sessions import add_session, device_event
from .fakes.stream import frames, sse
from .hubfake import HubFake, User, now_ms_text
from .seeding import assign_device, new_task
from .test_workvm import at

A = "br-00000000000a"
B = "br-00000000000b"
JPEG = b"\xff\xd8\xff\xe0picture\xff\xd9"


def state(ident: str = A, **changes: Any) -> dict[str, Any]:
    found = {
        "browser_id": ident,
        "operation_id": uuid7(),
        "implicit": False,
        "state": "open",
        "workvm_id": "wvm-0a1b2c3d",
        "reason": None,
        "url": "https://example.org/",
        "actions": 0,
        "ready_ms": 4000,
        "idle_seconds": 600,
        "opened_at": at(60),
        "closed_at": None,
    }
    return {**found, **changes}


def image(data: bytes = JPEG, **changes: Any) -> dict[str, Any]:
    return {"type": "image/jpeg", "width": 960, "height": 600, "data": b64u_encode(data), **changes}


def action(seq: int, ident: str = A, **changes: Any) -> dict[str, Any]:
    found = {
        "browser_id": ident,
        "seq": seq,
        "tool": "browser_click",
        "action": "click",
        "target": "e12",
        "text": None,
        "url": "https://example.org/",
        "title": "Example",
        "tab": 0,
        "ok": True,
        "error": None,
        "notice": None,
        "at": at(30 - seq),
        "duration_ms": 120,
        "image": image(),
        "image_omitted": None,
    }
    return {**found, **changes}


Setup = tuple[str, str, str, str, str, dict[str, str]]


def setup(tool: Tool, hub: HubFake, account: User) -> Setup:
    """`(org, wg, device, task, session, place)`."""
    org, wg, device = assign_device(tool, hub, account)
    task = new_task(tool, wg)
    session = add_session(hub, task, device=device)
    return org, wg, device, task, session, {"wg": wg, "task": task, "session": session}


def test_bodies_follow_the_specification() -> None:
    assert browser.parse_state(state()) is not None
    assert browser.parse_state({**state(), "extra": 1}) is None
    assert browser.parse_action(action(1)) is not None
    assert browser.parse_action(action(1, image=None, image_omitted="too_large")) is not None
    assert browser.parse_action(action(1, image=image(width=961))) is None
    assert browser.parse_action(action(1, image={**image(), "data": "!!"})) is None
    assert browser.parse_action(action(1, image=image(b"x" * (browser.IMAGE_BYTES + 1)))) is None


def test_list_the_browser_sessions(tool: Tool, hub: HubFake, account: User) -> None:
    _org, wg, device, task, session, place = setup(tool, hub, account)
    assert tool.run("browser", "list", "--session", session)[1]["browsers"] == []
    for body in (
        state(A, state="starting", opened_at=None, ready_ms=None),
        state(A),
        state(B, implicit=True, operation_id=None),
        state(A, state="closed", reason="browser_idle", actions=7, closed_at=at(1)),
    ):
        device_event(hub, account, device, "browser.state", body, **place)
    device_event(hub, account, device, "browser.state", {**state(), "extra": 1}, **place)  # invalid
    other = add_session(hub, task, device=device)
    device_event(hub, account, device, "browser.state", state("br-00000000000c"), wg=wg, task=task, session=other)
    code, listed = tool.run("browser", "list", "--session", session)
    assert code == 0, listed
    assert [row["browser_id"] for row in listed["browsers"]] == [B, A]
    closed = listed["browsers"][1]
    assert (closed["state"], closed["reason"], closed["actions"], closed["session_id"]) == (
        "closed",
        "browser_idle",
        7,
        session,
    )
    assert listed["browsers"][0]["implicit"] is True
    assert (listed["task_id"], listed["session_id"], listed["invalid"], listed["locked"]) == (task, session, 1, 0)
    whole = tool.run("browser", "list", "--task", task)[1]
    assert [row["session_id"] for row in whole["browsers"]] == [other, session, session]
    trimmed = tool.run("browser", "list", "--task", task, "--limit", "1", "--fields", "browser_id,state")[1]
    assert trimmed["browsers"] == [{"browser_id": "br-00000000000c", "state": "open"}]
    assert tool.run("browser", "list")[1]["reason"] == "session_or_task"
    assert tool.run("browser", "list", "--session", session, "--task", task)[1]["reason"] == "session_or_task"
    hub.hooks[("GET", f"/v1/sessions/{session}")] = lambda _body, _request: (
        200,
        {"session": {"session_id": session, "wg_id": wg}},
    )
    assert tool.run("browser", "list", "--session", session)[1]["reason"] == "response_invalid"
    del hub.hooks[("GET", f"/v1/sessions/{session}")]
    # an event of another task, session or kind that the hub slips in is left out; without the key all are locked
    stranger = new_task(tool, wg, "Other")
    slipped = [
        device_event(hub, account, device, "browser.state", state(), wg=wg, task=stranger, session=session),
        device_event(hub, account, device, "browser.state", state(), wg=wg, task=task, session=other),
        device_event(hub, account, device, "browser.action", action(1), **place),
    ]
    hub.hooks[("GET", f"/v1/scopes/{wg}/events")] = lambda _body, _request: (
        200,
        {"events": slipped, "next_after": None},
    )
    assert tool.run("browser", "list", "--session", session)[1]["unverified"] == 3
    del hub.hooks[("GET", f"/v1/scopes/{wg}/events")]
    hub.wraps[:] = [wrap for wrap in hub.wraps if wrap["scope"] != "wg" or wrap["recipient_id"] != account.id]
    locked = tool.run("browser", "list", "--session", session)[1]
    assert (locked["browsers"], locked["locked"]) == ([], 5)


def test_show_the_action_record(tool: Tool, hub: HubFake, account: User) -> None:
    _org, _wg, device, task, session, place = setup(tool, hub, account)
    device_event(hub, account, device, "browser.state", state(), **place)
    typed = action(2, tool="browser_type", action="type", target="#password", text="••••", image=None)
    for body in (action(1), {**typed, "image_omitted": "unavailable"}, action(1, B, ok=False, error="timeout")):
        device_event(hub, account, device, "browser.action", body, **place)
    device_event(hub, account, device, "browser.action", {**action(3), "image": "x"}, **place)  # invalid
    code, shown = tool.run("browser", "show", "--session", session)
    assert code == 0, shown
    assert [(item["browser_id"], item["seq"]) for item in shown["actions"]] == [(A, 1), (A, 2), (B, 1)]
    first = shown["actions"][0]
    assert first["image"] == {"width": 960, "height": 600, "bytes": len(JPEG)}
    assert (first["tool"], first["target"], first["ok"], first["url"]) == (
        "browser_click",
        "e12",
        True,
        "https://example.org/",
    )
    second = shown["actions"][1]
    assert (second["text"], second["image"], second["image_omitted"]) == ("••••", None, "unavailable")
    assert (shown["task_id"], shown["browser_id"], shown["invalid"]) == (task, None, 1)
    only = tool.run("browser", "show", "--session", session, "--browser", A, "--limit", "1", "--fields", "seq,ok")[1]
    assert (only["actions"], only["limited"], only["browser_id"]) == ([{"seq": 2, "ok": True}], {"actions": 2}, A)
    assert tool.run("browser", "show", "--session", session, "--browser", "br-x")[1]["reason"] == "browser_id"
    assert tool.run("browser", "show", "--session", "nope")[1]["error"] == "invalid_argument"


def test_frames_writes_the_recorded_pictures(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    _org, _wg, device, _task, session, place = setup(tool, hub, account)
    for body in (action(1), action(2, image=None, image_omitted="too_large"), action(1, B, image=image(b"other"))):
        device_event(hub, account, device, "browser.action", body, **place)
    out = tmp_path / "pictures"
    code, written = tool.run("browser", "frames", "--session", session, "--output", str(out))
    assert code == 0, written
    assert [item["file"] for item in written["frames"]] == [str(out / f"{A}-1.jpg"), str(out / f"{B}-1.jpg")]
    assert (out / f"{A}-1.jpg").read_bytes() == JPEG
    assert (out / f"{B}-1.jpg").read_bytes() == b"other"
    assert os.stat(out / f"{A}-1.jpg").st_mode & 0o777 == 0o600
    assert (written["without_picture"], written["output"], written["frames"][0]["bytes"]) == (1, str(out), len(JPEG))
    only = tool.run("browser", "frames", "--session", session, "--browser", B, "--output", str(out))[1]
    assert [item["seq"] for item in only["frames"]] == [1]
    # a file where the directory should be, a link where a picture goes, live options without --follow
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert tool.run("browser", "frames", "--session", session, "--output", str(blocker))[1]["reason"] == (
        "output_unwritable"
    )
    (out / f"{A}-1.jpg").unlink()
    (out / f"{A}-1.jpg").symlink_to(blocker)
    refused = tool.run("browser", "frames", "--session", session, "--output", str(out))[1]
    assert (refused["reason"], refused["file"]) == ("output_unwritable", str(out / f"{A}-1.jpg"))
    assert blocker.read_text() == "x"
    for extra in (["--kbps", "250"], ["--seconds", "5"]):
        assert tool.run("browser", "frames", "--session", session, "--output", str(out), *extra)[1]["reason"] == (
            "follow_only"
        )


def live(hub: HubFake, user: User, where: tuple[str, str, str], seq: int, **changes: Any) -> bytes:
    """One `browser_live` stream frame of the session `where` = (org, wg, session), sealed like the runner
    (`changes` replace seal arguments)."""
    org, wg, session = where
    arguments: dict[str, Any] = {
        "browser": A,
        "session": session,
        "wg": wg,
        "epoch": hub.orgs[org].log.state.epoch(WG, wg),
        "seq": seq,
        "ts": now_ms_text(),
        "jpeg": JPEG + bytes([seq]),
        **changes,
    }
    frame = browserframe.seal_frame(hub.org_key(org, user, WG, wg), **arguments)
    return sse("browser_live", {"session_id": session, "frame": frame})


def test_follow_writes_the_live_frames(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    org, wg, device, task, session, place = setup(tool, hub, account)
    device_event(hub, account, device, "browser.state", state(B, state="closed", reason="browser_closed"), **place)
    device_event(hub, account, device, "browser.state", state(A), **place)
    where = (org, wg, session)
    good = live(hub, account, where, 1)
    foreign = live(hub, account, where, 4, wg=uuid7())  # another workgroup: the format check fails
    hub.stream_script = frames(  # type: ignore[attr-defined]
        b": heartbeat\n\n",
        sse("hello", {"stream_id": "s", "heads": {}, "hub_time": now_ms_text()}),
        good,
        good,  # not newer
        live(hub, account, where, 5, browser=B),  # another browser session
        b"event: browser_live\ndata: {not json\n\n",
        sse("browser_live", {"session_id": session}),
        sse("browser_live", {"session_id": uuid7(), "frame": {}}),
        foreign,
        live(hub, account, where, 6, epoch=9),  # no key of that epoch
        b"event: browser_live\ndata: " + b"x" * (browser.MAX_LINE + 1) + b"\n\n",
        live(hub, account, where, 3),
    )
    out = tmp_path / "live"
    code, watched = tool.run(
        "browser", "frames", "--session", session, "--output", str(out), "--follow", "--kbps", "250"
    )
    assert code == 0, watched
    assert [item["seq"] for item in watched["frames"]] == [1, 3]
    assert (out / "live-3.jpg").read_bytes() == JPEG + b"\x03"
    assert watched["frames"][0]["file"] == str(out / "live-1.jpg")
    assert (watched["dropped"], watched["invalid"], watched["locked"]) == (2, 4, 1)
    assert (watched["ended"], watched["browser_id"], watched["kbps"], watched["task_id"]) == (
        "stream_closed",
        A,
        250,
        task,
    )
    assert hub.stream_requests[-1] == {"org": org, "browser": session, "browser_kbps": "250"}  # type: ignore[attr-defined]


def test_follow_stops_after_its_seconds_or_ctrl_c(
    tool: Tool, hub: HubFake, account: User, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _org, _wg, _device, _task, session, _place = setup(tool, hub, account)
    hub.stream_script = frames(sse("hello", {}), 3.0)  # type: ignore[attr-defined]
    argv = ["browser", "frames", "--session", session, "--output", str(tmp_path), "--follow", "--browser", B]
    code, watched = tool.run(*argv, "--seconds", "1")
    assert code == 0, watched
    assert (watched["ended"], watched["frames"], watched["browser_id"], watched["kbps"]) == ("deadline", [], B, 1000)

    def interrupted(_inbox: Any, _timeout: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(browser, "_take", interrupted)
    assert tool.run(*argv)[1]["ended"] == "interrupted"


def broken(handler: Any) -> None:
    """A `200` whose chunked body breaks after the headers."""
    handler.wfile.write(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\nzz\r\n")


def silent(handler: Any) -> None:
    """No answer at all: the connection closes."""


def test_follow_refusals(tool: Tool, hub: HubFake, account: User, tmp_path: Path) -> None:
    _org, _wg, device, _task, session, place = setup(tool, hub, account)
    argv = ["browser", "frames", "--session", session, "--output", str(tmp_path), "--follow"]
    assert tool.run(*argv)[1]["reason"] == "no_open_browser"
    device_event(hub, account, device, "browser.state", state(A, state="closed", reason="task_closed"), **place)
    assert tool.run(*argv)[1]["reason"] == "no_open_browser"
    device_event(hub, account, device, "browser.state", state(B, state="restarted", reason="vm_lost"), **place)
    for extra in (["--kbps", "99"], ["--kbps", "10001"], ["--seconds", "0"], ["--seconds", "3601"]):
        assert tool.run(*argv, *extra)[1]["reason"] == "kbps_or_seconds"
    refused = tool.run(*argv)[1]  # no stream at the fake: a signed 404
    assert (refused["error"], refused["hub_error"]) == ("hub_refused", "not_found")
    hub.hooks[("GET", "/v1/stream")] = lambda _body, _request: (500, ["no object"])
    assert tool.run(*argv)[1]["reason"] == "response_invalid"
    # a second hub key header: the refusal does not verify
    hub.hooks[("GET", "/v1/stream")] = lambda _body, _request: (404, {"error": "not_found"}, {"Agent-Hub-Key": "x"})
    assert tool.run(*argv)[1]["reason"] == "response_header_invalid"
    del hub.hooks[("GET", "/v1/stream")]
    hub.stream_script = silent  # type: ignore[attr-defined]
    assert tool.run(*argv)[1]["error"] == "hub_unreachable"
    hub.stream_script = broken  # type: ignore[attr-defined]
    ended = tool.run(*argv, "--seconds", "5")[1]
    assert (ended["ended"], ended["browser_id"]) == ("stream_closed", B)
