"""Units of the gateway: usage meter, session token, credential parsing and injection, store, policy, hand-off."""

from __future__ import annotations

import http.client
import json
import threading
from dataclasses import replace
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from agent_guest import gateway as gateway_module
from agent_guest import gateway_credentials, sessions
from agent_guest.apisock import ApiClient, ApiSockError
from agent_guest.catalog import Budget, ModelPolicy, policy_from
from agent_guest.config import GuestConfig
from agent_guest.control import ControlContext
from agent_guest.control_routes.credential import Handoff, handoff
from agent_guest.gateway import Gateway, UsageMeter, _Call, gateway_for, session_token
from agent_guest.gateway_credentials import (
    OAUTH_BETA,
    RESERVED_HEADERS,
    CredentialInvalid,
    CredentialStore,
    parse_credential,
)
from agent_guest.server import create_server
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID, ControlClient
from tests.platform.guest.gateway.conftest import DEEPSEEK, OPUS, ROUTER_KEY, credential_body


def sse(name: str, data: object) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


# ---------------------------------------------------------------------------------------------- usage meter


def test_meter_reads_split_sse_events_with_crlf() -> None:
    meter = UsageMeter(streaming=True)
    start = {
        "type": "message_start",
        "message": {"provider": "Nebius", "usage": {"input_tokens": 10, "cache_read_input_tokens": 3}},
    }
    delta = {
        "type": "message_delta",
        "usage": {"output_tokens": 9, "cost": 0.25, "output_tokens_details": {"thinking_tokens": 4}},
    }
    stream = (
        sse("message_start", start).replace(b"\n", b"\r\n")
        + sse("ping", {"type": "ping"})
        + sse("message_delta", delta)
    )
    for index in range(0, len(stream), 7):
        meter.feed(stream[index : index + 7])
    meter.finish()
    assert meter.tokens == {"input_tokens": 10, "cache_read_input_tokens": 3, "output_tokens": 9}
    assert (meter.thinking, meter.cost, meter.provider) == (4, 0.25, "Nebius")


@pytest.mark.parametrize(
    "event",
    [
        b"event: message_start\ndata: {not json\n\n",
        b"event: message_start\ndata: [1]\n\n",
        sse("message_start", {"type": "message_start", "message": "text"}),
        sse("message_start", {"type": "message_start", "message": {"usage": [1], "provider": "x" * 65}}),
        sse("message_delta", {"type": "message_delta", "usage": {"input_tokens": True, "output_tokens": -1}}),
        sse("message_delta", {"type": "message_delta", "usage": {"cost": True, "output_tokens_details": [1]}}),
        sse("message_delta", {"type": "message_delta", "usage": {"cost": -1.0, "output_tokens_details": {}}}),
        sse("message_delta", {"type": "message_delta", "usage": {"cost": "0.1"}}),
        sse("message_delta", {"type": "message_stop_message_delta"}),
    ],
)
def test_meter_ignores_malformed_usage(event: bytes) -> None:
    meter = UsageMeter(streaming=True)
    meter.feed(event)
    meter.finish()
    assert (meter.tokens, meter.thinking, meter.cost, meter.provider) == ({}, 0, None, None)


def test_meter_ignores_infinite_cost() -> None:
    meter = UsageMeter(streaming=False)
    meter.feed(b'{"usage": {"cost": 1e999}}')
    meter.finish()
    assert meter.cost is None


def test_meter_reads_many_events_of_one_chunk() -> None:
    meter = UsageMeter(streaming=True)
    deltas = b"".join(sse("content_block_delta", {"type": "content_block_delta"}) for _ in range(500))
    meter.feed(deltas + sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": 3}}) + b"event: x")
    assert meter.tokens == {"output_tokens": 3}
    assert bytes(meter._buffer) == b"event: x"


def test_meter_skips_an_oversized_event(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_module, "MAX_SSE_EVENT", 64)
    meter = UsageMeter(streaming=True)
    meter.feed(b"event: content_block_delta\ndata: " + b"x" * 100)
    meter.feed(b"y" * 10 + b"\n\n")
    meter.feed(sse("message_delta", {"type": "message_delta", "usage": {"output_tokens": 2}}))
    assert meter.tokens == {"output_tokens": 2}


def test_meter_json_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    meter = UsageMeter(streaming=False)
    meter.feed(b'{"provider": "Fireworks", "usage": {"input_tokens": 1')
    meter.feed(b', "output_tokens": 2}}')
    meter.finish()
    assert (meter.tokens, meter.provider) == ({"input_tokens": 1, "output_tokens": 2}, "Fireworks")
    broken = UsageMeter(streaming=False)
    broken.feed(b"<html>")
    broken.finish()
    assert broken.tokens == {}
    monkeypatch.setattr(gateway_module, "MAX_USAGE_JSON", 8)
    large = UsageMeter(streaming=False)
    large.feed(b'{"usage": {"input_tokens": 1}}')
    large.feed(b"{}")
    large.finish()
    assert large.tokens == {}


# ---------------------------------------------------------------------------------------------- session token


def headers(*pairs: tuple[str, str]) -> Message:
    message = Message()
    for name, value in pairs:
        message[name] = value
    return message


def test_session_token_rules() -> None:
    assert session_token(headers(("Authorization", "bearer abc")), api_key=False) == "abc"
    assert session_token(headers(("x-api-key", "abc")), api_key=False) is None
    assert session_token(headers(("x-api-key", " abc ")), api_key=True) == "abc"
    assert session_token(headers(("Authorization", "Bearer abc"), ("x-api-key", "abc")), api_key=True) == "abc"
    assert session_token(headers(("Authorization", "Bearer abc"), ("x-api-key", "xyz")), api_key=True) is None
    assert session_token(headers(("Authorization", "Token abc")), api_key=True) is None
    assert session_token(headers(), api_key=True) is None


# ---------------------------------------------------------------------------------------------- credentials


def test_reserved_headers_match_the_contract(contracts_dir: Path) -> None:
    index = json.loads((contracts_dir / "guest" / "INDEX.json").read_text(encoding="utf-8"))
    assert set(index["reserved_headers"]) == RESERVED_HEADERS


def test_credential_repr_hides_the_secret() -> None:
    credential = parse_credential(credential_body("platform", "https://openrouter.example/api/"))
    assert ROUTER_KEY not in repr(credential)
    assert repr(credential) == "Credential(access='platform', upstream=https://openrouter.example, auth=<redacted>)"
    assert (credential.base_path, credential.port) == ("/api", None)


@pytest.mark.parametrize(
    ("change", "pointer"),
    [
        ({"upstream_base_url": "https://user:pw@openrouter.example/api"}, "/upstream_base_url"),
        ({"upstream_base_url": "https://openrouter.example/api?x=1"}, "/upstream_base_url"),
        ({"upstream_base_url": "https://openrouter.example/api#f"}, "/upstream_base_url"),
        ({"upstream_base_url": "https://openrouter.example:99999/api"}, "/upstream_base_url"),
        ({"upstream_base_url": "https://:443/api"}, "/upstream_base_url"),
        ({"extra_headers": [{"name": "anthropic-beta", "value": OAUTH_BETA}]}, "/extra_headers/0/name"),
        ({"extra_headers": [{"name": "cookie", "value": "a"}]}, "/extra_headers/0/name"),
        ({"extra_headers": [{"name": "x-a", "value": "1"}, {"name": "x-a", "value": "2"}]}, "/extra_headers/1/name"),
        ({"auth_header": "x-api-key"}, "/auth_header"),
        ({"auth_value": "test-router-key-without-scheme"}, "/auth_header"),
    ],
)
def test_credential_rules_beyond_the_schema(change: dict[str, Any], pointer: str) -> None:
    with pytest.raises(CredentialInvalid) as caught:
        parse_credential({**credential_body("platform", "https://openrouter.example/api"), **change})
    assert caught.value.pointer == pointer


def test_upstream_headers_per_kind() -> None:
    incoming = [
        ("Authorization", "Bearer session"),
        ("anthropic-beta", "a, ,b"),
        ("X-Title", "spoof"),
        ("x-app", "cli"),
    ]
    extra = [{"name": "x-title", "value": "agent.dfx.swiss"}]
    subscription = parse_credential(credential_body("subscription", "https://api.anthropic.example", extra=extra))
    sent = subscription.upstream_headers(incoming)
    assert ("anthropic-beta", f"a,b,{OAUTH_BETA}") in sent
    assert ("x-title", "agent.dfx.swiss") in sent
    assert ("X-Title", "spoof") not in sent
    assert sent[-1][0] == "authorization"
    assert [value for name, value in sent if name.lower() == "authorization"] == [sent[-1][1]]
    api_key = parse_credential(credential_body("api_key", "https://api.anthropic.example"))
    assert [name for name, _ in api_key.upstream_headers([("x-app", "cli")])] == ["x-app", "x-api-key"]
    assert ("anthropic-beta", "a,b") in api_key.upstream_headers(incoming)


def test_connection_listed_and_folded_headers_are_dropped() -> None:
    credential = parse_credential(credential_body("api_key", "https://api.anthropic.example"))
    incoming = [
        ("Connection", "keep-alive, X-Secret-Hop"),
        ("X-Secret-Hop", "1"),
        ("X-Folded", "a\r\n b"),
        ("x-app", "cli"),
    ]
    assert [name for name, _ in credential.upstream_headers(incoming)] == ["x-app", "x-api-key"]


def test_api_key_provisioning_needs_x_api_key() -> None:
    body = {**credential_body("api_key", "https://a.example"), "auth_header": "authorization", "auth_value": "Bearer k"}
    with pytest.raises(CredentialInvalid):
        parse_credential(body)


def test_store_charges_the_session_and_a_renewal_without_budget_starts_at_zero() -> None:
    store = CredentialStore()
    first = parse_credential(credential_body("platform", "https://openrouter.example/api"))
    second = parse_credential(credential_body("platform", "https://openrouter.example/api"))
    store.provide("s1", first)
    store.charge("s1", first, 0.5)
    store.charge("s2", first, 1.0)  # no credential: nothing to charge
    assert store.lookup("s1") == (first, 0.5)
    assert store._entries["s1"].charges == []  # without a budget no charge is kept for a carry-over (KOR-10)
    store.provide("s1", second)
    assert store.lookup("s1") == (second, 0.0)
    # a request that ends after the renewal is charged to the current entry (its cost is in no spent amount yet)
    store.charge("s1", first, 0.5)
    assert store.lookup("s1") == (second, 0.5)
    store.provide("s2", first)
    store.retain(["s2"])
    assert (store.sessions(), store.lookup("s1")) == (["s2"], None)


def _budgeted(limit: float, spent: float, journal_through: int | None = None) -> Any:
    budget: dict[str, Any] = {"usd_per_day": limit, "spent_usd": spent}
    if journal_through is not None:
        budget["journal_through"] = journal_through
    return parse_credential(credential_body("platform", "https://openrouter.example/api", budget=budget))


def test_a_renewal_keeps_the_cost_the_new_spent_amount_does_not_contain_yet() -> None:
    # AUD-01 A-29: old spent + observed and new spent are both lower bounds of the true spending; the larger counts.
    store = CredentialStore()
    store.provide("s1", _budgeted(10.0, 2.0))
    store.charge("s1", _budgeted(10.0, 2.0), 3.0)
    renewed = _budgeted(10.0, 4.0)  # the hub has counted 2.0 of the 3.0 so far
    store.provide("s1", renewed)
    assert store.lookup("s1") == (renewed, 1.0)
    assert not renewed.policy.within_budget(6.0)
    assert renewed.policy.within_budget(5.9)
    caught_up = _budgeted(10.0, 6.0)  # everything counted (and 1.0 spent elsewhere): nothing is counted twice
    store.provide("s1", caught_up)
    assert store.lookup("s1") == (caught_up, 0.0)
    # a smaller spent amount (a new day began): yesterday's observed cost is not carried into the new day
    store.charge("s1", caught_up, 0.25)
    next_day = _budgeted(10.0, 0.0)
    store.provide("s1", next_day)
    assert store.lookup("s1") == (next_day, 0.0)
    store.charge("s1", next_day, 0.3)
    later = _budgeted(10.0, 0.1)  # today's cost is carried again from now on
    store.provide("s1", later)
    found_later = store.lookup("s1")
    assert found_later is not None
    assert found_later[0] is later
    assert found_later[1] == pytest.approx(0.2)
    # without a budget before or after there is nothing to carry
    unlimited = parse_credential(credential_body("platform", "https://openrouter.example/api"))
    store.provide("s1", unlimited)
    assert store.lookup("s1") == (unlimited, 0.0)
    store.charge("s1", unlimited, 1.0)
    store.provide("s1", _budgeted(10.0, 0.0))
    found = store.lookup("s1")
    assert found is not None
    assert found[1] == 0.0


def test_a_renewal_keeps_the_own_uncounted_cost_of_each_session_when_others_spent() -> None:
    """KOR-10 (AUD-02 NA-10): the spent amount is the organization's. Old 4, this session's 3 not in the runner's
    outbox yet, others 5 counted meanwhile: new 9, and `old + observed - new` is negative; the session's own 3 is
    carried. Two sessions keep their own parts; a part is dropped once its `usage` record is at or below the
    `journal_through` of the provisioning (the runner took it into the spent amount)."""
    store = CredentialStore()
    old = _budgeted(20.0, 4.0, 0)
    store.provide("s1", old)
    store.provide("s2", old)
    one = store.charge("s1", old, 3.0)
    two = store.charge("s2", old, 1.0)
    assert one is not None
    assert two is not None
    store.journaled(one, 10)
    store.journaled(two, 11)
    renewed = _budgeted(20.0, 9.0, 9)
    store.provide("s1", renewed)
    assert store.lookup("s1") == (renewed, 3.0)  # before KOR-10: max(0, 4 + 3 - 9) = 0
    s2_renewed = _budgeted(20.0, 12.0, 10)  # the runner took s1's record in (its 3 is in the amount), not s2's
    store.provide("s2", s2_renewed)
    assert store.lookup("s2") == (s2_renewed, 1.0)
    caught_up = _budgeted(20.0, 13.0, 11)
    store.provide("s1", caught_up)
    store.provide("s2", caught_up)
    assert (store.lookup("s1"), store.lookup("s2")) == ((caught_up, 0.0), (caught_up, 0.0))
    # a charge not journaled yet is the session's own; one that could not be journaled is dropped (never twice)
    pending = store.charge("s1", caught_up, 0.5)
    failed = store.charge("s1", caught_up, 0.25)
    assert pending is not None
    assert failed is not None
    store.journaled(failed, None)
    again = _budgeted(20.0, 13.0, 99)
    store.provide("s1", again)
    assert store.lookup("s1") == (again, 0.75)  # the old bound: 13 + 0.75 - 13; own part 0.5
    store.journaled(pending, 100)
    store.provide("s1", _budgeted(20.0, 13.75, 100))
    found = store.lookup("s1")
    assert found is not None
    assert found[1] == 0.0
    # without journal_through only the old bound applies
    store.charge("s2", caught_up, 2.0)
    unknown = _budgeted(20.0, 15.0)
    store.provide("s2", unknown)
    assert store.lookup("s2") == (unknown, 0.0)
    # a new day clears the own parts too
    next_day = _budgeted(20.0, 0.0, 200)
    store.provide("s1", next_day)
    assert store.lookup("s1") == (next_day, 0.0)
    assert store.charge("unknown", again, 1.0) is None


def test_the_charges_of_a_session_are_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gateway_credentials, "MAX_CHARGES", 2)
    store = CredentialStore()
    budgeted = _budgeted(20.0, 0.0, 0)
    store.provide("s1", budgeted)
    for cost in (1.0, 2.0, 4.0):
        store.charge("s1", budgeted, cost)
    assert [charge.cost_usd for charge in store._entries["s1"].charges] == [2.0, 4.0]
    renewed = _budgeted(20.0, 0.0, 0)
    store.provide("s1", renewed)
    assert store.lookup("s1") == (renewed, 7.0)  # the old bound keeps everything observed; the own part is capped


# ---------------------------------------------------------------------------------------------- policy


def test_policy_from_minimal_and_full_bodies() -> None:
    minimal = policy_from(credential_body("api_key", "https://api.anthropic.example", models={OPUS: 1000}))
    assert minimal == ModelPolicy("api_key", {OPUS: 1000}, frozenset(), None)
    assert (minimal.context_window(OPUS), minimal.context_window(DEEPSEEK)) == (1000, None)
    assert not minimal.provider_outside("Any")
    assert minimal.within_budget(1e9)
    full = policy_from(
        credential_body("platform", "https://x.example", providers=["A"], budget={"usd_per_day": 2, "spent_usd": 1})
    )
    assert full.budget == Budget(2.0, 1.0)
    assert (full.provider_outside("A"), full.provider_outside("B"), full.provider_outside(None)) == (False, True, False)
    assert (full.within_budget(0.99), full.within_budget(1.0)) == (True, False)


# ---------------------------------------------------------------------------------------------- hand-off


def test_handoff_keeps_only_active_sessions() -> None:
    slots = Handoff()
    slots.offer("a", {"n": 1})
    slots.offer("a", {"n": 2})
    slots.offer("b", {"n": 3})
    assert slots.waiting() == ["a", "b"]
    assert slots.take("a", {"a"}) == {"n": 2}
    assert slots.take("a", {"a"}) is None
    assert slots.waiting() == []


def test_handoff_is_one_per_control_process(context: ControlContext) -> None:
    assert handoff(context) is handoff(context)


def test_credential_take_op_checks_its_arguments(api_client: ApiClient, session: Session) -> None:
    for args in ({}, {"session_id": 5}, {"session_id": SESSION_ID, "x": 1}):
        with pytest.raises(ApiSockError) as caught:
            api_client.call("credential.take", args)
        assert caught.value.code == "request_invalid"
    assert api_client.call("credential.take", {"session_id": SESSION_ID}) == {
        "credential": None,
        "active": [SESSION_ID],
    }


@pytest.mark.parametrize("state", ["stopped", None])
def test_provisioning_needs_an_active_session(
    state: str | None, control_client: ControlClient, context: ControlContext, session: Session
) -> None:
    if state is not None:
        context.state.save_session(replace(session, state=state))
    target = SESSION_ID if state else "01a1ee56-5e00-7003-8f1b-340691d99f04"
    reply = control_client.request(
        "PUT", f"/v1/sessions/{target}/credential", credential_body("api_key", "https://a.example")
    )
    assert (reply.status, reply.json()) == (404, {"error": "not_found"})
    assert handoff(context).waiting() == []


def test_provisioning_starts_the_agent_after_the_hand_off(
    control_client: ControlClient, context: ControlContext, session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def start(called: ControlContext, session_id: str) -> bool:
        assert (called, session_id) == (context, SESSION_ID)
        seen.append(handoff(context).waiting())
        return False

    monkeypatch.setattr(sessions, "credential_provisioned", start)
    body = credential_body("api_key", "https://a.example")
    for _ in range(2):
        assert control_client.request("PUT", f"/v1/sessions/{SESSION_ID}/credential", body).status == 200
    assert seen == [[SESSION_ID], [SESSION_ID]]


def test_provisioning_refusals(
    control_client: ControlClient, session: Session, context: ControlContext, started_agents: list[str]
) -> None:
    path = f"/v1/sessions/{SESSION_ID}/credential"
    body = credential_body("api_key", "https://a.example", extra=[{"name": "host", "value": "evil.example"}])
    reply = control_client.request("PUT", path, body)
    assert (reply.status, reply.json()) == (
        400,
        {"error": "request_invalid", "detail": {"pointers": ["/extra_headers/0/name"]}},
    )
    bad = {**credential_body("api_key", "https://a.example"), "auth_value": "Bearer a\r\nInjected: b"}
    reply = control_client.request("PUT", path, bad)
    assert reply.status == 400
    assert b"Injected" not in reply.body
    assert (
        control_client.request("PUT", path, credential_body("api_key", "https://a.example"), token="wrong").status
        == 401
    )
    assert handoff(context).waiting() == []


# ---------------------------------------------------------------------------------------------- process wiring


def test_gateway_is_one_per_api_process(tmp_path: Path) -> None:
    control = ApiClient(str(tmp_path / "api.sock"))
    assert gateway_for(control) is gateway_for(control)
    assert gateway_for(control) is not gateway_for(ApiClient(str(tmp_path / "api.sock")))


class BrokenJournal:
    """An ``ApiClient`` stand-in whose journal append fails."""

    def append(self, kind: str, body: dict[str, Any], session_id: str | None = None) -> int:
        raise ApiSockError("unavailable")


def test_journal_failure_is_logged_not_raised(capsys: pytest.CaptureFixture[str]) -> None:
    gateway = Gateway(BrokenJournal())  # type: ignore[arg-type]
    gateway._usage(_Call(SESSION_ID, OPUS, b"{}", True), 200, 10, UsageMeter(streaming=False), outside=False)
    assert capsys.readouterr().err == "agent-guest-api: journal usage failed: unavailable\n"


def test_control_unreachable(guest_config: GuestConfig, short_tmp: Path) -> None:
    server = create_server(replace(guest_config, api_port=0, api_socket=str(short_tmp / "gone.sock")))
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        for method, target, expected in (
            ("POST", "/model/v1/messages", 503),
            ("POST", "/model/v1/messages/count_tokens", 503),
            ("GET", "/model/v1/models", 503),
            ("POST", "/mcp", 503),
        ):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
            body = b"{}" if method == "POST" else None
            connection.request(method, target, body=body, headers={"Authorization": "Bearer token-0123456789"})
            response = connection.getresponse()
            data = json.loads(response.read())
            connection.close()
            assert response.status == expected
            assert data == {"error": "unavailable"} if target == "/mcp" else data["error"]["type"] == "api_error"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
