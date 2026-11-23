"""Signed HTTP client of the runner (03-api.md section 1, 05-runner.md section 2.3).

Every request is signed with `agent-request-v1` (texts of the legacy `agent_cli.keys.request_text`, a read-only
import allowed by 00-overview.md section 5): key ID `device.<32 hex>`, METHOD, the hub's host line, the target as sent
(path and `?query`), the time `YYYY-MM-DDTHH:MM:SSZ`, a fresh 16-byte nonce and `sha256_hex(body)`, in the headers
`Agent-Key`, `Agent-Time`, `Agent-Nonce` and `Agent-Signature`.

Every answer must be signed by the **pinned** hub key with `agent-response-v1` (`agent_cli.keys.response_text`) for
exactly this request (key ID, nonce, time) and be within 120 s of the local clock. An answer that is unsigned,
signed by another key, altered, stale, compressed, larger than the limit or a redirect is a transport error
(`HubTransportError`, retried by the caller) and its body is never used; an unsigned error body yields at most a
`claimed` code for the log line. Redirects are never followed. A verified refusal is a `HubRefusal` with a code of
the closed list `codes.HUB_ERROR_CODES`; a verified refusal with a code outside that list is a transport error too.

Connections (KOR-18): the client keeps an idle connection for at most `KEEPALIVE_EXPIRY_S`, well below the hub's
idle limit (`agent-hub serve`: 120 s; plain servers often close after 5 s), so it does not send on a connection the hub
is closing. When a request still fails on a **reused** connection before any byte of the answer arrived (the hub
closed it while the request was in flight: `RemoteProtocolError`, `ReadError`, `WriteError`), it is sent exactly once
more, signed anew (time, nonce) with the same body; the pool has dropped the broken connection and takes a new one or
another idle one, and a failure of the repeat is final. This is safe because every device endpoint
treats a repeated identical body as a repeat (outbox items by `origin_seq`, responses by `seq`, credential writes by
the stored bytes, operations by their ID; reads have no effect). A failure on a new connection, a timeout, a failure
after the answer began, and every answer that does not verify are not repeated. `HubTransportError` names whether the
connection was reused and the class of the transport error (`describe()`), never secrets.

`hub_origin` is copied and adapted from the legacy `agent_cli.keys.hub_origin` (host line rules).
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from agent_cli.keys import request_text, response_text
from agent_runner.encoding import b64u_decode, b64u_encode
from agent_runner.sync.codes import HUB_ERROR_CODES

TIME_WINDOW_S = 120
NONCE_BYTES = 16
SIGNATURE_BYTES = 64
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
"""Upper bound of an answer body (requests are at most 4 MiB, answers are smaller; pulls are paged)."""
REQUEST_TIMEOUT_S = 30.0
KEEPALIVE_EXPIRY_S = 2.0
"""Seconds an idle connection is reused: well below every server idle limit the runner meets (hub 120 s, common
defaults 5 s), with a margin far above a round trip, so the client closes before the server does."""
MAX_CONNECTIONS = 100
MAX_IDLE_CONNECTIONS = 20
"""Pool bounds (httpx's defaults, kept when the idle time is set explicitly)."""
_STALE_ERRORS = (httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError)
"""Transport errors of a connection the server closed under the request (no timeouts, no connect errors)."""
RETRY_AFTER_MAX_S = 3600
HUB_HEADERS = ("Agent-Hub-Key", "Agent-Hub-Time", "Agent-Hub-Signature")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_WS_SCHEMES = {"http": "ws", "https": "wss"}
_HUB_KEY_ID_RE = re.compile(r"[a-z0-9-]{1,64}")
_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
_TARGET_RE = re.compile(r"/[!-~]*")
_CODE_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class HubTransportError(Exception):
    """The hub could not be reached or its answer did not verify; retry later. `reason` is a closed code."""

    def __init__(
        self,
        reason: str,
        *,
        status: int | None = None,
        claimed: str | None = None,
        reused: bool | None = None,
        cause: str | None = None,
    ) -> None:
        self.reason = reason
        self.status = status
        self.claimed = claimed
        self.reused = reused
        self.cause = cause
        super().__init__(reason)

    def describe(self) -> str:
        """`reason=<code>[ reused=<yes|no>][ cause=<class>]` for log lines (no secret)."""
        parts = [f"reason={self.reason}"]
        if self.reused is not None:
            parts.append(f"reused={'yes' if self.reused else 'no'}")
        if self.cause is not None:
            parts.append(f"cause={self.cause}")
        return " ".join(parts)


class _StaleConnection(HubTransportError):
    """`hub_unreachable` on a reused connection before the answer began: the one case sent again."""


class HubRefusal(Exception):
    """A verified refusal of the hub: HTTP status, error code, optional `detail` and `Retry-After` seconds."""

    def __init__(
        self, status: int, code: str, detail: Mapping[str, object] | None = None, retry_after: int | None = None
    ) -> None:
        self.status = status
        self.code = code
        self.detail = dict(detail) if detail is not None else {}
        self.retry_after = retry_after
        super().__init__(f"{status} {code}")


@dataclass(frozen=True)
class HubOrigin:
    """Base URL (`scheme://host`), the signed host line and the WebSocket base URL of the hub."""

    base_url: str
    host: str
    ws_base_url: str


class _Trace:
    """Records the httpcore events of one attempt (the `trace` request extension of httpx)."""

    def __init__(self) -> None:
        self.events: set[str] = set()

    async def __call__(self, name: str, _info: Mapping[str, object]) -> None:
        self.events.add(name)

    @property
    def reused(self) -> bool | None:
        """True when the request was written on a pooled connection, None when the transport reported nothing."""
        if "http11.send_request_headers.started" not in self.events:
            return None
        return not any(name.startswith("connection.connect_") for name in self.events)

    @property
    def answered(self) -> bool:
        return "http11.receive_response_headers.complete" in self.events


@dataclass(frozen=True)
class SignedRequest:
    headers: dict[str, str]
    nonce: str
    agent_time: str


def hub_origin(hub_url: str) -> HubOrigin:
    """The origin of the configured hub URL (no user information, path, query or fragment; ValueError otherwise).

    The host line is the lower-case host, an IPv6 address in brackets, followed by `:port` only when the port is not
    the default of the scheme (the hub builds the same line from its public URL).
    """
    parts = urlsplit(hub_url)
    if parts.scheme not in _DEFAULT_PORTS or "@" in parts.netloc or parts.query or parts.fragment:
        raise ValueError("hub URL must be http(s)://host[:port]")
    if parts.path not in ("", "/") or "?" in hub_url or "#" in hub_url:
        raise ValueError("hub URL must not carry a path, query or fragment")
    url = httpx.URL(f"{parts.scheme}://{parts.netloc}/")
    host = url.raw_host.decode("ascii").lower()
    if host == "":
        raise ValueError("hub URL has no host")
    if ":" in host:
        host = f"[{host}]"
    if url.port is not None and url.port != _DEFAULT_PORTS[parts.scheme]:
        host = f"{host}:{url.port}"
    return HubOrigin(
        base_url=f"{parts.scheme}://{host}", host=host, ws_base_url=f"{_WS_SCHEMES[parts.scheme]}://{host}"
    )


def format_time(seconds: float) -> str:
    """`YYYY-MM-DDTHH:MM:SSZ` of a Unix time (whole seconds)."""
    return datetime.fromtimestamp(int(seconds), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text: str) -> float | None:
    """Unix seconds of `YYYY-MM-DDTHH:MM:SSZ`, None for anything else."""
    if not _TIME_RE.fullmatch(text):
        return None
    try:
        moment = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    return moment.timestamp()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def encode_body(body: object) -> bytes:
    """Compact UTF-8 JSON with sorted keys: the same value always gives the same bytes, also after a round trip
    through `jsonb` (which reorders keys), so a retried sealed event is sent byte for byte as before."""
    return json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False, sort_keys=True).encode("utf-8")


def _error_code(body: bytes) -> tuple[str, dict[str, object]] | None:
    """`(error, detail)` of an error body `{"error": code[, "detail": {...}]}` with a well-formed code."""
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict) or "error" not in data or not set(data) <= {"error", "detail"}:
        return None
    code, detail = data["error"], data.get("detail", {})
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code) or not isinstance(detail, dict):
        return None
    return code, detail


def _retry_after(headers: httpx.Headers) -> int | None:
    value = headers.get("retry-after", "")
    if not value.isascii() or not value.isdigit():
        return None
    return min(max(int(value), 1), RETRY_AFTER_MAX_S)


class Signer:
    """Signs requests with the device key (`device.<32 hex>`) for one hub host line."""

    def __init__(self, *, key_id: str, signing_key: Ed25519PrivateKey, host: str, wall: Callable[[], float]) -> None:
        self.key_id = key_id
        self._key = signing_key
        self.host = host
        self.wall = wall

    def sign(self, method: str, target: str, body: bytes) -> SignedRequest:
        if not _TARGET_RE.fullmatch(target):
            raise ValueError("request target must be a visible-ASCII absolute path")
        agent_time = format_time(self.wall())
        nonce = b64u_encode(secrets.token_bytes(NONCE_BYTES))
        text = request_text(self.key_id, method, self.host, target, agent_time, nonce, sha256_hex(body))
        signature = b64u_encode(self._key.sign(text.encode("utf-8")))
        headers = {
            "Agent-Key": self.key_id,
            "Agent-Time": agent_time,
            "Agent-Nonce": nonce,
            "Agent-Signature": signature,
        }
        return SignedRequest(headers=headers, nonce=nonce, agent_time=agent_time)


def verify_answer(
    hub_key: Ed25519PublicKey,
    signed: SignedRequest,
    key_id: str,
    *,
    status: int,
    headers: httpx.Headers,
    body: bytes,
    now: float,
) -> None:
    """Accept an answer only when the pinned hub key signed it for this request within the time window."""
    if not any(headers.get_list(name) for name in HUB_HEADERS):
        claimed = _error_code(body) if status >= 400 else None
        raise HubTransportError("response_unsigned", status=status, claimed=None if claimed is None else claimed[0])
    values: dict[str, str] = {}
    for name in HUB_HEADERS:
        found = headers.get_list(name)
        if len(found) != 1:
            raise HubTransportError("response_header_invalid", status=status)
        values[name] = found[0]
    hub_time = values["Agent-Hub-Time"]
    hub_seconds = parse_time(hub_time)
    if not _HUB_KEY_ID_RE.fullmatch(values["Agent-Hub-Key"]) or hub_seconds is None:
        raise HubTransportError("response_header_invalid", status=status)
    try:
        signature = b64u_decode(values["Agent-Hub-Signature"])
    except ValueError:
        signature = b""
    if len(signature) != SIGNATURE_BYTES:
        raise HubTransportError("response_header_invalid", status=status)
    text = response_text(key_id, signed.nonce, signed.agent_time, status, hub_time, sha256_hex(body))
    try:
        hub_key.verify(signature, text.encode("utf-8"))
    except InvalidSignature:
        raise HubTransportError("response_signature_invalid", status=status) from None
    if abs(hub_seconds - now) > TIME_WINDOW_S:
        raise HubTransportError("response_time_skew", status=status)


class HubClient:
    """Signed requests to the hub with verified answers; `request` returns the JSON object of a 2xx answer."""

    def __init__(
        self,
        *,
        hub_url: str,
        key_id: str,
        signing_key: Ed25519PrivateKey,
        hub_key: bytes,
        transport: httpx.AsyncBaseTransport | None = None,
        wall: Callable[[], float] = time.time,
        timeout: float = REQUEST_TIMEOUT_S,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
        keepalive_expiry: float = KEEPALIVE_EXPIRY_S,
    ) -> None:
        self.origin = hub_origin(hub_url)
        self.signer = Signer(key_id=key_id, signing_key=signing_key, host=self.origin.host, wall=wall)
        self._hub_key = Ed25519PublicKey.from_public_bytes(hub_key)
        self._max = max_response_bytes
        self._client = httpx.AsyncClient(
            transport=transport,
            follow_redirects=False,
            timeout=timeout,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=MAX_CONNECTIONS,
                max_keepalive_connections=MAX_IDLE_CONNECTIONS,
                keepalive_expiry=keepalive_expiry,
            ),
        )

    @property
    def key_id(self) -> str:
        return self.signer.key_id

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _read(self, response: httpx.Response) -> bytes:
        if "content-encoding" in response.headers:
            raise HubTransportError("response_encoded", status=response.status_code)
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_raw():
            size += len(chunk)
            if size > self._max:
                raise HubTransportError("response_too_large", status=response.status_code)
            chunks.append(chunk)
        return b"".join(chunks)

    async def _send(
        self, method: str, target: str, raw: bytes, json_body: bool
    ) -> tuple[SignedRequest, int, httpx.Headers, bytes]:
        """One attempt: sign, send and read; `HubTransportError("hub_unreachable")` with `reused` and `cause`."""
        signed = self.signer.sign(method, target, raw)
        headers = {**signed.headers, "Accept-Encoding": "identity"}
        if json_body:
            headers["Content-Type"] = "application/json"
        trace = _Trace()
        request = self._client.build_request(
            method, self.origin.base_url + target, headers=headers, content=raw, extensions={"trace": trace}
        )
        try:
            response = await self._client.send(request, stream=True)
            try:
                status = response.status_code
                answer = await self._read(response)
            finally:
                await response.aclose()
        except httpx.HTTPError as error:
            stale = isinstance(error, _STALE_ERRORS) and trace.reused is True and not trace.answered
            kind = _StaleConnection if stale else HubTransportError
            raise kind("hub_unreachable", reused=trace.reused, cause=type(error).__name__) from None
        return signed, status, response.headers, answer

    async def request(self, method: str, target: str, body: object | None = None) -> dict[str, object]:
        """Send one signed request; the verified JSON object of a 2xx answer, `HubRefusal` or `HubTransportError`.

        A request that failed on a reused connection before its answer began is sent once more (module docstring).
        """
        raw = b"" if body is None else encode_body(body)
        try:
            signed, status, headers, answer = await self._send(method, target, raw, body is not None)
        except _StaleConnection:
            signed, status, headers, answer = await self._send(method, target, raw, body is not None)
        verify_answer(
            self._hub_key,
            signed,
            self.key_id,
            status=status,
            headers=headers,
            body=answer,
            now=self.signer.wall(),
        )
        if 300 <= status < 400:
            raise HubTransportError("redirect_refused", status=status)
        if status >= 400:
            refusal = _error_code(answer)
            if refusal is None or refusal[0] not in HUB_ERROR_CODES:
                raise HubTransportError("response_invalid", status=status)
            raise HubRefusal(status, refusal[0], refusal[1], _retry_after(headers))
        try:
            data = json.loads(answer)
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict) or not 200 <= status < 300:
            raise HubTransportError("response_invalid", status=status)
        return data
