"""U-RUN-guestlink client: the control protocol of 03-api.md section 7.1 over a real Unix socket (GST-01 guest), and
the request shapes and failure classes of every call (httpx mock transport for the endpoints later packages add to
the guest)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from agent_runner.guestlink.client import GuestClient, Health, JournalPage
from agent_runner.guestlink.errors import (
    GuestError,
    GuestProtocolError,
    GuestRefused,
    GuestUnauthorized,
    GuestUnreachable,
)

from .conftest import BOOT_ID, Guest

VM = "vm-0a1b2c3d"
ORG = "01930001-0000-7000-8000-000000000001"
WG = "01930002-0000-7000-8000-000000000002"
SESSION = "01a1ee56-5e00-7003-8f1b-340691d99f03"


def run(coro: Any) -> Any:
    return asyncio.run(coro)


# -- real guest -------------------------------------------------------------------------------------------------


def test_health_and_assign(guest: Guest) -> None:
    async def body() -> Any:
        async with GuestClient(guest.socket, guest.token) as client:
            before = await client.health()
            await client.assign(VM, ORG, WG)
            await client.assign(VM, ORG, WG)  # the same assignment again is accepted
            with pytest.raises(GuestRefused) as refused:
                await client.assign(VM, ORG, "01930002-0000-7000-8000-000000000003")
            return before, await client.health(timeout=10), refused.value

    before, after, refused = run(body())
    assert before == Health(state="unassigned", guest_version=before.guest_version, claude_version="2.1.330",
                            landlock_abi=4, boot_id=BOOT_ID, org_id=None, wg_id=None, sessions=())  # fmt: skip
    assert (after.state, after.org_id, after.wg_id) == ("assigned", ORG, WG)
    assert (refused.code, refused.name, refused.status, refused.error) == (
        "guest_refused",
        "assign",
        409,
        "already_assigned",
    )


def test_wrong_token_and_missing_socket(guest: Guest) -> None:
    async def body() -> Any:
        async with GuestClient(guest.socket, "wrong") as client:
            with pytest.raises(GuestUnauthorized) as unauthorized:
                await client.health()
        async with GuestClient(guest.socket + ".none", guest.token) as client:
            with pytest.raises(GuestUnreachable) as unreachable:
                await client.health()
        return unauthorized.value, unreachable.value

    unauthorized, unreachable = run(body())
    assert (unauthorized.code, unauthorized.status) == ("guest_unauthorized", 401)
    assert (unreachable.code, unreachable.name, unreachable.status) == ("guest_unreachable", "health", None)
    # The token is never part of an error.
    assert guest.token not in str(unauthorized) + str(unreachable)


def test_journal_long_poll_and_ack(guest: Guest) -> None:
    for code in ("gateway_route", "recorder_failed", "scanner_failed"):
        guest.context.journal.append("guest.error", {"code": code})

    async def body() -> Any:
        async with GuestClient(guest.socket, guest.token) as client:
            page = await client.journal(0, limit=2, wait=0)
            await client.ack(page.records[-1].seq)
            rest = await client.journal(page.records[-1].seq, wait=99)
            with pytest.raises(GuestRefused) as beyond:
                await client.ack(rest.head + 5)
            waiter = asyncio.ensure_future(client.journal(rest.head, wait=5))
            await asyncio.sleep(0.2)
            guest.context.journal.append("guest.error", {"code": "pending_expired"})
            late = await asyncio.wait_for(waiter, 10)
            return page, rest, beyond.value, late

    page, rest, beyond, late = run(body())
    assert isinstance(page, JournalPage)
    assert [r.body["code"] for r in page.records] == ["gateway_route", "recorder_failed"]
    assert page.records[0].kind == "guest.error"
    assert page.records[0].session_id is None
    assert page.head == 3
    assert [r.body["code"] for r in rest.records] == ["scanner_failed"]
    assert beyond.error == "state_conflict"
    assert [r.body["code"] for r in late.records] == ["pending_expired"]


def test_pending_and_prompts_of_unknown_ids(guest: Guest) -> None:
    async def body() -> Any:
        async with GuestClient(guest.socket, guest.token) as client:
            with pytest.raises(GuestRefused) as pending:
                await client.answer_pending(
                    "0193aaaa-0000-7000-8000-000000000001",
                    {"status": "error", "error": {"error": "timeout", "message": "Timed out."}},
                )
            with pytest.raises(GuestRefused) as prompt:
                await client.register_prompt(SESSION, "0193aaaa-0000-7000-8000-000000000002", "hello")
            return pending.value.error, prompt.value.error

    assert run(body()) == ("not_found", "not_found")


# -- mock transport ---------------------------------------------------------------------------------------------


def mock(handler: Callable[[httpx.Request], httpx.Response]) -> GuestClient:
    return GuestClient("/nonexistent.sock", "tok", transport=httpx.MockTransport(handler))


def recorder(response: httpx.Response | None = None) -> tuple[list[httpx.Request], GuestClient]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return response or httpx.Response(200, json={})

    return seen, mock(handler)


def test_session_endpoints_send_the_documented_requests() -> None:
    seen, client = recorder()

    async def body() -> None:
        async with client:
            assert await client.create_session({"session_id": SESSION}) == {}
            await client.put_credential(SESSION, {"auth_header": "x-api-key"})
            await client.register_prompt(SESSION, "p1", "text")
            await client.control(SESSION, "suspend")
            await client.register_prompt(SESSION, "p2", "more", "agent.dfx.swiss instruction from: x")
            await client.ping(SESSION, {"ping_id": "q", "kind": "whiteboard.entry"})
            await client.answer_pending("pend-1", {"status": "error", "error": {}})
            assert await client.metrics() == {}

    run(body())
    assert [(r.method, r.url.path) for r in seen] == [
        ("POST", "/v1/sessions"),
        ("PUT", f"/v1/sessions/{SESSION}/credential"),
        ("POST", f"/v1/sessions/{SESSION}/prompts"),
        ("POST", f"/v1/sessions/{SESSION}/control"),
        ("POST", f"/v1/sessions/{SESSION}/prompts"),
        ("POST", f"/v1/sessions/{SESSION}/pings"),
        ("POST", "/v1/pending/pend-1/answer"),
        ("GET", "/v1/metrics"),
    ]
    assert all(r.headers["Authorization"] == "Bearer tok" for r in seen)
    assert json.loads(seen[2].content) == {"prompt_id": "p1", "text": "text"}
    assert json.loads(seen[3].content) == {"action": "suspend"}
    # the sender line goes with the registration only when there is one (ADR-083 section 1)
    speaker = "agent.dfx.swiss instruction from: x"
    assert json.loads(seen[4].content) == {"prompt_id": "p2", "text": "more", "speaker": speaker}


def test_files_import_and_export() -> None:
    manifest = "a" * 64
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/import"):
            return httpx.Response(200, json={"files": 1, "bytes": 3})
        return httpx.Response(200, content=b"tar-bytes", headers={"X-Manifest-Sha256": manifest})

    async def body() -> Any:
        chunks: list[bytes] = []
        async with mock(handler) as client:
            imported = await client.import_files(SESSION, "work", b"abc", replace=True)
            got = await client.export_files(SESSION, "work", chunks.append, paths=["a.txt"])
            await client.export_files(SESSION, "out", chunks.append)
        return imported, got, b"".join(chunks)

    imported, got, data = run(body())
    assert imported == {"files": 1, "bytes": 3}
    assert got == manifest
    assert data == b"tar-bytestar-bytes"
    assert seen[0].url.params["dir"] == "work"
    assert seen[0].url.params["replace"] == "true"
    assert seen[0].headers["Content-Type"] == "application/x-tar"
    assert seen[0].content == b"abc"
    assert json.loads(seen[1].content) == {"dir": "work", "paths": ["a.txt"]}
    assert json.loads(seen[2].content) == {"dir": "out"}


def test_files_import_streams_with_its_length() -> None:
    # The guest takes no chunked body: a streamed import carries Content-Length and is sent piece by piece (A-27).
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"files": 1, "bytes": 6})

    async def pieces() -> Any:
        for piece in (b"ab", b"cd", b"ef"):
            yield piece

    async def body() -> Any:
        async with mock(handler) as client:
            imported = await client.import_files(SESSION, "work", pieces(), size=6)
            with pytest.raises(ValueError, match="size"):
                await client.import_files(SESSION, "work", pieces())
            with pytest.raises(ValueError, match="size"):
                await client.import_files(SESSION, "work", pieces(), size=-1)
        return imported

    assert run(body()) == {"files": 1, "bytes": 6}
    assert seen[0].headers["Content-Length"] == "6"
    assert "Transfer-Encoding" not in seen[0].headers
    assert seen[0].content == b"abcdef"
    assert len(seen) == 1


def test_export_failures() -> None:
    async def body(response: httpx.Response | Exception) -> Any:
        def handler(_request: httpx.Request) -> httpx.Response:
            if isinstance(response, Exception):
                raise response
            return response

        async with mock(handler) as client:
            with pytest.raises(GuestError) as caught:
                await client.export_files(SESSION, "work", lambda _b: None)
            return caught.value

    refused = run(body(httpx.Response(404, json={"error": "not_found"})))
    assert isinstance(refused, GuestRefused)
    assert refused.error == "not_found"
    assert isinstance(run(body(httpx.Response(200, content=b"x"))), GuestProtocolError)
    assert isinstance(run(body(httpx.ConnectError("refused"))), GuestUnreachable)


def test_error_bodies_and_bad_answers() -> None:
    cases: list[tuple[httpx.Response, type[Exception], str | None]] = [
        (httpx.Response(500, content=b"not json"), GuestRefused, "unknown"),
        (httpx.Response(400, json=["list"]), GuestRefused, "unknown"),
        (httpx.Response(400, json={"error": "Bad Code!"}), GuestRefused, "unknown"),
        (httpx.Response(503, json={"error": "unavailable"}), GuestRefused, "unavailable"),
        (httpx.Response(200, content=b"<html>"), GuestProtocolError, None),
        (httpx.Response(200, json=[1]), GuestProtocolError, None),
    ]
    for response, kind, error in cases:
        _, client = recorder(response)

        async def body(client: GuestClient = client, kind: type[Exception] = kind) -> Exception:
            async with client:
                with pytest.raises(kind) as caught:
                    await client.metrics()
                return caught.value

        caught = run(body())
        assert getattr(caught, "error", None) == error


def test_timeout_is_unreachable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    async def body() -> Any:
        async with mock(handler) as client:
            with pytest.raises(GuestUnreachable) as caught:
                await client.journal(0)
            return caught.value.name

    assert run(body()) == "journal"


@pytest.mark.parametrize(
    "body",
    [
        {
            "state": "odd",
            "guest_version": "1",
            "claude_version": "1",
            "boot_id": "b",
            "landlock_abi": 4,
            "sessions": [],
        },
        {
            "state": "assigned",
            "guest_version": 1,
            "claude_version": "1",
            "boot_id": "b",
            "landlock_abi": 4,
            "sessions": [],
        },
        {
            "state": "assigned",
            "guest_version": "1",
            "claude_version": "1",
            "boot_id": "b",
            "landlock_abi": 4,
            "sessions": [],
            "wg_id": 5,
        },
        {
            "state": "assigned",
            "guest_version": "1",
            "claude_version": "1",
            "boot_id": "b",
            "landlock_abi": True,
            "sessions": [],
        },
        {"state": "assigned", "guest_version": "1", "claude_version": "1", "boot_id": "b", "landlock_abi": 4},
        {
            "state": "assigned",
            "guest_version": "1",
            "claude_version": "1",
            "boot_id": "b",
            "landlock_abi": 4,
            "sessions": ["x"],
        },
        {
            "state": "assigned",
            "guest_version": "1",
            "claude_version": "1",
            "boot_id": "b",
            "landlock_abi": 4,
            "sessions": [{"session_id": "s", "state": 1}],
        },
    ],
)
def test_invalid_health(body: dict[str, Any]) -> None:
    with pytest.raises(GuestProtocolError):
        Health.parse(body)


def test_health_with_sessions() -> None:
    health = Health.parse(
        {
            "state": "assigned",
            "guest_version": "1",
            "claude_version": "2",
            "boot_id": "b",
            "landlock_abi": 4,
            "org_id": ORG,
            "wg_id": WG,
            "sessions": [{"session_id": SESSION, "state": "working"}],
        }
    )
    assert health.sessions[0].session_id == SESSION
    assert health.sessions[0].state == "working"


@pytest.mark.parametrize(
    "answer",
    [
        {"records": {}, "head": 0},
        {"records": [], "head": "0"},
        {"records": ["x"], "head": 1},
        {"records": [{"seq": "1", "ts": "t", "session_id": None, "kind": "k", "body": {}}], "head": 1},
        {"records": [{"seq": 1, "ts": 1, "session_id": None, "kind": "k", "body": {}}], "head": 1},
        {"records": [{"seq": 1, "ts": "t", "session_id": 1, "kind": "k", "body": {}}], "head": 1},
        {"records": [{"seq": 1, "ts": "t", "session_id": None, "kind": 1, "body": {}}], "head": 1},
        {"records": [{"seq": 1, "ts": "t", "session_id": None, "kind": "k", "body": []}], "head": 1},
    ],
)
def test_invalid_journal_answers(answer: dict[str, Any]) -> None:
    _, client = recorder(httpx.Response(200, json=answer))

    async def body() -> None:
        async with client:
            with pytest.raises(GuestProtocolError):
                await client.journal(0)

    run(body())


def test_journal_parameters() -> None:
    seen, client = recorder(httpx.Response(200, json={"records": [], "head": 0}))

    async def body() -> None:
        async with client:
            await client.journal(7, limit=50, wait=-3)
            await client.journal(7)

    run(body())
    assert dict(seen[0].url.params) == {"after": "7", "limit": "50", "wait": "0"}
    assert dict(seen[1].url.params) == {"after": "7", "limit": "200", "wait": "25"}


def test_ids_and_actions_are_checked_before_sending() -> None:
    seen, client = recorder()

    async def body() -> None:
        async with client:
            for call in (
                client.put_credential("../x", {}),
                client.register_prompt("a/b", "p", "t"),
                client.control(SESSION, "kill"),
                client.control("", "stop"),
                client.ping("x y", {}),
                client.import_files("..", "d", b""),
                client.export_files("?", "d", lambda _b: None),
                client.answer_pending("a/../b", {}),
            ):
                with pytest.raises(GuestProtocolError):
                    await call

    run(body())
    assert seen == []


def test_context_export_and_stage() -> None:
    # KOR-33: the context archive streams out to `write`; a stage streams in with its length
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/context/export"):
            return httpx.Response(200, content=b"tar-bytes", headers={"Content-Type": "application/x-tar"})
        return httpx.Response(200, json={"files": 2, "bytes": 4})

    async def pieces() -> Any:
        for piece in (b"ab", b"cd"):
            yield piece

    chunks: list[bytes] = []

    async def body() -> Any:
        async with mock(handler) as client:
            await client.export_context(SESSION, chunks.append, workspace_max_bytes=5, transcript_max_bytes=6)
            return await client.stage_context(SESSION, pieces(), 4)

    assert run(body()) == {"files": 2, "bytes": 4}
    assert b"".join(chunks) == b"tar-bytes"
    assert seen[0].url.path == f"/v1/sessions/{SESSION}/context/export"
    assert json.loads(seen[0].content) == {"workspace_max_bytes": 5, "transcript_max_bytes": 6}
    assert seen[1].url.path == f"/v1/contexts/{SESSION}/stage"
    assert seen[1].headers["Content-Length"] == "4"
    assert seen[1].content == b"abcd"


def test_context_export_failures() -> None:
    async def body(response: httpx.Response | Exception) -> Any:
        def handler(_request: httpx.Request) -> httpx.Response:
            if isinstance(response, Exception):
                raise response
            return response

        async with mock(handler) as client:
            with pytest.raises(GuestError) as caught:
                await client.export_context(SESSION, lambda _b: None, workspace_max_bytes=0, transcript_max_bytes=1)
            return caught.value

    refused = run(body(httpx.Response(413, json={"error": "size_exceeded"})))
    assert isinstance(refused, GuestRefused)
    assert refused.error == "size_exceeded"
    assert isinstance(run(body(httpx.ConnectError("refused"))), GuestUnreachable)
