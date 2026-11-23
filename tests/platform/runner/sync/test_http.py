"""U-RUN-sync (HTTP): signed requests accepted by the fake hub, answers verified against the pinned hub key;
manipulated answers are transport errors and never used (03-api.md section 1)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.sync import http
from agent_runner.sync.http import HubClient, HubRefusal, HubTransportError, Signer, hub_origin

from .conftest import Env, Harness, run
from .test_outbox import fixture_body

TAMPER = {
    "unsigned": "response_unsigned",
    "other_key": "response_signature_invalid",
    "bad_signature": "response_signature_invalid",
    "body_changed": "response_signature_invalid",
    "stale_time": "response_time_skew",
    "short_signature": "response_header_invalid",
    "padded_signature": "response_header_invalid",
    "bad_key_id": "response_header_invalid",
    "bad_time": "response_header_invalid",
    "impossible_time": "response_header_invalid",
    "duplicate_header": "response_header_invalid",
    "encoded": "response_encoded",
    "too_large": "response_too_large",
}


async def state(env: Env) -> dict[str, Any]:
    return await env.sync.client.request("GET", "/v1/device/state")


def test_signed_request_and_verified_answer(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            answer = await state(env)
            assert answer["state"] == "active"
            assert answer["last_origin_seq"] == 0
            pushed = await env.sync.client.request(
                "POST",
                "/v1/device/outbox",
                {"items": [{"origin_seq": 1, "item_kind": "metrics", "body": fixture_body("metrics")}]},
            )
            assert pushed == {"acked_through": 1, "results": [{"origin_seq": 1, "status": "ok"}]}
            assert [r.target for r in env.hub.requests] == ["/v1/device/state", "/v1/device/outbox"]
            assert env.sync.client.key_id == "device." + env.context.identity.device.device_id.replace("-", "")  # type: ignore[union-attr]

    run(body)


@pytest.mark.parametrize("mode", sorted(TAMPER))
def test_manipulated_answers_are_discarded(harness: Harness, mode: str) -> None:
    async def body() -> None:
        async with harness.env() as env:
            env.hub.tamper.append(mode)
            with pytest.raises(HubTransportError) as caught:
                await state(env)
            assert caught.value.reason == TAMPER[mode]
            # the next, untampered answer is accepted again
            assert (await state(env))["state"] == "active"

    run(body)


def test_unsigned_refusal_reports_only_the_claimed_code(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            hub_now = env.wall.now - 3600
            env.hub.wall = lambda: hub_now  # the hub refuses the request time before its gate signs anything
            with pytest.raises(HubTransportError) as caught:
                await state(env)
            assert (caught.value.reason, caught.value.status, caught.value.claimed) == (
                "response_unsigned",
                401,
                "time_skew",
            )

    run(body)


def test_verified_refusals(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            scripted: list[tuple[int, dict[str, Any], dict[str, str]]] = [
                (403, {"error": "forbidden"}, {}),
                (409, {"error": "origin_seq_gap", "detail": {"expected": 3}}, {}),
                (429, {"error": "rate_limited"}, {"Retry-After": "7"}),
                (429, {"error": "rate_limited"}, {"Retry-After": "0"}),
                (429, {"error": "rate_limited"}, {"Retry-After": "99999"}),
                (429, {"error": "rate_limited"}, {"Retry-After": "soon"}),
            ]
            env.hub.scripted.extend(scripted)
            found = []
            for _ in scripted:
                with pytest.raises(HubRefusal) as caught:
                    await state(env)
                found.append((caught.value.status, caught.value.code, caught.value.detail, caught.value.retry_after))
            assert found == [
                (403, "forbidden", {}, None),
                (409, "origin_seq_gap", {"expected": 3}, None),
                (429, "rate_limited", {}, 7),
                (429, "rate_limited", {}, 1),
                (429, "rate_limited", {}, 3600),
                (429, "rate_limited", {}, None),
            ]
            assert str(HubRefusal(403, "forbidden")) == "403 forbidden"

    run(body)


@pytest.mark.parametrize(
    ("status", "answer", "reason"),
    [
        (400, {"error": "not_a_hub_code"}, "response_invalid"),
        (400, {"error": "Bad Code"}, "response_invalid"),
        (400, {"error": 7}, "response_invalid"),
        (400, {"error": "forbidden", "detail": []}, "response_invalid"),
        (400, {"error": "forbidden", "extra": 1}, "response_invalid"),
        (400, {"nothing": 1}, "response_invalid"),
        (400, [], "response_invalid"),
        (200, [], "response_invalid"),
        (302, {"location": "elsewhere"}, "redirect_refused"),
    ],
)
def test_malformed_but_signed_answers(harness: Harness, status: int, answer: Any, reason: str) -> None:
    async def body() -> None:
        async with harness.env() as env:
            env.hub.scripted.append((status, answer, {"Location": "http://127.0.0.1:9/v1/device/state"}))
            with pytest.raises(HubTransportError) as caught:
                await state(env)
            assert (caught.value.reason, caught.value.status) == (reason, status)
            assert len(env.hub.requests) == 1  # the redirect was not followed

    run(body)


class _Fixed(httpx.AsyncBaseTransport):
    """Answers every request with fixed bytes signed for it (statuses httpx cannot get from an ASGI app)."""

    def __init__(self, hub: Any, status: int, raw: bytes) -> None:
        self.hub, self.status, self.raw = hub, status, raw

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        headers = {k.lower(): v for k, v in request.headers.items()}
        signed = self.hub.sign(headers, self.status, self.raw)
        return httpx.Response(
            self.status, headers=[(k.decode(), v.decode()) for k, v in signed], stream=httpx.ByteStream(self.raw)
        )


class _Broken(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)


@pytest.mark.parametrize(("status", "raw"), [(200, b"not json"), (199, b"{}"), (200, b"[1]")])
def test_answers_that_are_not_a_json_object(harness: Harness, status: int, raw: bytes) -> None:
    async def body() -> None:
        async with harness.env() as env:
            client = _client(env, _Fixed(env.hub, status, raw))
            with pytest.raises(HubTransportError, match="response_invalid"):
                await client.request("GET", "/v1/device/state")
            await client.aclose()

    run(body)


def _client(env: Env, transport: httpx.AsyncBaseTransport, **extra: Any) -> HubClient:
    identity = env.context.identity
    assert identity is not None
    return HubClient(
        hub_url=identity.device.hub_url,
        key_id=identity.device.key_id,
        signing_key=identity.keys.signing_key(),
        hub_key=env.hub.public,
        transport=transport,
        wall=env.wall,
        **extra,
    )


def test_unreachable_hub(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            client = _client(env, _Broken())
            with pytest.raises(HubTransportError, match="hub_unreachable"):
                await client.request("POST", "/v1/device/outbox", {"items": []})
            await client.aclose()

    run(body)


def test_signer_refuses_a_bad_target() -> None:
    signer = Signer(key_id="device.0", signing_key=Ed25519PrivateKey.generate(), host="h", wall=lambda: 0.0)
    for target in ("v1/x", "/v1/x y", "/v1/ä"):
        with pytest.raises(ValueError, match="visible-ASCII"):
            signer.sign("GET", target, b"")
    signed = signer.sign("GET", "/v1/device/pull?cursor=a:1", b"")
    assert signed.agent_time == "1970-01-01T00:00:00Z"
    assert set(signed.headers) == {"Agent-Key", "Agent-Time", "Agent-Nonce", "Agent-Signature"}


@pytest.mark.parametrize(
    ("url", "base", "host", "ws"),
    [
        ("https://Hub.Example.org", "https://hub.example.org", "hub.example.org", "wss://hub.example.org"),
        ("https://hub.example.org:443/", "https://hub.example.org", "hub.example.org", "wss://hub.example.org"),
        (
            "https://hub.example.org:8788",
            "https://hub.example.org:8788",
            "hub.example.org:8788",
            "wss://hub.example.org:8788",
        ),
        ("http://127.0.0.1:8788", "http://127.0.0.1:8788", "127.0.0.1:8788", "ws://127.0.0.1:8788"),
        ("http://[::1]:80", "http://[::1]", "[::1]", "ws://[::1]"),
    ],
)
def test_hub_origin(url: str, base: str, host: str, ws: str) -> None:
    assert hub_origin(url) == http.HubOrigin(base_url=base, host=host, ws_base_url=ws)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://hub.example.org",
        "https://user@hub.example.org",
        "https://hub.example.org/api",
        "https://hub.example.org?x=1",
        "https://hub.example.org#f",
        "https://hub.example.org/?",
        "https://",
    ],
)
def test_hub_origin_refusals(url: str) -> None:
    with pytest.raises(ValueError, match="hub URL"):
        hub_origin(url)


def test_times() -> None:
    assert http.parse_time("2026-11-21T12:00:00Z") == 1_795_262_400.0
    assert http.format_time(1_795_262_400.9) == "2026-11-21T12:00:00Z"
    for bad in ("2026-02-30T00:00:00Z", "2026-11-21T12:00:00.000Z", "yesterday"):
        assert http.parse_time(bad) is None


def test_error_bodies_that_are_not_json(harness: Harness) -> None:
    async def body() -> None:
        async with harness.env() as env:
            client = _client(env, _Fixed(env.hub, 400, b"<html>"))
            with pytest.raises(HubTransportError, match="response_invalid"):
                await client.request("GET", "/v1/device/state")
            await client.aclose()
            env.hub.tamper.append("unsigned")
            client = _client(env, _Fixed(env.hub, 502, b"<html>"))
            with pytest.raises(HubTransportError) as caught:
                await client.request("GET", "/v1/device/state")
            assert (caught.value.reason, caught.value.claimed) == ("response_unsigned", None)
            await client.aclose()

    run(body)
