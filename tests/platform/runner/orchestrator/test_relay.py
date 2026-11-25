"""U-RUN-orchestrator relay: the journal handler `hub.relay` (05-runner.md section 17.2; ADR-082 sections 6.5, 9.7).

The handler runs through the real journal consumer (`VmJournal`) on the migrated store; the hub is an
`httpx.MockTransport` that records what reached it, the guest a double that records the answers. The rules of `parse`
are checked against the contract fixtures of `guest/journal/hub.relay.json`.

Attack cases (KOR-57): a record of a session without an orchestrator pairing, of another VM's session, of an ended
session, or without a daemon is answered `refused` and never reaches the hub.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from agent_runner.encoding import b64u_encode
from agent_runner.guestlink.client import JournalRecord
from agent_runner.guestlink.errors import GuestUnreachable
from agent_runner.orchestrator import journal, store
from agent_runner.orchestrator.env import install
from agent_runner.sessions.consume import VmJournal
from agent_runner.sessions.journal import JournalRecordInvalid

from .kit import HUB_URL, OTHER, OTHER_VM, VM, Kit, principal, run, session_id

ROOT = Path(__file__).resolve().parents[4]
FIXTURES = ROOT / "contracts" / "fixtures" / "guest" / "journal" / "hub.relay"
PENDING = "0193000a-0000-7000-8000-00000000000a"
PAIRING = "0193000a-0000-7000-8000-00000000000b"
AGENT_KEY = "agent.0193000a00007000800000000000000b"
"""The key ID of `PAIRING`: only the orchestrator's own key is relayed (review KOR-57 b no. 5)."""
SIG = "A" * 86
HEADERS = {
    "Agent-Key": AGENT_KEY,
    "Agent-Time": "2026-11-25T10:00:00Z",
    "Agent-Nonce": "AAECAwQFBgcICQoLDA0ODw",
    "Agent-Signature": SIG,
    "Content-Type": "application/json",
}
Json = dict[str, Any]


def relay_body(**over: Any) -> Json:
    body: Json = {
        "pending_id": PENDING,
        "method": "POST",
        "target": "/v1/agent/claim",
        "headers": dict(HEADERS),
        "body": b64u_encode(b'{"claim":"x"}'),
    }
    body.update(over)
    return body


# -- parse -------------------------------------------------------------------------------------------------------


def test_parse_takes_a_signed_agent_request() -> None:
    relay = journal.parse(relay_body())
    assert (relay.pending_id, relay.method, relay.target, relay.body) == (
        PENDING,
        "POST",
        "/v1/agent/claim",
        b'{"claim":"x"}',
    )
    assert relay.headers == HEADERS
    get = journal.parse(relay_body(method="GET", target="/v1/me?x=1", body="", headers={**HEADERS}))
    assert get.body == b""


@pytest.mark.parametrize(
    "over",
    [
        {"extra": 1},
        {"pending_id": "nope"},
        {"method": "CONNECT"},
        {"target": "https://other.example/v1/me"},
        {"target": "/v2/me"},
        {"target": "/v1/../device/state"},
        {"target": "/v1/a/./b"},
        {"target": "/v1//device"},
        {"target": "/v1/me#x"},
        {"target": "/v1/" + "a" * journal.MAX_TARGET},
        {"target": 3},
        {"headers": {k: v for k, v in HEADERS.items() if k != "Agent-Signature"}},
        {"headers": {**HEADERS, "Cookie": "a=b"}},
        {"headers": {**HEADERS, "Agent-Key": "device.0193000a00007000800000000000000b"}},
        {"headers": {**HEADERS, "Content-Type": "text/plain"}},
        {"headers": {**HEADERS, "Agent-Time": 1}},
        {"headers": "x"},
        {"body": 1},
        {"body": "e30="},
    ],
    ids=str,
)
def test_parse_refuses_everything_else(over: Json) -> None:
    with pytest.raises(JournalRecordInvalid):
        journal.parse(relay_body(**over))


def test_parse_agrees_with_the_contract_fixtures() -> None:
    for path in sorted((FIXTURES / "valid").glob("*.json")):
        journal.parse(json.loads(path.read_text(encoding="utf-8")))
    invalid = sorted((FIXTURES / "invalid").glob("*.json"))
    assert invalid
    for path in invalid:
        with pytest.raises(JournalRecordInvalid):
            journal.parse(json.loads(path.read_text(encoding="utf-8")))


# -- the handler -------------------------------------------------------------------------------------------------


class HubDouble:
    def __init__(self, response: httpx.Response | Exception | None = None) -> None:
        self.seen: list[httpx.Request] = []
        self.response = response

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response or httpx.Response(
            200,
            stream=httpx.ByteStream(b'{"ok":true}'),
            headers={
                "Agent-Hub-Key": "hub-1",
                "Agent-Hub-Time": "2026-11-25T10:00:01Z",
                "Agent-Hub-Signature": SIG,
                "Content-Type": "application/json",
                "Set-Cookie": "a=b",
            },
        )


async def orchestrator_session(
    k: Kit, *, row: bool = True, state: str = "idle", vm_id: str = VM, pairing: str = PAIRING
) -> str:
    await k.add_vm()
    if vm_id != VM:
        await k.add_vm(vm_id)
    sid = await k.add_session(state=state, vm_id=vm_id)
    if row:
        async with k.env.sessions.transaction() as conn:
            await store.register(conn, sid, pairing, k.env.now())
    return sid


async def relay(k: Kit, sid: str, hub: HubDouble, *, context: Any = "env", **over: Any) -> Json:
    """One `hub.relay` record through the journal consumer of the VM; the answer the guest got."""
    k.env.transport = httpx.MockTransport(hub)
    if context == "env":
        context = SimpleNamespace()
        install(context, k.env)
    k.env.sessions.context = context
    k.guest.answered.clear()
    record = JournalRecord(
        seq=1, ts="2026-11-25T10:00:00.000Z", session_id=sid, kind=journal.KIND, body=relay_body(**over)
    )
    consumer = VmJournal(k.env.sessions, await k.vm(), handlers=journal.HANDLERS)
    await consumer.handle([record])
    await asyncio.wait_for(k.guest.answered.wait(), 10)
    [(pending, answer)] = k.guest.of("relay")[-1:]
    assert pending == over.get("pending_id", PENDING)
    return dict(answer)


def test_a_relay_of_the_orchestrator_session_reaches_the_hub_unchanged(store_dsn: str) -> None:
    async def body(k: Kit) -> None:
        sid = await orchestrator_session(k)
        hub = HubDouble()
        answer = await relay(k, sid, hub)
        [request] = hub.seen
        assert (request.method, str(request.url)) == ("POST", HUB_URL + "/v1/agent/claim")
        assert request.content == b'{"claim":"x"}'
        for name, value in HEADERS.items():
            assert request.headers[name] == value
        assert request.headers["Accept-Encoding"] == "identity"
        assert answer == {
            "status": 200,
            "headers": {
                "Agent-Hub-Key": "hub-1",
                "Agent-Hub-Time": "2026-11-25T10:00:01Z",
                "Agent-Hub-Signature": SIG,
                "Content-Type": "application/json",
            },
            "body": b64u_encode(b'{"ok":true}'),
        }
        # handed over: still relayed; an overlong header is not passed on
        async with k.env.sessions.transaction() as conn:
            await store.handed_over(conn, sid, k.env.now())
        long = HubDouble(
            httpx.Response(
                401, stream=httpx.ByteStream(b"{}"), headers={"Agent-Hub-Key": "k" * 300, "Retry-After": "5"}
            )
        )
        answer = await relay(k, sid, long, pending_id=session_id(30))
        assert answer == {"status": 401, "headers": {"Retry-After": "5"}, "body": "e30"}
        # the same pending call read again is not sent twice
        await VmJournal(k.env.sessions, await k.vm(), handlers=journal.HANDLERS).handle(
            [
                JournalRecord(
                    seq=2, ts="t", session_id=sid, kind=journal.KIND, body=relay_body(pending_id=session_id(30))
                )
            ]
        )
        await asyncio.sleep(0.05)
        assert len(long.seen) == 1

    run(store_dsn, body)


@pytest.mark.parametrize(
    "case",
    ["no_pairing", "other_vm", "ended", "no_daemon", "foreign_key", "revoked", "team_org", "other_user", "org_device"],
)
def test_a_relay_of_any_other_session_is_refused_and_never_reaches_the_hub(store_dsn: str, case: str) -> None:
    async def body(k: Kit) -> None:
        sid = await orchestrator_session(
            k,
            row=case != "no_pairing",
            state="stopped" if case == "ended" else "idle",
            vm_id=OTHER_VM if case == "other_vm" else VM,
            pairing=session_id(7) if case == "foreign_key" else PAIRING,  # another agent's key signs the request
        )
        if case == "revoked":
            async with k.env.sessions.transaction() as conn:
                assert await store.ended(conn, sid, PAIRING)
        # review KOR-57 b no. 3: the organization must still fit in the verified log at every relay
        k.book.state.private = case != "team_org"
        k.book.org_device = case == "org_device"
        if case == "other_user":
            k.book.state.members[OTHER] = principal("bob")
        hub = HubDouble()
        answer = await relay(k, sid, hub, context=None if case == "no_daemon" else "env")
        assert answer == {"error": "refused"}
        assert hub.seen == []
        assert any("relay_refused" in line for line in k.lines)

    run(store_dsn, body)


def test_limits_and_transport_failures(store_dsn: str, monkeypatch: pytest.MonkeyPatch) -> None:
    async def body(k: Kit) -> None:
        sid = await orchestrator_session(k)
        hub = HubDouble()
        large = b64u_encode(b"x" * (journal.MAX_BODY + 1))
        assert await relay(k, sid, hub, body=large, pending_id=session_id(31)) == {"error": "too_large"}
        assert hub.seen == []
        monkeypatch.setattr(journal, "MAX_RESPONSE", 4)
        assert await relay(k, sid, hub, pending_id=session_id(32)) == {"error": "too_large"}
        monkeypatch.setattr(journal, "MAX_RESPONSE", 8 * 1024 * 1024)
        broken = HubDouble(httpx.ConnectError("refused"))
        assert await relay(k, sid, broken, pending_id=session_id(33)) == {"error": "hub_unreachable"}
        assert any("relay_unreachable error=ConnectError" in line for line in k.lines)
        consumed = HubDouble(httpx.Response(200, content=b"{}"))  # a body read already: an unexpected failure
        assert await relay(k, sid, consumed, pending_id=session_id(35)) == {"error": "hub_unreachable"}
        assert any("relay_failed error=StreamConsumed" in line for line in k.lines)
        monkeypatch.setattr(journal, "MAX_RELAYS_PER_SESSION", 0)
        assert await relay(k, sid, hub, pending_id=session_id(34)) == {"error": "refused"}
        assert any("reason=busy" in line for line in k.lines)

    run(store_dsn, body)


def test_answers_are_retried_and_the_memory_of_taken_calls_is_bounded(
    store_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(k: Kit) -> None:
        sid = await orchestrator_session(k)
        hub = HubDouble()
        k.guest.fail["relay"] = GuestUnreachable("relay")
        assert (await relay(k, sid, hub, pending_id=session_id(40)))["status"] == 200
        assert any("relay_answer_failed" in line and "attempt=1" in line for line in k.lines)
        # an answer that never arrives ends after the attempts
        k.guest.failing.add("relay")
        k.env.transport = httpx.MockTransport(hub)
        k.env.sessions.context = SimpleNamespace()
        install(k.env.sessions.context, k.env)
        relays = journal.relays_for(k.env, None)
        task = relays.spawn(
            k.env,
            await k.vm(),
            journal.parse(relay_body(pending_id=session_id(41))),
            session_id=sid,
            sessions=k.env.sessions,
            allowed=True,
        )
        assert task is not None
        await task
        assert sum("relay_answer_failed" in line for line in k.lines) == 1 + journal.ANSWER_ATTEMPTS
        monkeypatch.setattr(journal, "PENDING_MEMORY", 1)
        k.guest.failing.clear()
        for n in (42, 43):
            task = relays.spawn(
                k.env,
                await k.vm(),
                journal.parse(relay_body(pending_id=session_id(n))),
                session_id=sid,
                sessions=k.env.sessions,
                allowed=False,
            )
            assert task is not None
            await task
        assert list(relays.answered) == [session_id(43)]
        assert relays_of_holder() is not None

    def relays_of_holder() -> journal.Relays:
        holder = SimpleNamespace()
        first = journal.relays_for(None, holder)
        assert journal.relays_for(None, holder) is first
        return first

    run(store_dsn, body)


def test_the_handler_is_registered() -> None:
    assert {"hub.relay": journal.hub_relay} == journal.HANDLERS
