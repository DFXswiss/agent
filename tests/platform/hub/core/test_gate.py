"""U-HUB-auth (gate part): the signature gate of 03-api.md section 1 and the response signature.

Every failure class of the gate is refused with its code: missing or malformed signature headers, unknown key,
refusal by the resolver, time skew, invalid signature (also a changed body, query, method or host), nonce replay and
size. Responses to signed requests verify with `agent_core.signing.response_text`.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Iterator
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import Request
from psycopg_pool import ConnectionPool

from agent_core.signing import (
    b64url_decode,
    b64url_encode,
    format_time,
    request_text,
    response_text,
    sha256_hex,
)
from agent_hub.auth.gate import verify_enrollment
from agent_hub.auth.keys import (
    KeyContext,
    Principal,
    ResolvedKey,
    check_resolvers,
    ed25519_verifier,
    resolver_for,
)
from agent_hub.config import HubConfig
from agent_hub.config_api.keys import resolve_platform_root
from agent_hub.errors import HubError
from agent_hub.http.context import Context, CurrentPrincipal
from agent_hub.http.routing import EVENT_BATCH_BYTES, REQUEST_BODY_BYTES, HubRouter
from tests.platform.hub.conftest import FixedClock, HubHarness, SigningKey, raw_public, start_hub

router = HubRouter()


def principal_body(principal: Principal) -> dict[str, Any]:
    return {
        "kind": principal.kind,
        "key_id": principal.key_id,
        "principal_id": None if principal.principal_id is None else str(principal.principal_id),
    }


@router.post("/v1/test/browser", auth="browser", right="user")
async def browser_route(request: Request, principal: CurrentPrincipal) -> dict[str, Any]:
    return {**principal_body(principal), "body": (await request.body()).decode()}


@router.get("/v1/test/anon", auth="anon", right="anon")
async def anon_route(principal: CurrentPrincipal) -> dict[str, Any]:
    return principal_body(principal)


@router.post("/v1/test/anon-or-browser", auth="anon_or_browser", right=("anon", "user"))
async def anon_or_browser_route(principal: CurrentPrincipal) -> dict[str, Any]:
    return principal_body(principal)


@router.get("/v1/test/device", auth="device", right="device", read_resource="device.state")
async def device_route(principal: CurrentPrincipal, x: str | None = None) -> dict[str, Any]:
    return {**principal_body(principal), "state": principal.attributes["state"]}


@router.get("/v1/test/root", auth="root", right="root", read_resource="platform.enrollments")
async def root_route(principal: CurrentPrincipal) -> dict[str, Any]:
    return principal_body(principal)


@router.post("/v1/test/batch", auth="device", right="device", max_body=EVENT_BATCH_BYTES)
async def batch_route(request: Request) -> dict[str, Any]:
    return {"size": len(await request.body())}


@router.post("/v1/test/enroll", auth="device_enroll", right="invite_proof")
async def enroll_route(request: Request, context: Context) -> dict[str, Any]:
    body = await request.json()
    device_id = UUID(hex=body.get("device", request.headers["agent-key"].removeprefix("device.")))
    principal = Principal(kind="device", key_id=f"device.{device_id.hex}", principal_id=device_id, actor_kind="device")
    await verify_enrollment(request, context, ed25519_verifier(b64url_decode(body["pub"]) or b""), principal)
    return {"enrolled": True}


@pytest.fixture
def gate(hub_config: HubConfig, hub_pool: ConnectionPool[Any], clock: FixedClock) -> Iterator[HubHarness]:
    yield from start_hub(hub_config, hub_pool, clock, routers=[("test", router)])


def headers_for(
    key: SigningKey,
    method: str,
    target: str,
    body: bytes = b"",
    *,
    host: str = "localhost:8788",
    agent_time: str = "2026-11-21T12:00:00Z",
    nonce: str | None = None,
) -> dict[str, str]:
    nonce = nonce or b64url_encode(secrets.token_bytes(16))
    text = request_text(key.key_id, method, host, target, agent_time, nonce, sha256_hex(body))
    return {
        "Agent-Key": key.key_id,
        "Agent-Time": agent_time,
        "Agent-Nonce": nonce,
        "Agent-Signature": b64url_encode(key.sign(text)),
    }


def assert_refused(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json() == {"error": code}


# Successful requests of every key kind.


def test_browser_session(gate: HubHarness) -> None:
    client = gate.browser()
    response = client.post("/v1/test/browser", content=b'{"x":1}')
    assert response.status_code == 200
    assert response.extensions["hub_signature"] is True
    key = client.key
    assert key is not None
    assert response.json() == {
        "kind": "session",
        "key_id": key.key_id,
        "principal_id": str(key.principal.principal_id),
        "body": '{"x":1}',
    }


def test_device(gate: HubHarness) -> None:
    response = gate.device(state="pending_certification").get("/v1/test/device")
    assert response.status_code == 200
    assert response.json()["kind"] == "device"
    assert response.json()["state"] == "pending_certification"


def test_platform_root(gate: HubHarness) -> None:
    response = gate.root.get("/v1/test/root")
    assert response.status_code == 200
    assert response.json() == {"kind": "root", "key_id": "platform-root", "principal_id": None}


def test_anonymous_route_accepts_a_signed_request(gate: HubHarness) -> None:
    response = gate.root.get("/v1/test/anon")
    assert response.json()["kind"] == "root"
    assert gate.anon.get("/v1/test/anon").json()["kind"] == "anonymous"


def test_response_signature_with_agent_core_response_text(gate: HubHarness) -> None:
    key = gate.keys.device()
    headers = headers_for(key, "GET", "/v1/test/device?x=1")
    response = gate.base.get("/v1/test/device?x=1", headers=headers)
    assert response.status_code == 200
    hub = gate.config.signer()
    assert response.headers["agent-hub-key"] == hub.key_id
    hub_time = response.headers["agent-hub-time"]
    assert hub_time == format_time(int(gate.clock().timestamp()))
    text = response_text(
        key.key_id, headers["Agent-Nonce"], headers["Agent-Time"], 200, hub_time, sha256_hex(response.content)
    )
    signature = b64url_decode(response.headers["agent-hub-signature"])
    assert signature is not None
    Ed25519PublicKey.from_public_bytes(hub.public_key).verify(signature, text)


def test_refusals_after_the_gate_are_signed(gate: HubHarness) -> None:
    response = gate.device().post("/v1/test/browser")
    assert_refused(response, 403, "forbidden")
    assert response.extensions["hub_signature"] is True


def test_unknown_route_of_a_signed_request_is_signed(gate: HubHarness) -> None:
    for method in ("GET", "POST"):
        response = gate.root.request(method, "/v1/test/unknown")
        assert_refused(response, 404, "not_found")
        assert response.extensions["hub_signature"] is True


def test_head_of_a_signed_request(gate: HubHarness) -> None:
    response = gate.root.request("HEAD", "/v1/test/root")
    assert response.status_code == 404
    assert response.content == b""
    assert response.extensions["hub_signature"] is True


# Authentication classes.


@pytest.mark.parametrize(
    ("method", "path"), [("POST", "/v1/test/browser"), ("GET", "/v1/test/device"), ("GET", "/v1/test/root")]
)
def test_signed_routes_refuse_anonymous_requests(gate: HubHarness, method: str, path: str) -> None:
    response = gate.anon.request(method, path)
    assert_refused(response, 401, "signature_missing")
    assert "agent-hub-signature" not in response.headers


def test_wrong_key_kinds_are_forbidden(gate: HubHarness) -> None:
    assert_refused(gate.root.post("/v1/test/browser"), 403, "forbidden")
    assert_refused(gate.browser().get("/v1/test/device"), 403, "forbidden")
    assert_refused(gate.device().get("/v1/test/root"), 403, "forbidden")
    assert_refused(gate.device().post("/v1/test/anon-or-browser"), 403, "forbidden")
    assert gate.browser().post("/v1/test/anon-or-browser").json()["kind"] == "session"
    assert gate.anon.post("/v1/test/anon-or-browser").json()["kind"] == "anonymous"


# Malformed headers: 401 signature_missing, unsigned.


def good_headers(gate: HubHarness) -> dict[str, str]:
    return headers_for(gate.keys.root, "GET", "/v1/test/root")


@pytest.mark.parametrize("missing", ["Agent-Key", "Agent-Time", "Agent-Nonce", "Agent-Signature"])
def test_incomplete_headers(gate: HubHarness, missing: str) -> None:
    headers = good_headers(gate)
    del headers[missing]
    response = gate.base.get("/v1/test/anon", headers=headers)
    assert_refused(response, 401, "signature_missing")
    assert "agent-hub-signature" not in response.headers


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("Agent-Key", "session.ABC"),
        ("Agent-Key", "device." + "0" * 31),
        ("Agent-Key", "platform-root2"),
        ("Agent-Key", "hub-key"),
        ("Agent-Time", "2026-11-21 12:00:00Z"),
        ("Agent-Time", "2026-13-21T12:00:00Z"),
        ("Agent-Nonce", b64url_encode(b"\x00" * 15)),
        ("Agent-Nonce", "AAAAAAAAAAAAAAAAAAAAAA=="),
        ("Agent-Signature", b64url_encode(b"\x00" * 63)),
        ("Agent-Signature", "not base64!"),
        ("Agent-Key", "platform root"),
    ],
)
def test_malformed_headers(gate: HubHarness, name: str, value: str) -> None:
    headers = good_headers(gate)
    headers[name] = value
    assert_refused(gate.base.get("/v1/test/root", headers=headers), 401, "signature_missing")


def test_repeated_header(gate: HubHarness) -> None:
    headers = [*good_headers(gate).items(), ("Agent-Nonce", b64url_encode(b"\x01" * 16))]
    assert_refused(gate.base.get("/v1/test/root", headers=headers), 401, "signature_missing")


def test_non_ascii_header(gate: HubHarness) -> None:
    headers = [(name.encode(), value.encode()) for name, value in good_headers(gate).items()]
    headers[0] = (b"Agent-Key", "platform-röot".encode("latin-1"))
    assert_refused(gate.base.get("/v1/test/root", headers=headers), 401, "signature_missing")


def test_non_ascii_target(gate: HubHarness) -> None:
    key = gate.keys.root
    headers = headers_for(key, "GET", "/v1/test/anon")
    request = gate.base.build_request("GET", "http://localhost:8788/v1/test/anon?x=%C3%A4", headers=headers)
    request.url = httpx.URL("http://localhost:8788/v1/test/anon?x=ä")
    assert request.url.raw_path.isascii()
    assert_refused(gate.base.send(request), 401, "signature_invalid")


# Unknown keys and resolver refusals.


def test_unknown_session_and_device_keys(gate: HubHarness) -> None:
    for key_id in ("session." + "a" * 32, "device." + "b" * 32):
        stranger = SigningKey(key_id=key_id, sign=Ed25519PrivateKey.generate().sign, principal=gate.keys.root.principal)
        assert_refused(gate.client(stranger).get("/v1/test/anon"), 401, "key_unknown")


def test_prefix_without_a_resolver(hub_config: HubConfig, hub_pool: ConnectionPool[Any], clock: FixedClock) -> None:
    for harness in start_hub(hub_config, hub_pool, clock, routers=[("test", router)], resolvers={}):
        assert_refused(harness.root.get("/v1/test/root"), 401, "key_unknown")
        assert_refused(harness.browser().post("/v1/test/browser"), 401, "key_unknown")


def test_resolver_refusals(gate: HubHarness) -> None:
    assert_refused(gate.browser(expired=True).post("/v1/test/browser"), 401, "session_expired")
    assert_refused(gate.browser(ended=True).post("/v1/test/browser"), 401, "key_revoked")


def test_platform_root_resolver_only_knows_its_key_id(hub_config: HubConfig) -> None:
    class Context:
        config = hub_config

    context: Any = Context()
    resolved = resolve_platform_root(context, "platform-root")
    assert resolved is not None
    assert resolved.principal.actor_kind == "operator"
    assert resolve_platform_root(context, "session." + "0" * 32) is None


def test_resolver_lookup_and_registration_rules() -> None:
    def resolver(_context: KeyContext, _key_id: str) -> ResolvedKey | None:
        return None

    resolvers = {"session.": resolver, "platform-root": resolver}
    assert resolver_for(resolvers, "session." + "1" * 32) is resolver
    assert resolver_for(resolvers, "platform-root") is resolver
    assert resolver_for(resolvers, "device." + "1" * 32) is None
    assert resolver_for(resolvers, "platformroot") is None
    check_resolvers({"session.": resolver, "device.": resolver, "platform-root": resolver})
    for bad in ({"sess": resolver}, {"user.": resolver}, {"platform-root": "not callable"}):
        with pytest.raises(ValueError, match="key resolver"):
            check_resolvers(bad)  # type: ignore[arg-type]


# Time window.


@pytest.mark.parametrize(("offset", "accepted"), [(-120, True), (120, True), (-121, False), (121, False)])
def test_time_window(gate: HubHarness, offset: int, accepted: bool) -> None:
    sent = format_time(int(gate.clock().timestamp()) + offset)
    headers = headers_for(gate.keys.root, "GET", "/v1/test/root", agent_time=sent)
    response = gate.base.get("/v1/test/root", headers=headers)
    if accepted:
        assert response.status_code == 200
    else:
        assert_refused(response, 401, "time_skew")


# Signature.


def test_signature_by_another_key(gate: HubHarness) -> None:
    impostor = SigningKey(
        key_id="platform-root", sign=Ed25519PrivateKey.generate().sign, principal=gate.keys.root.principal
    )
    assert_refused(gate.client(impostor).get("/v1/test/root"), 401, "signature_invalid")


@pytest.mark.parametrize(
    "change",
    ["body", "query", "method", "host", "path", "key", "time", "nonce"],
)
def test_signature_covers_every_line(gate: HubHarness, change: str) -> None:
    key = gate.keys.session()
    body = b'{"a":1}'
    # (method, target, body, host) signed instead of what is sent.
    signed: dict[str, tuple[str, str, bytes, str]] = {
        "body": ("POST", "/v1/test/browser", b'{"a":2}', "localhost:8788"),
        "query": ("POST", "/v1/test/browser?x=2", body, "localhost:8788"),
        "method": ("PUT", "/v1/test/browser", body, "localhost:8788"),
        "host": ("POST", "/v1/test/browser", body, "evil.example"),
        "path": ("POST", "/v1/test/anon-or-browser", body, "localhost:8788"),
        "key": ("POST", "/v1/test/browser", body, "localhost:8788"),
        "time": ("POST", "/v1/test/browser", body, "localhost:8788"),
        "nonce": ("POST", "/v1/test/browser", body, "localhost:8788"),
    }
    method, signed_target, signed_body, host = signed[change]
    headers = headers_for(key, method, signed_target, signed_body, host=host)
    if change == "key":
        other = gate.keys.session()
        headers["Agent-Key"] = other.key_id
    if change == "time":
        headers["Agent-Time"] = "2026-11-21T12:00:01Z"
    if change == "nonce":
        headers["Agent-Nonce"] = b64url_encode(secrets.token_bytes(16))
    target = "/v1/test/browser?x=1" if change == "query" else "/v1/test/browser"
    response = gate.base.post(target, content=body, headers=headers)
    assert_refused(response, 401, "signature_invalid")


def test_host_header_is_not_the_signed_host(gate: HubHarness) -> None:
    # The hub signs and verifies with the host of AGENT_HUB_PUBLIC_URL, whatever the Host header says.
    key = gate.keys.root
    headers = headers_for(key, "GET", "/v1/test/root")
    response = gate.base.get("http://other.example:9999/v1/test/root", headers=headers)
    assert response.status_code == 200


# Nonce replay store.


def test_nonce_replay(gate: HubHarness) -> None:
    headers = headers_for(gate.keys.root, "GET", "/v1/test/root")
    assert gate.base.get("/v1/test/root", headers=headers).status_code == 200
    response = gate.base.get("/v1/test/root", headers=headers)
    assert_refused(response, 401, "nonce_replayed")


def test_same_nonce_of_another_key(gate: HubHarness) -> None:
    nonce = b64url_encode(secrets.token_bytes(16))
    first = headers_for(gate.keys.root, "GET", "/v1/test/anon", nonce=nonce)
    second = headers_for(gate.keys.device(), "GET", "/v1/test/anon", nonce=nonce)
    assert gate.base.get("/v1/test/anon", headers=first).status_code == 200
    assert gate.base.get("/v1/test/anon", headers=second).status_code == 200


def test_nonces_expire_after_the_window(gate: HubHarness) -> None:
    nonce = b64url_encode(secrets.token_bytes(16))
    first = headers_for(gate.keys.root, "GET", "/v1/test/anon", nonce=nonce)
    assert gate.base.get("/v1/test/anon", headers=first).status_code == 200
    gate.clock.advance(601)
    again = headers_for(
        gate.keys.root, "GET", "/v1/test/anon", nonce=nonce, agent_time=format_time(int(gate.clock().timestamp()))
    )
    assert gate.base.get("/v1/test/anon", headers=again).status_code == 200
    with gate.pool.connection() as connection:
        rows = connection.execute("SELECT count(*) FROM hub.request_nonce WHERE nonce = %s", (nonce,)).fetchone()
    assert rows == (1,)


# Body limits and content coding.


def test_body_limit(gate: HubHarness) -> None:
    client = gate.browser()
    assert client.post("/v1/test/browser", content=b"x" * REQUEST_BODY_BYTES).status_code == 200
    response = client.post("/v1/test/browser", content=b"x" * (REQUEST_BODY_BYTES + 1))
    assert_refused(response, 413, "size_exceeded")
    assert "agent-hub-signature" not in response.headers


def test_batch_limit(gate: HubHarness) -> None:
    client = gate.device()
    assert client.post("/v1/test/batch", content=b"x" * EVENT_BATCH_BYTES).json() == {"size": EVENT_BATCH_BYTES}
    assert_refused(client.post("/v1/test/batch", content=b"x" * (EVENT_BATCH_BYTES + 1)), 413, "size_exceeded")


def test_body_limit_without_content_length(gate: HubHarness) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(17):
            yield b"x" * 65536

    response = gate.anon.post("/v1/test/anon-or-browser", content=chunks())
    assert_refused(response, 413, "size_exceeded")


def test_body_limit_of_unrouted_requests(gate: HubHarness) -> None:
    assert_refused(gate.anon.post("/v1/nothing", content=b"x" * (REQUEST_BODY_BYTES + 1)), 413, "size_exceeded")


@pytest.mark.parametrize("value", ["abc", "-1"])
def test_malformed_content_length(gate: HubHarness, value: str) -> None:
    request = gate.base.build_request("POST", "/v1/test/anon-or-browser", content=b"{}")
    request.headers["Content-Length"] = value
    assert_refused(gate.base.send(request), 400, "request_invalid")


def test_content_encoding_is_refused(gate: HubHarness) -> None:
    response = gate.anon.post("/v1/test/anon-or-browser", content=b"x", headers={"Content-Encoding": "gzip"})
    assert_refused(response, 400, "request_invalid")


# Device enrolment: the route proves possession with the key from the body.


def test_enrollment(gate: HubHarness) -> None:
    device = Ed25519PrivateKey.generate()
    body = ('{"pub":"' + b64url_encode(raw_public(device)) + '"}').encode()
    key = SigningKey(key_id="device." + "c" * 32, sign=device.sign, principal=gate.keys.root.principal)
    response = gate.client(key).post("/v1/test/enroll", content=body)
    assert response.status_code == 200
    assert response.extensions["hub_signature"] is True


def test_enrollment_replay(gate: HubHarness) -> None:
    device = Ed25519PrivateKey.generate()
    body = ('{"pub":"' + b64url_encode(raw_public(device)) + '"}').encode()
    key = SigningKey(key_id="device." + "d" * 32, sign=device.sign, principal=gate.keys.root.principal)
    headers = headers_for(key, "POST", "/v1/test/enroll", body)
    assert gate.base.post("/v1/test/enroll", content=body, headers=headers).status_code == 200
    assert_refused(gate.base.post("/v1/test/enroll", content=body, headers=headers), 401, "nonce_replayed")


def test_enrollment_principal_must_match_the_signing_key(gate: HubHarness) -> None:
    device = Ed25519PrivateKey.generate()
    other = "e" * 32
    body = ('{"pub":"' + b64url_encode(raw_public(device)) + '","device":"' + other + '"}').encode()
    key = SigningKey(key_id="device." + "f" * 32, sign=device.sign, principal=gate.keys.root.principal)
    assert_refused(gate.client(key).post("/v1/test/enroll", content=body), 401, "signature_missing")


def test_enrollment_counts_against_the_anonymous_limit(gate: HubHarness) -> None:
    key = SigningKey(
        key_id="device." + "a" * 32, sign=Ed25519PrivateKey.generate().sign, principal=gate.keys.root.principal
    )
    body = ('{"pub":"' + b64url_encode(raw_public(Ed25519PrivateKey.generate())) + '"}').encode()
    for _ in range(30):
        assert gate.client(key).post("/v1/test/enroll", content=body).status_code == 401
    assert_refused(gate.client(key).post("/v1/test/enroll", content=body), 429, "rate_limited")


def test_enrollment_with_a_wrong_key(gate: HubHarness) -> None:
    body = ('{"pub":"' + b64url_encode(raw_public(Ed25519PrivateKey.generate())) + '"}').encode()
    key = SigningKey(
        key_id="device." + "c" * 32, sign=Ed25519PrivateKey.generate().sign, principal=gate.keys.root.principal
    )
    response = gate.client(key).post("/v1/test/enroll", content=body)
    assert_refused(response, 401, "signature_invalid")


def test_enrollment_needs_signature_headers(gate: HubHarness) -> None:
    assert_refused(gate.anon.post("/v1/test/enroll", content=b"{}"), 401, "signature_missing")


def test_time_is_checked_before_the_key_lookup(gate: HubHarness) -> None:
    stranger = SigningKey(
        key_id="session." + "9" * 32, sign=Ed25519PrivateKey.generate().sign, principal=gate.keys.root.principal
    )
    headers = headers_for(stranger, "GET", "/v1/test/anon", agent_time="2026-11-21T11:00:00Z")
    assert_refused(gate.base.get("/v1/test/anon", headers=headers), 401, "time_skew")


def test_verify_enrollment_without_pending_signature() -> None:
    request = Request({"type": "http", "headers": [], "method": "POST", "path": "/"})
    verify = ed25519_verifier(raw_public(Ed25519PrivateKey.generate()))
    with pytest.raises(HubError, match="signature_missing"):
        asyncio.run(verify_enrollment(request, cast(KeyContext, None), verify, cast(Principal, None)))


def test_ed25519_verifier_refuses_small_order_keys_and_non_canonical_s() -> None:
    identity = bytes([1]) + bytes(31)
    assert not ed25519_verifier(identity)(identity + bytes(32), b"any text")
    device = Ed25519PrivateKey.generate()
    signature = device.sign(b"text")
    verify = ed25519_verifier(raw_public(device))
    assert verify(signature, b"text")
    order = 2**252 + 27742317777372353535851937790883648493
    high = (int.from_bytes(signature[32:], "little") + order).to_bytes(32, "little")
    assert not verify(signature[:32] + high, b"text")
