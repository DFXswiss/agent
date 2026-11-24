"""Test kit of the RUN-09 suites (requests, credentials, github): a hub with the device-request and credential
endpoints, users that seal requests and read responses, records sealed under the owner's `CK`, and a daemon-like
context whose org logs are verified by the real reconcile steps.

`CredHub` extends the platform hub of the reconcile rig (signature gate, signed answers, contract validation of every
answer) with `GET /v1/device/requests/{id}`, `POST /v1/device/requests/{id}/responses` (gapless `seq`, identical
retries, `403 forbidden` once a final response arrived or the request was closed), `GET|PUT
/v1/device/credentials/{owner}/{kind}` (author signature with the device key, `expected_version`, the current `CK`
epoch, an active request of the owner) and `requests`/`credentials_changed` in the desired state. Requests, responses
and records are built and opened with the crypto copy's reference functions and the vector identities, independently
of the code under test.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit

import httpx

from agent_runner.crypto import credential as credential_format
from agent_runner.crypto import request as request_format
from agent_runner.crypto.encoding import body_json, parse_json
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log import vectors as v
from agent_runner.encoding import b64u_encode
from agent_runner.github.client import GitHubOptions
from agent_runner.host import Host
from agent_runner.identity import Device, Identity
from agent_runner.orgs.book import book_for
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import store_scope_key
from agent_runner.reconcile import logs, state, wraps
from agent_runner.reconcile.engine import Run
from agent_runner.sessions.records import time_text
from agent_runner.store.pool import open_pool
from agent_runner.sync.outbox import open_item
from agent_runner.sync.runtime import SyncOptions, SyncRuntime, runtime

from ..reconcile.rig import DEVICE, HUB_URL, ROOT, Clock, PlatformHub, World, device_keys, org_log, pid, standard_world

Json = dict[str, Any]
__all__ = ["DEVICE", "pid"]
WALL = 1_795_000_000.0
CLIENT_ID = "Iv1.0000double0000"
ALICE = pid("alice")
BOB = pid("bob")


def ms(seconds: float) -> str:
    return time_text(seconds)


def ck(owner: str = ALICE, epoch: int = 1) -> bytes:
    return v.scope_key("cred", owner, epoch)


def seal_request(
    kind: str,
    body: object,
    *,
    user: str = "alice",
    device: str = DEVICE,
    follows: str = "",
    ts: float = WALL,
    request_id: str | None = None,
    signer: str | None = None,
    raw: bytes | None = None,
    encrypt_to: str | None = None,
) -> Json:
    """A device request of `user` to `device` (signed by `signer`, default the user; encrypted to `encrypt_to`,
    default the device)."""
    header = {
        "v": 1,
        "id": request_id or uuid7(),
        "user": pid(user),
        "device": pid(device),
        "kind": kind,
        "follows": follows,
        "ts": ms(ts),
        "enc_alg": request_format.ENC_ALG,
    }
    target = v.identity(encrypt_to or device)
    plaintext = raw if raw is not None else body_json(body)
    return request_format.seal_request(header, target.kem_public(), plaintext, v.identity(signer or user).sig)


def make_record(
    body: Json,
    *,
    owner: str = ALICE,
    kind: str = "github",
    version: int = 1,
    epoch: int = 1,
    author: str = "alice",
    record_id: str | None = None,
    key: bytes | None = None,
    ts: float = WALL,
) -> Json:
    header = credential_format.new_header(
        id=record_id or uuid7(),
        owner=owner,
        kind=kind,
        version=version,
        epoch=epoch,
        author=pid(author),
        ts=ms(ts),
    )
    return credential_format.seal_credential(key or ck(owner, epoch), header, body_json(body), v.identity(author).sig)


ACCOUNTS = {
    "double-user-token": 101,
    "double-other-token": 102,
    "operator-double-token-1": 501,
    "operator-double-token-2": 502,
}
"""The GitHub account ID of each token of the GitHub doubles' worlds (the account check, `GET /user`, KOR-47)."""


def github_body(token: str = "double-user-token", login: str = "octo-user") -> Json:
    """A credential body whose `user_id` is the account of the token (101 for unknown tokens)."""
    return {"token": token, "login": login, "user_id": ACCOUNTS.get(token, 101), "scopes": [], "created_at": ms(WALL)}


@dataclass
class Stored:
    envelope: Json
    responses: list[Json] = field(default_factory=list)
    closed: bool = False


class CredHub(PlatformHub):
    """PlatformHub with device requests and credentials (03-api.md section 6.1)."""

    stored_requests: dict[str, Stored]
    listed: list[str] | None
    records: dict[tuple[str, str], Json]
    changed: list[str]
    ck_epoch: int
    put_refusals: list[tuple[int, Json]]
    get_refusals: list[tuple[int, Json]]
    put_lost: list[str]
    """Per PUT, in order: `before` (connection lost before the hub stored), `after` (stored, answer lost), `""`."""

    def setup(self) -> None:
        self.stored_requests, self.listed, self.records, self.changed = {}, None, {}, []
        self.ck_epoch, self.put_refusals, self.get_refusals, self.put_lost = 1, [], [], []

    def add_request(self, envelope: Json) -> str:
        self.check("api/device-request-get.response.json", {"envelope": envelope})
        request_id = str(envelope["header"]["id"])
        self.stored_requests[request_id] = Stored(envelope)
        return request_id

    def active_state(self) -> dict[str, Any]:
        answer = super().active_state()
        active = [r for r, found in self.stored_requests.items() if not found.closed]
        answer["requests"] = [{"request_id": r} for r in (self.listed if self.listed is not None else active)]
        answer["credentials_changed"] = list(self.changed)
        return answer

    def route(self, method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:  # noqa: PLR0911
        path = urlsplit(target).path
        parts = path.strip("/").split("/")
        if parts[:3] == ["v1", "device", "requests"] and len(parts) == 4 and method == "GET":
            found = self.stored_requests.get(parts[3])
            if found is None:
                return 404, {"error": "not_found"}
            answer = {"envelope": found.envelope}
            self.check("api/device-request-get.response.json", answer)
            return 200, answer
        if parts[:3] == ["v1", "device", "requests"] and len(parts) == 5 and method == "POST":
            return self.response(parts[3], body)
        if parts[:3] == ["v1", "device", "credentials"] and len(parts) == 5:
            owner, kind = parts[3], parts[4]
            if method == "GET":
                if self.get_refusals:
                    refusal = self.get_refusals.pop(0)
                    if refusal[0] == 0:  # an answer that does not verify: a transport error for the runner
                        self.tamper.insert(0, "bad_signature")
                        return 200, {}
                    return refusal
                record = self.records.get((owner, kind))
                if record is None:
                    return 404, {"error": "not_found"}
                answer = {"record": record}
                self.check("api/device-credential-get.response.json", answer)
                return 200, answer
            if method == "PUT":
                return self.put(owner, kind, body)
        return super().route(method, target, body)

    def response(self, request_id: str, body: Any) -> tuple[int, dict[str, Any]]:
        self.check("api/device-request-responses.request.json", body)
        found = self.stored_requests.get(request_id)
        if found is None:
            return 404, {"error": "not_found"}
        envelope = body["envelope"]
        header = request_format.verify_response(envelope, self.device_public)
        if header["in_reply_to"] != request_id or header["device"] != self.device_id:
            return 400, {"error": "header_invalid"}
        seq = cast(int, header["seq"])
        if seq <= len(found.responses) and found.responses[seq - 1] == envelope:
            return 200, {}
        if found.closed:
            return 403, {"error": "forbidden"}
        if seq != len(found.responses) + 1:
            return 409, {"error": "seq_conflict", "detail": {"expected_seq": len(found.responses) + 1}}
        found.responses.append(envelope)
        if header["final"]:
            found.closed = True
            root = str(found.envelope["header"]["follows"]) or request_id
            for other in self.stored_requests.values():
                if other.envelope["header"]["id"] == root or other.envelope["header"]["follows"] == root:
                    other.closed = True
        return 200, {}

    def put(self, owner: str, kind: str, body: Any) -> tuple[int, dict[str, Any]]:  # noqa: PLR0911
        self.check("api/device-credential-put.request.json", body)
        lost = self.put_lost.pop(0) if self.put_lost else None
        if lost == "before":  # the connection broke before the hub read the request
            raise httpx.ReadError("connection lost")
        if self.put_refusals:
            return self.put_refusals.pop(0)
        record = body["record"]
        header = credential_format.verify_credential(record, self.device_public)
        request = self.stored_requests.get(body["request_id"])
        if request is None or request.closed or request.envelope["header"]["user"] != owner:
            return 403, {"error": "forbidden"}
        if (header["owner"], header["kind"], header["author"]) != (owner, kind, self.device_id):
            return 400, {"error": "header_invalid"}
        if header["epoch"] != self.ck_epoch:
            return 400, {"error": "epoch_stale"}
        current = self.records.get((owner, kind))
        version = 0 if current is None else int(current["header"]["version"])
        if current == record:  # a repeat of the stored bytes: answered with the stored version (like the hub)
            return 200, {"version": version}
        if body["expected_version"] != version:
            return 409, {"error": "version_conflict", "detail": {"expected": version}}
        if header["version"] != version + 1:
            return 400, {"error": "request_invalid"}
        self.records[(owner, kind)] = record
        if lost == "after":  # stored, but the answer was lost on the way back
            raise httpx.ReadError("connection lost")
        return 200, {"version": version + 1}

    def responses(self, request_id: str, user: str = "alice") -> list[tuple[Json, Json]]:
        """`(header, body)` of every response to the request, opened as the user."""
        out: list[tuple[Json, Json]] = []
        for envelope in self.stored_requests[request_id].responses:
            header = request_format.verify_response(envelope, self.device_public)
            plain = request_format.open_response(v.identity(user).kem, envelope, self.device_public)
            out.append((header, cast(Json, parse_json(plain))))
        return out

    def bodies(self, request_id: str, user: str = "alice") -> list[Json]:
        return [body for _header, body in self.responses(request_id, user)]


@dataclass
class Env:
    context: Any
    hub: CredHub
    world: World
    clock: Clock

    @property
    def sync(self) -> SyncRuntime:
        built = runtime(self.context)
        assert built is not None
        return built

    @property
    def pool(self) -> Any:
        return self.context.pool

    def log(self) -> str:
        return str(self.context.log.getvalue())

    async def rows(self, sql: str, *params: object) -> list[tuple[Any, ...]]:
        async with self.pool.connection() as conn:
            cur = await conn.execute(sql, params or None)
            return list(await cur.fetchall())

    async def audits(self) -> list[Json]:
        rows = await self.rows("SELECT origin_seq, item_kind, body_ct FROM runner.outbox ORDER BY origin_seq")
        return [open_item(self.sync.storage_key, seq, kind, bytes(blob)) for seq, kind, blob in rows if kind == "audit"]

    async def verify_logs(self) -> Run:
        """Reconcile steps 1-3 (state, logs, wraps): the book holds the verified org copies."""
        run = Run(context=self.context, sync=self.sync, book=book_for(self.context))
        await state.fetch_state(run)
        await logs.sync_logs(run)
        await wraps.step(run)
        return run

    async def desired(self) -> Run:
        run = Run(context=self.context, sync=self.sync, book=book_for(self.context))
        await state.fetch_state(run)
        return run

    async def add_ck(self, owner: str = ALICE, epoch: int = 1, key: bytes | None = None) -> None:
        async with session(self.pool) as conn:
            await store_scope_key(
                conn, self.sync.storage_key, ("cred", owner, epoch), key or ck(owner, epoch), source="wrap"
            )

    async def cache_rows(self) -> list[tuple[Any, ...]]:
        return await self.rows(
            "SELECT owner_id::text, kind, version, record FROM runner.credential_cache ORDER BY kind"
        )


def world_with_users() -> World:
    """Org `a` (founder alice; bob reads workgroup one; the device on the default workgroup) and org `z` without the
    device, founded by `zed` (a user the device does not know: the hub does not list org `z` to it)."""
    world = standard_world("a")
    spec = world.add_org(org_log("z", founder="zed", device=None, extra_wg=False))
    world.listed = [org for org in world.orgs if org != spec.org]  # the hub lists only the device's orgs
    return world


def config(github_url: str = "http://127.0.0.1:9") -> SimpleNamespace:
    return SimpleNamespace(
        platform_root_key=ROOT,
        github_client_id=CLIENT_ID,
        github_api_url=github_url,
        github_web_url=github_url,
        operator_github_token_file=None,
        operator_github_users=frozenset(),
        operator_github_org=None,
    )


@asynccontextmanager
async def kit_env(
    dsn: str,
    world: World | None = None,
    *,
    github_url: str = "http://127.0.0.1:9",
    github_wall: Callable[[], float] | None = None,
    package: str | None = None,
    verified: bool = True,
) -> AsyncIterator[Env]:
    world = world or world_with_users()
    keys = device_keys()
    device_id = pid(DEVICE)
    hub = CredHub(
        device_id=device_id, device_public=keys.signing_key().public_key().public_bytes_raw(), host="127.0.0.1:9"
    )
    hub.setup()
    hub.world = world
    hub.wall = lambda: WALL
    device = Device(
        device_id=device_id,
        kind="platform",
        hub_url=HUB_URL,
        hub_key=b64u_encode(hub.public),
        enrolled_at="2026-11-21T00:00:00.000Z",
    )
    clock = Clock()
    transport = httpx.ASGITransport(app=hub)  # type: ignore[arg-type]
    host = Host(env={}, clock=clock, sleep=clock.sleep)
    if package is not None:
        host.package = package
    pool = await open_pool(dsn, max_size=6)
    context = SimpleNamespace(
        identity=Identity(device=device, keys=keys),
        host=host,
        dsn=dsn,
        pool=pool,
        log=io.StringIO(),
        sync_options=SyncOptions(transport=transport, wall=hub.wall, random=lambda: 0.5),
        github_options=GitHubOptions(wall=github_wall or (lambda: WALL)),
        config=config(github_url),
    )
    context.stopping = asyncio.Event()
    env = Env(context=context, hub=hub, world=world, clock=clock)
    try:
        if verified:
            await env.verify_logs()
        yield env
    finally:
        built = getattr(context, "sync_runtime", None)
        if built is not None:
            await built.close()
        await pool.close()
    assert hub.violations == [], hub.violations


def github_port(offset: int) -> int:
    """A loopback port of the package range for a GitHub double (`WP_PORT_BASE + 2 .. + 9`), 0 outside it."""
    base = os.environ.get("WP_PORT_BASE")
    return int(base) + 2 + offset % 8 if base else 0


def dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True)
