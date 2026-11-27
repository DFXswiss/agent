"""The model gateway on port 7700 against the real model double (DBL-01), 05-runner.md §10.2-§10.4.

Every access kind is checked by the double itself: it accepts a request only with exactly the injected credential
(``x-api-key`` / ``Authorization: Bearer`` / bearer plus ``anthropic-beta: oauth-2025-04-20``) and refuses any other
credential header, so ``state.ok`` proves the injection and the removal of the session token.
"""

from __future__ import annotations

import email
import http.client
import io
import json
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest

from agent_guest import gateway as gateway_module
from agent_guest import routing
from agent_guest.apisock import ApiClient
from agent_guest.control import ControlContext
from agent_guest.gateway import gateway_for, refusal_of
from agent_guest.state import Session
from tests.platform.guest.conftest import SESSION_ID, SESSION_TOKEN, ControlClient, make_session
from tests.platform.guest.gateway.conftest import (
    DEEPSEEK,
    KINDS,
    OPUS,
    SECRETS,
    Api,
    Started,
    all_journal_text,
    credential_body,
    journal_bodies,
    messages,
    sse_events,
    text_step,
)

MESSAGES = "/model/v1/messages?beta=true"
Provision = Callable[..., Any]
StartDouble = Callable[..., Started]


def usage_records(context: ControlContext) -> list[dict[str, Any]]:
    return [body for session_id, body in journal_bodies(context, "usage") if session_id == SESSION_ID]


def wait_for_usage(context: ControlContext, count: int) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        found = usage_records(context)
        if len(found) >= count:
            return found
        time.sleep(0.02)
    raise AssertionError(f"expected {count} usage records, found {usage_records(context)}")


# ---------------------------------------------------------------------------------------------- the three kinds


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_streaming_request_per_access_kind(
    kind: str, start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    started = start_double(kind, text_step("Hello there.", usage={"cache_creation_input_tokens": 4}))
    provision(kind, started.url, models={KINDS[kind][4]: 200_000})
    answer = api.request("POST", MESSAGES, messages(KINDS[kind][4]))
    assert answer.status == 200
    assert answer.headers["Content-Type"].startswith("text/event-stream")
    events = sse_events(answer.body)
    assert events[0][0] == "message_start"
    assert events[-1][0] == "message_stop"
    text = "".join(data["delta"]["text"] for name, data in events if name == "content_block_delta")
    assert text == "Hello there."
    state = started.client.state()
    assert state["ok"], state
    [request] = started.client.journal()
    assert (request["path"], request["query"]) == ("/v1/messages", "beta=true")
    [usage] = wait_for_usage(context, 1)
    final = next(data["usage"] for name, data in events if name == "message_delta")
    assert usage["model"] == KINDS[kind][4]
    assert usage["status"] == 200
    assert usage["input_tokens"] == final["input_tokens"]
    assert usage["output_tokens"] == final["output_tokens"]
    assert (usage["thinking_tokens"], usage["cache_write_tokens"]) == (0, 4)
    assert usage["context_window_tokens"] == 200_000
    assert usage["response_bytes"] == len(answer.body)
    assert usage["request_bytes"] == len(json.dumps(messages(KINDS[kind][4])).encode())
    assert usage["request_id"] == answer.headers["request-id"]
    if kind == "platform":
        assert usage["provider"] == "DeepInfra"
        assert usage["cost_usd"] == final["cost"]
    else:
        assert "provider" not in usage
        assert "cost_usd" not in usage
    assert "provider_outside_allowlist" not in usage
    journal_text = all_journal_text(context) + json.dumps(started.client.journal())
    for secret in SECRETS:
        assert secret not in journal_text


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_json_request_per_access_kind(
    kind: str, start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    started = start_double(kind, text_step("Plain.", usage={"cache_read_input_tokens": 11}))
    provision(kind, started.url)
    answer = api.request("POST", MESSAGES, messages(KINDS[kind][4], stream=False), via="x-api-key")
    assert answer.status == 200
    message = answer.json()
    assert message["content"] == [{"type": "text", "text": "Plain."}]
    assert started.client.state()["ok"]
    [usage] = wait_for_usage(context, 1)
    assert usage["input_tokens"] == message["usage"]["input_tokens"]
    assert usage["cache_read_tokens"] == 11
    assert usage["cache_write_tokens"] == 0
    assert usage["context_window_tokens"] == 1_000_000
    assert (usage.get("provider"), usage.get("cost_usd")) == (
        ("DeepInfra", message["usage"]["cost"]) if kind == "platform" else (None, None)
    )


def test_provider_outside_the_allow_list_is_flagged_not_blocked(
    start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    started = start_double("platform", text_step(), text_step("Second."), openrouter={"provider": "Elsewhere"})
    provision("platform", started.url, providers=["Fireworks", "Nebius"])
    assert api.request("POST", MESSAGES, messages(DEEPSEEK)).status == 200
    provision("platform", started.url, providers=["Elsewhere"])
    assert api.request("POST", MESSAGES, messages(DEEPSEEK)).status == 200
    first, second = wait_for_usage(context, 2)
    assert (first["provider"], first["provider_outside_allowlist"]) == ("Elsewhere", True)
    assert "provider_outside_allowlist" not in second


def test_count_tokens_is_forwarded_without_usage(
    start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    started = start_double("subscription")
    provision("subscription", started.url)
    answer = api.request("POST", "/model/v1/messages/count_tokens?beta=true", {"model": OPUS, "messages": []})
    assert answer.status == 200
    assert isinstance(answer.json()["input_tokens"], int)
    refused = api.request("POST", "/model/v1/messages/count_tokens", {"model": DEEPSEEK, "messages": []})
    assert (refused.status, refused.json()["error"]["type"]) == (400, "model_not_allowed")
    assert usage_records(context) == []


# ---------------------------------------------------------------------------------------------- header injection


def test_session_headers_cannot_reach_the_upstream(
    start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    """ATK-39: hop-by-hop, cookie and proxy headers are removed; provisioned extra headers win over the session's."""
    started = start_double(
        "platform",
        {
            "id": "checked",
            "expect": {
                "headers": {"x-title": "agent.dfx.swiss", "anthropic-version": "2023-06-01"},
                "headers_absent": ["cookie", "proxy-authorization", "x-api-key", "keep-alive"],
            },
            "reply": {"content": [{"type": "text", "text": "ok"}]},
        },
    )
    provision("platform", started.url, extra=[{"name": "x-title", "value": "agent.dfx.swiss"}])
    answer = api.request(
        "POST",
        MESSAGES,
        messages(DEEPSEEK),
        headers={
            "Cookie": "a=b",
            "Proxy-Authorization": "Basic eA==",
            "Keep-Alive": "timeout=5",
            "X-Title": "spoofed",
        },
    )
    assert answer.status == 200, answer.body
    assert started.client.state()["ok"], started.client.state()
    [request] = started.client.journal()
    assert request["headers"]["x-title"] == "agent.dfx.swiss"
    assert request["headers"]["host"].startswith("127.0.0.1:")
    assert request["headers"]["accept-encoding"] == "identity"


def test_subscription_beta_is_appended_once(start_double: StartDouble, provision: Provision, api: Api) -> None:
    started = start_double("subscription")
    provision("subscription", started.url)
    beta = "claude-code-20250219,oauth-2025-04-20"
    assert api.request("POST", MESSAGES, messages(OPUS), headers={"anthropic-beta": beta}).status == 200
    [request] = started.client.journal()
    assert request["headers"]["anthropic-beta"] == beta
    assert started.client.state()["ok"]


def test_api_key_access_without_betas_sends_none(start_double: StartDouble, provision: Provision, api: Api) -> None:
    started = start_double(
        "api_key",
        {
            "id": "a",
            "expect": {"headers_absent": ["anthropic-beta"]},
            "reply": {"content": [{"type": "text", "text": "k"}]},
        },
    )
    provision("api_key", started.url)
    connection = api.connection()
    connection.putrequest("POST", MESSAGES)
    data = json.dumps(messages(OPUS, stream=False)).encode()
    for name, value in (("Authorization", f"Bearer {SESSION_TOKEN}"), ("Content-Length", str(len(data)))):
        connection.putheader(name, value)
    connection.endheaders(data)
    assert connection.getresponse().status == 200
    connection.close()
    assert started.client.state()["ok"]


@pytest.mark.parametrize(
    "route",
    [
        ("/model/v1/models", "GET"),
        ("/model/v1/complete", "POST"),
        ("/model/v1/messages/../../v1/models", "GET"),
        ("/model//v1/messages", "POST"),
        ("/model", "PUT"),
        ("/model/api/hello", "POST"),
        ("/model/v1/messages", "DELETE"),
    ],
)
def test_other_routes_are_refused_and_reported(
    route: tuple[str, str], start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    """ATK-39: no other upstream path; 404 in the Anthropic shape and ``guest.error gateway_route``."""
    target, method = route
    started = start_double()
    provision("api_key", started.url)
    body = b"{}" if method in ("POST", "PUT") else None
    answer = api.request(method, target, raw=body)
    assert answer.status == 404
    assert answer.json()["error"]["type"] == "not_found_error"
    assert journal_bodies(context, "guest.error") == [(SESSION_ID, {"code": "gateway_route"})]
    assert started.client.journal() == []


def test_unknown_head_route_has_no_body(api: Api, session: Session, context: ControlContext) -> None:
    answer = api.request("HEAD", "/model/v1/models")
    assert (answer.status, answer.body) == (404, b"")
    assert int(answer.headers["Content-Length"]) > 0
    assert journal_bodies(context, "guest.error") == [(SESSION_ID, {"code": "gateway_route"})]


def test_unknown_route_without_token_is_not_journaled(api: Api, session: Session, context: ControlContext) -> None:
    answer = api.request("GET", "/model/v1/models", token=None)
    assert answer.status == 401
    assert answer.json()["error"]["type"] == "authentication_error"
    # the failed token check is journaled (ATS-02), the route is not
    assert journal_bodies(context, "guest.error") == [(None, {"code": "auth_failed"})]


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_hello_is_answered_locally(method: str, api: Api) -> None:
    answer = api.request(method, "/model/api/hello", token=None)
    assert answer.status == 200
    assert answer.body == (b"{}" if method == "GET" else b"")


# ---------------------------------------------------------------------------------------------- authentication


def two_header_request(api: Api, headers: list[tuple[str, str]]) -> int:
    connection = api.connection()
    data = json.dumps(messages(OPUS)).encode()
    connection.putrequest("POST", MESSAGES)
    for name, value in [*headers, ("Content-Length", str(len(data)))]:
        connection.putheader(name, value)
    connection.endheaders(data)
    status = connection.getresponse().status
    connection.close()
    return status


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [("Authorization", "Bearer wrong-token-0123456789")],
        [("Authorization", f"Basic {SESSION_TOKEN}")],
        [("Authorization", "Bearer ")],
        [("Authorization", f"Bearer {SESSION_TOKEN}"), ("Authorization", f"Bearer {SESSION_TOKEN}")],
        [("x-api-key", SESSION_TOKEN), ("x-api-key", SESSION_TOKEN)],
        [("Authorization", f"Bearer {SESSION_TOKEN}"), ("x-api-key", "another-token-0123456789")],
        [("x-api-key", "")],
    ],
)
def test_only_the_session_token_is_accepted(
    headers: list[tuple[str, str]], start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    started = start_double()
    provision("api_key", started.url)
    assert two_header_request(api, headers) == 401
    assert started.client.journal() == []
    assert usage_records(context) == []


def test_same_token_in_both_headers_is_accepted(start_double: StartDouble, provision: Provision, api: Api) -> None:
    started = start_double()
    provision("api_key", started.url)
    headers = [("Authorization", f"Bearer {SESSION_TOKEN}"), ("x-api-key", SESSION_TOKEN)]
    assert two_header_request(api, headers) == 200


def test_stopped_session_is_refused(
    start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    started = start_double()
    provision("api_key", started.url)
    context.state.save_session(make_session(state="stopped"))
    assert api.request("POST", MESSAGES, messages(OPUS)).status == 401
    assert started.client.journal() == []


# ---------------------------------------------------------------------------------------------- checks


def test_model_outside_the_allow_list_is_refused(
    start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    started = start_double("platform")
    provision("platform", started.url)
    answer = api.request("POST", MESSAGES, messages("anthropic/claude-opus-5-5"))
    assert answer.status == 400
    assert answer.json() == {
        "type": "error",
        "error": {"type": "model_not_allowed", "message": "The model is not allowed for this session."},
    }
    assert started.client.journal() == []
    [usage] = usage_records(context)
    assert (usage["status"], usage["model"], usage["context_window_tokens"]) == (400, "anthropic/claude-opus-5-5", 0)
    assert usage["response_bytes"] == len(answer.body)
    assert usage["request_id"].startswith("gw_")
    assert api.request("POST", MESSAGES, messages("not a model id")).status == 400
    assert len(usage_records(context)) == 1


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'{"model": 5}', b'{"messages": []}'])
def test_body_without_a_model_is_refused(
    raw: bytes, start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    started = start_double()
    provision("api_key", started.url)
    answer = api.request("POST", MESSAGES, raw=raw)
    assert answer.status == 400
    assert answer.json()["error"]["type"] == "invalid_request_error"
    assert started.client.journal() == []
    assert usage_records(context) == []


def test_missing_credential_is_reported(
    api: Api, session: Session, context: ControlContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gateway_module, "CREDENTIAL_WAIT_SECONDS", 0.3)  # the wait itself: test_credential_wait.py
    answer = api.request("POST", MESSAGES, messages(OPUS))
    assert answer.status == 503
    assert answer.json()["error"]["message"] == "Model access is not available for this session right now."
    assert journal_bodies(context, "guest.error") == [(SESSION_ID, {"code": "credential_missing"})]
    [usage] = usage_records(context)
    assert usage["status"] == 503


def test_budget_is_enforced(start_double: StartDouble, provision: Provision, api: Api, context: ControlContext) -> None:
    started = start_double("platform", text_step("one", cost=0.002), text_step("two", cost=0.002))
    provision("platform", started.url, budget={"usd_per_day": 1.0, "spent_usd": 1.0})
    answer = api.request("POST", MESSAGES, messages(DEEPSEEK))
    assert (answer.status, answer.json()["error"]["type"]) == (429, "budget_exceeded")
    provision("platform", started.url, budget={"usd_per_day": 0.003, "spent_usd": 0.0})
    assert api.request("POST", MESSAGES, messages(DEEPSEEK)).status == 200
    assert api.request("POST", MESSAGES, messages(DEEPSEEK)).status == 200
    assert api.request("POST", MESSAGES, messages(DEEPSEEK)).status == 429
    # a renewal whose spent amount does not contain this gateway's cost yet keeps that cost (AUD-01 A-29)
    provision("platform", started.url, budget={"usd_per_day": 0.003, "spent_usd": 0.0})
    assert api.request("POST", MESSAGES, messages(DEEPSEEK)).status == 429
    # the hub counted it: the limit is raised and the observed cost is not counted twice
    provision("platform", started.url, budget={"usd_per_day": 0.005, "spent_usd": 0.004})
    assert api.request("POST", MESSAGES, messages(DEEPSEEK)).status == 400  # double: no step left
    statuses = [usage["status"] for usage in wait_for_usage(context, 6)]
    assert statuses == [429, 200, 200, 429, 429, 400]


def test_refreshed_credential_replaces_the_old_one(
    start_double: StartDouble, provision: Provision, api: Api, control_client: ControlClient
) -> None:
    started = start_double("api_key", text_step("a"), text_step("b"))
    body = credential_body("api_key", started.url)
    body["auth_value"] = "test-revoked-key-not-real-0123456789"
    assert control_client.request("PUT", f"/v1/sessions/{SESSION_ID}/credential", body).status == 200
    answer = api.request("POST", MESSAGES, messages(OPUS))
    assert (answer.status, answer.json()["error"]["type"]) == (401, "authentication_error")
    provision("api_key", started.url)
    assert api.request("POST", MESSAGES, messages(OPUS)).status == 200


# ---------------------------------------------------------------------------------------------- upstream failures


def test_upstream_errors_pass_through(
    start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    started = start_double(
        "api_key",
        {
            "id": "busy",
            "error": {
                "status": 529,
                "type": "overloaded_error",
                "message": "Overloaded",
                "headers": {"retry-after": "1"},
            },
        },
        {"id": "mid", "error": {"status": 500, "type": "api_error", "message": "boom", "in_stream": True}},
    )
    provision("api_key", started.url)
    busy = api.request("POST", MESSAGES, messages(OPUS))
    assert (busy.status, busy.headers["retry-after"]) == (529, "1")
    assert busy.json()["error"]["type"] == "overloaded_error"
    mid = api.request("POST", MESSAGES, messages(OPUS))
    assert mid.status == 200
    assert [name for name, _ in sse_events(mid.body)] == ["message_start", "error"]
    assert [usage["status"] for usage in wait_for_usage(context, 2)] == [529, 200]


def test_a_model_the_subscription_does_not_cover_is_journaled_as_such(
    start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    """HOT-26: the provider's "usage credits are required" answer is passed through unchanged and its `usage` record
    carries `refusal: model_not_in_plan`; a plain rate limit does not."""
    credits = "Usage credits are required for this model."
    started = start_double(
        "subscription",
        {"id": "credits", "error": {"status": 429, "type": "rate_limit_error", "message": credits}},
        {"id": "limit", "error": {"status": 429, "type": "rate_limit_error", "message": "Rate limited"}},
    )
    provision("subscription", started.url)
    refused = api.request("POST", MESSAGES, messages(OPUS))
    assert (refused.status, refused.json()["error"]["message"]) == (429, credits)
    assert api.request("POST", MESSAGES, messages(OPUS)).status == 429
    first, second = wait_for_usage(context, 2)
    assert (first["status"], first["refusal"]) == (429, "model_not_in_plan")
    assert (second["status"], "refusal" in second) == (429, False)


def test_only_an_error_answer_naming_the_plan_is_a_model_refusal() -> None:
    assert refusal_of({"type": "error", "error": {"message": "Your plan: this is NOT INCLUDED IN YOUR PLAN"}}) == (
        "model_not_in_plan"
    )
    answers: list[object] = [
        {"type": "error", "error": {"message": "Overloaded"}},
        {"type": "error", "error": "x"},
        [],
        None,
    ]
    for answer in answers:
        assert refusal_of(answer) is None
    assert refusal_of({"type": "message", "error": {"message": "usage credits are required"}}) is None


def test_unreachable_upstream_is_502(provision: Provision, api: Api, context: ControlContext) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    provision("api_key", f"http://127.0.0.1:{port}")
    answer = api.request("POST", MESSAGES, messages(OPUS))
    assert (answer.status, answer.json()["error"]["type"]) == (502, "api_error")
    assert [usage["status"] for usage in usage_records(context)] == [502]


def test_tls_upstream_must_present_a_valid_certificate(
    start_double: StartDouble, provision: Provision, api: Api
) -> None:
    started = start_double()
    provision("api_key", started.url.replace("http://", "https://"))
    assert api.request("POST", MESSAGES, messages(OPUS)).status == 502
    assert started.client.journal() == []


def test_client_abort_still_records_usage(
    raw_upstream: Callable[..., Any],
    provision: Provision,
    api: Api,
    context: ControlContext,
    capfd: pytest.CaptureFixture[str],
) -> None:
    """The session drops the connection mid-stream: the upstream is closed and the usage written."""
    upstream = raw_upstream(hold=True)
    provision("api_key", upstream.url)
    connection = api.connection()
    connection.request(
        "POST", MESSAGES, body=json.dumps(messages(OPUS)), headers={"Authorization": f"Bearer {SESSION_TOKEN}"}
    )
    response = connection.getresponse()
    assert response.status == 200
    assert b"message_start" in response.read1(65536)
    connection.sock.shutdown(socket.SHUT_RDWR)
    connection.close()
    upstream.release()
    [usage] = wait_for_usage(context, 1)
    assert usage["input_tokens"] == 5
    assert usage["request_id"] == "req_raw_1"
    assert upstream.closed.wait(10)
    err = capfd.readouterr().err
    for secret in SECRETS:
        assert secret not in err


def test_upstream_abort_breaks_the_client_stream(
    raw_upstream: Callable[..., Any], provision: Provision, api: Api, context: ControlContext
) -> None:
    upstream = raw_upstream(cut=True)
    provision("api_key", upstream.url)
    connection = api.connection()
    connection.request(
        "POST", MESSAGES, body=json.dumps(messages(OPUS)), headers={"Authorization": f"Bearer {SESSION_TOKEN}"}
    )
    response = connection.getresponse()
    with pytest.raises(http.client.IncompleteRead):
        response.read()
    connection.close()
    [usage] = wait_for_usage(context, 1)
    assert usage["status"] == 200


def test_upstream_closing_without_answer_is_502(
    raw_upstream: Callable[..., Any], provision: Provision, api: Api
) -> None:
    upstream = raw_upstream(silent=True)
    provision("api_key", upstream.url)
    assert api.request("POST", MESSAGES, messages(OPUS)).status == 502


def test_response_headers_are_filtered_and_joined(
    raw_upstream: Callable[..., Any], provision: Provision, api: Api, context: ControlContext
) -> None:
    upstream = raw_upstream(plain=True)
    provision("api_key", upstream.url + "/base/")
    answer = api.request("POST", MESSAGES, messages(OPUS, stream=False))
    assert answer.status == 200
    assert answer.headers["Content-Type"] == "application/json"
    assert answer.headers["x-dup"] == "a, b"
    assert "set-cookie" not in answer.headers
    assert answer.headers.get_all("Cache-Control") == ["no-store"]
    assert upstream.requests[0].startswith(b"POST /base/v1/messages?beta=true HTTP/1.1\r\n")
    [usage] = wait_for_usage(context, 1)
    assert usage["request_id"].startswith("gw_")
    assert usage["output_tokens"] == 0


def test_only_the_beta_query_parameter_is_forwarded(
    start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    started = start_double("api_key", {**text_step("One."), "repeat": 2})
    provision("api_key", started.url)
    for path in ("/model/v1/messages?key=x&beta=true&provider=y", "/model/v1/messages?route=fallback"):
        assert api.request("POST", path, messages(OPUS)).status == 200
    first, second = started.client.journal()
    assert (first["path"], first["query"]) == ("/v1/messages", "beta=true")
    assert (second["path"], second["query"]) == ("/v1/messages", "")
    wait_for_usage(context, 2)


@pytest.mark.parametrize("field", ["models", "provider", "route"])
def test_routing_fields_are_refused(
    field: str, start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    """ATK-39: OpenRouter must not pick another model or provider than the checked one."""
    started = start_double("platform")
    provision("platform", started.url)
    answer = api.request("POST", MESSAGES, {**messages(DEEPSEEK), field: ["anthropic/claude-opus-5-5"]})
    assert (answer.status, answer.json()["error"]["type"]) == (400, "invalid_request_error")
    assert started.client.journal() == []


def test_parallel_first_requests_all_find_the_credential(
    start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    started = start_double("api_key", *[text_step(f"n{index}") for index in range(8)])
    provision("api_key", started.url)
    with ThreadPoolExecutor(8) as pool:
        statuses = list(pool.map(lambda _: api.request("POST", MESSAGES, messages(OPUS)).status, range(8)))
    assert statuses == [200] * 8
    assert journal_bodies(context, "guest.error") == []


def test_model_requests_are_capped(
    start_double: StartDouble, provision: Provision, api: Api, monkeypatch: pytest.MonkeyPatch, api_server: Any
) -> None:
    started = start_double()
    provision("api_key", started.url)
    gateway = gateway_for(api_server.context.control)
    monkeypatch.setattr(gateway, "_slots", threading.BoundedSemaphore(1))
    gateway._slots.acquire()
    answer = api.request("POST", MESSAGES, messages(OPUS))
    assert (answer.status, answer.json()["error"]["type"]) == (529, "overloaded_error")
    gateway._slots.release()
    assert api.request("POST", MESSAGES, messages(OPUS)).status == 200


# ---------------------------------------------------------------------------------------- a body never read (A-26)


def test_usage_is_journaled_when_the_response_is_never_written(
    raw_upstream: Callable[..., Any],
    provision: Provision,
    api: Api,
    context: ControlContext,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # the client is gone before the status line: the body is dropped without one chunk read (AUD-01 A-26)
    upstream = raw_upstream(plain=True)
    provision("api_key", upstream.url)
    original = routing.Handler.send_response

    def gone(handler: routing.Handler, code: int, message: str | None = None) -> None:
        if handler.path.startswith("/model/"):
            raise BrokenPipeError("client gone")
        original(handler, code, message)

    monkeypatch.setattr(routing.Handler, "send_response", gone)
    connection = api.connection()
    connection.request(
        "POST", MESSAGES, body=json.dumps(messages(OPUS)), headers={"Authorization": f"Bearer {SESSION_TOKEN}"}
    )
    with pytest.raises((http.client.HTTPException, OSError)):
        connection.getresponse()
    connection.close()
    [usage] = wait_for_usage(context, 1)
    assert (usage["status"], usage["response_bytes"]) == (200, 0)


def test_a_relay_ends_once_however_it_is_left(
    raw_upstream: Callable[..., Any], provision: Provision, context: ControlContext, api: Api
) -> None:
    upstream = raw_upstream(plain=True)
    provision("api_key", upstream.url)
    ended: list[int] = []

    class Gateway(gateway_module.Gateway):
        def _finish(self, stream: Any) -> None:
            ended.append(stream.sent)
            super()._finish(stream)

    request = routing.Request(
        method="POST",
        path=MESSAGES,
        query={"beta": "true"},
        headers=email.message_from_string(f"Authorization: Bearer {SESSION_TOKEN}\n\n"),
        stream=io.BytesIO(json.dumps(messages(OPUS)).encode()),  # type: ignore[arg-type]
    )
    response = Gateway(ApiClient(context.config.api_socket)).forward(request, "/v1/messages", record_usage=True)
    relay = response.body
    assert isinstance(relay, gateway_module.Relay)
    assert iter(relay) is relay
    first = next(relay)
    assert first
    relay.close()
    relay.close()
    assert ended == [len(first)]
    with pytest.raises(StopIteration):
        next(relay)  # ended: nothing more is read
    [usage] = wait_for_usage(context, 1)
    assert usage["response_bytes"] == len(first)


def test_a_renewal_keeps_the_own_uncounted_cost_although_others_spent(
    start_double: StartDouble, provision: Provision, api: Api, context: ControlContext
) -> None:
    """KOR-10 (AUD-02 NA-10): the spent amount is the organization's. Others spent 0.003 meanwhile and the hub counted
    it; this gateway's 0.002 was not in the runner's outbox yet (its `usage` record is above `journal_through`), so the
    renewal keeps it although `old spent + observed - new spent` is negative. Once the runner took the record in (and
    added it to the spent amount), it is not carried any more."""
    started = start_double("platform", text_step("one", cost=0.002), text_step("two", cost=0.002))
    provision("platform", started.url, budget={"usd_per_day": 0.008, "spent_usd": 0.004, "journal_through": 0})
    assert api.request("POST", MESSAGES, messages(DEEPSEEK)).status == 200
    wait_for_usage(context, 1)
    seq = next(r["seq"] for r in context.journal.read(0, 100)[0] if r["kind"] == "usage")
    provision("platform", started.url, budget={"usd_per_day": 0.008, "spent_usd": 0.007, "journal_through": seq - 1})
    assert api.request("POST", MESSAGES, messages(DEEPSEEK)).status == 429  # 0.007 + own 0.002 >= 0.008
    provision("platform", started.url, budget={"usd_per_day": 0.012, "spent_usd": 0.009, "journal_through": seq})
    assert api.request("POST", MESSAGES, messages(DEEPSEEK)).status == 200  # counted once: 0.009 < 0.012
    statuses = [usage["status"] for usage in wait_for_usage(context, 3)]
    assert statuses == [200, 429, 200]
