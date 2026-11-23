"""A fake hub for the sync tests: ASGI app for HTTP plus a loopback WebSocket server (03-api.md sections 1, 6.1, 6.2).

It behaves like the hub where the runner can observe it and is written independently of the runner code:

- every request must carry a valid `agent-request-v1` signature of the enrolled device (time window 120 s, nonce
  replay refused), every answer is signed with `agent-response-v1` by the hub key;
- every request body, answer body and WebSocket frame is validated against `contracts/` (a violation fails the test
  through `violations`);
- `POST /v1/device/outbox` follows the rules of 03-api.md section 6.1: strict `last + 1`, replay of identical items,
  conflict on different bytes, `reseal` for an event with a stale epoch (E6, processing stops there), `rejected` for
  a poison item (the sequence continues), transactional refusals; events are verified with the device key (E1, E2,
  E4, E8) before they are stored.

Tamper switches (`tamper`) corrupt the next answers in the ways a hostile network or hub could.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from agent_guest.schema import SchemaSet
from agent_runner.crypto.event import verify_event
from agent_runner.encoding import b64u_decode, b64u_encode

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
SCHEMAS = SchemaSet(CONTRACTS)
WINDOW = 120


def utc(seconds: float) -> str:
    return datetime.fromtimestamp(int(seconds), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_ms(seconds: float) -> str:
    return datetime.fromtimestamp(int(seconds), UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def request_lines(fields: tuple[str, str, str, str, str, str], body: bytes) -> bytes:
    """`fields` = key ID, method, host, target, time, nonce."""
    lines = ["agent-request-v1", *fields, hashlib.sha256(body).hexdigest()]
    return "\n".join(lines).encode()


def response_lines(fields: tuple[str, str, str, int, str], body: bytes) -> bytes:
    """`fields` = key ID, nonce, request time, status, hub time."""
    lines = ["agent-response-v1", *map(str, fields), hashlib.sha256(body).hexdigest()]
    return "\n".join(lines).encode()


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@dataclass
class Received:
    method: str
    target: str
    body: Any
    raw: bytes = b""


@dataclass
class FakeHub:
    device_id: str
    device_public: bytes
    host: str
    wall: Callable[[], float] = time.time
    hub_seed: bytes = bytes(range(100, 132))
    hub_key_id: str = "hub-test"
    # hub state
    stored: dict[int, bytes] = field(default_factory=dict)
    epochs: dict[str, int] = field(default_factory=dict)
    reject: Callable[[dict[str, Any]], str | None] = lambda item: None
    device_state: dict[str, Any] | None = None
    # observation and fault injection
    requests: list[Received] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    nonces: set[str] = field(default_factory=set)
    tamper: list[str] = field(default_factory=list)
    scripted: list[tuple[int, dict[str, Any], dict[str, str]]] = field(default_factory=list)
    frames: list[dict[str, Any]] = field(default_factory=list)
    connections: int = 0
    refuse_ws: int = 0
    redirect_ws: int = 0
    to_send: asyncio.Queue[str | None] = field(default_factory=asyncio.Queue)
    server: Server | None = None
    ws_connection: ServerConnection | None = None

    def __post_init__(self) -> None:
        self.key = Ed25519PrivateKey.from_private_bytes(self.hub_seed)
        self.public = self.key.public_key().public_bytes_raw()

    @property
    def last(self) -> int:
        return max(self.stored, default=0)

    def check(self, schema: str, instance: object) -> None:
        errors = SCHEMAS.errors(schema, instance)
        if errors:
            self.violations.append(f"{schema}: {errors[:3]}")

    # --- signature gate -------------------------------------------------------------------------------------------

    def verify(self, method: str, target: str, headers: dict[str, str], body: bytes) -> str | None:
        """The error code of a request that fails the gate, None when it passes."""
        key, at, nonce, sig = (headers.get(h) for h in ("agent-key", "agent-time", "agent-nonce", "agent-signature"))
        if key is None or at is None or nonce is None or sig is None:
            return "signature_missing"
        if key != "device." + self.device_id.replace("-", ""):
            return "key_unknown"
        sent = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC).timestamp()
        if abs(sent - self.wall()) > WINDOW:
            return "time_skew"
        try:
            Ed25519PublicKey.from_public_bytes(self.device_public).verify(
                b64u_decode(sig), request_lines((key, method, self.host, target, at, nonce), body)
            )
        except Exception:
            return "signature_invalid"
        if len(b64u_decode(nonce)) != 16 or nonce in self.nonces:
            return "nonce_replayed"
        self.nonces.add(nonce)
        return None

    def sign(self, headers: dict[str, str], status: int, body: bytes) -> list[tuple[bytes, bytes]]:
        hub_time = utc(self.wall())
        request = (headers["agent-key"], headers["agent-nonce"], headers["agent-time"])
        text = response_lines((*request, status, hub_time), body)
        signature = b64u_encode(self.key.sign(text))
        mode = self.tamper.pop(0) if self.tamper else None
        out = [(b"agent-hub-key", self.hub_key_id.encode()), (b"agent-hub-time", hub_time.encode())]
        if mode == "stale_time":
            stale = utc(self.wall() - 600)
            text = response_lines((*request, status, stale), body)
            out = [(b"agent-hub-key", self.hub_key_id.encode()), (b"agent-hub-time", stale.encode())]
            signature = b64u_encode(self.key.sign(text))
        if mode == "other_key":
            signature = b64u_encode(Ed25519PrivateKey.generate().sign(text))
        if mode == "bad_signature":
            signature = b64u_encode(bytes(64))
        if mode == "short_signature":
            signature = b64u_encode(bytes(10))
        if mode == "padded_signature":
            signature += "="
        if mode == "bad_key_id":
            out[0] = (b"agent-hub-key", b"HUB KEY")
        if mode == "bad_time":
            out[1] = (b"agent-hub-time", b"yesterday")
        if mode == "impossible_time":
            out[1] = (b"agent-hub-time", b"2026-13-45T00:00:00Z")
        out.append((b"agent-hub-signature", signature.encode()))
        if mode == "duplicate_header":
            out.append((b"agent-hub-signature", signature.encode()))
        if mode == "unsigned":
            out = []
        if mode == "encoded":
            out.append((b"content-encoding", b"gzip"))
        self.mode = mode
        return out

    # --- ASGI -----------------------------------------------------------------------------------------------------

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        assert scope["type"] == "http"
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        body = b"".join(chunks)
        headers = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        target = scope["raw_path"].decode() + ("?" + scope["query_string"].decode() if scope["query_string"] else "")
        method = scope["method"]
        parsed = json.loads(body) if body else None
        self.requests.append(Received(method, target, parsed, body))
        extra: dict[str, str] = {}
        refusal = self.verify(method, target, headers, body)
        if refusal is not None:
            status, answer = 401, {"error": refusal}
            signed = False
        else:
            signed = True
            if self.scripted:
                status, answer, extra = self.scripted.pop(0)
            else:
                status, answer = self.route(method, target, parsed)
                if status >= 400:
                    self.check("api/error.response.json", answer)
        raw = json.dumps(answer).encode()
        out = self.sign(headers, status, raw) if signed else []
        if getattr(self, "mode", None) == "body_changed":
            raw = raw.replace(b"}", b" }", 1)
        if getattr(self, "mode", None) == "too_large":
            raw = raw + b" " * (9 * 1024 * 1024)
        out = [*out, (b"content-type", b"application/json")]
        out += [(k.encode(), v.encode()) for k, v in extra.items()]
        await send({"type": "http.response.start", "status": status, "headers": out})
        await send({"type": "http.response.body", "body": raw})

    def route(self, method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
        if (method, target) == ("POST", "/v1/device/outbox"):
            return self.outbox(body)
        if (method, target) == ("GET", "/v1/device/state"):
            state = self.device_state or self.active_state()
            self.check("api/device-state.response.json", state)
            return 200, state
        return 404, {"error": "not_found"}

    def active_state(self) -> dict[str, Any]:
        return {
            "state": "active",
            "last_origin_seq": self.last,
            "orgs": [],
            "assigned": [],
            "sessions": [],
            "pending_prompts": [],
            "delivered_prompt_event_ids": {},
            "requests": [],
            "pings": [],
            "credentials_changed": [],
            "rotation_pending": [],
            "quota_poll_owners": [],
            "platform_budget": None,
            "hub_time": utc_ms(self.wall()),
        }

    # --- outbox rules (03-api.md section 6.1) ---------------------------------------------------------------------

    def outbox(self, body: Any) -> tuple[int, dict[str, Any]]:
        self.check("api/device-outbox.request.json", body)
        if len(canonical(body)) > 4 * 1024 * 1024:
            return 413, {"error": "size_exceeded"}
        staged: dict[int, bytes] = {}
        results: list[dict[str, Any]] = []
        last = self.last
        for item in body["items"]:
            self.check(f"outbox/{item['item_kind']}.json", item["body"])
            seq, raw = item["origin_seq"], canonical(item)
            if seq in self.stored:
                if self.stored[seq] != raw:
                    return 409, {"error": "origin_seq_conflict"}
                results.append({"origin_seq": seq, "status": "replayed"})
                continue
            if seq != last + 1:
                return 409, {"error": "origin_seq_gap", "detail": {"expected": last + 1}}
            if item["item_kind"] == "event":
                header = verify_event(item["body"]["event"], self.device_public)
                current = self.epochs.get(str(header["scope_id"]), 1)
                if header["epoch"] != current:
                    results.append({"origin_seq": seq, "status": "reseal"})
                    break
            error = self.reject(item)
            if error is None:
                results.append({"origin_seq": seq, "status": "ok"})
            else:
                results.append({"origin_seq": seq, "status": "rejected", "error": error})
            staged[seq] = raw
            last = seq
        self.stored.update(staged)
        answer = {"acked_through": last, "results": results}
        self.check("api/device-outbox.response.json", answer)
        return 200, answer

    def items(self) -> list[dict[str, Any]]:
        return [json.loads(self.stored[seq]) for seq in sorted(self.stored)]

    # --- WebSocket ------------------------------------------------------------------------------------------------

    def process_request(self, connection: ServerConnection, request: Request) -> Response | None:
        headers = {k.lower(): v for k, v in request.headers.raw_items()}
        if self.refuse_ws > 0:
            self.refuse_ws -= 1
            return connection.respond(503, "unavailable\n")
        if self.redirect_ws > 0:
            self.redirect_ws -= 1
            return Response(302, "Found", Headers([("Location", "/v1/device/ws")]), b"")
        if request.path != "/v1/device/ws" or self.verify("GET", request.path, headers, b"") is not None:
            return connection.respond(401, "refused\n")
        return None

    async def handler(self, connection: ServerConnection) -> None:
        self.connections += 1
        self.ws_connection = connection
        sender = asyncio.ensure_future(self._send(connection))
        try:
            async for message in connection:
                frame = json.loads(message)
                self.check("ws/device/heartbeat.json", frame)
                self.frames.append(frame)
        except Exception:
            pass
        finally:
            sender.cancel()

    async def _send(self, connection: ServerConnection) -> None:
        while True:
            frame = await self.to_send.get()
            if frame is None:
                await connection.close()
                return
            try:
                parsed = json.loads(frame)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and parsed.get("type") not in (None, "bogus"):
                self.check("ws/device/hint.json", parsed)
            await connection.send(frame)

    async def start_ws(self, port: int) -> int:
        self.server = await serve(
            self.handler, "127.0.0.1", port, process_request=self.process_request, compression=None
        )
        return int(self.server.sockets[0].getsockname()[1])

    async def stop_ws(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
