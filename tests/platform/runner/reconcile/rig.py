"""Test rig of the RUN-03 suites: signed logs, wraps and events of a small platform, and a hub that serves them.

`World` builds real, signed data with the reference builders of the crypto copy (`crypto.log.vectors`): a platform
log (root, device invitation, certification of the runner device `rdev`), membership logs of two orgs that assign
`rdev` to a workgroup through its certification, wraps from the founders to `rdev` and sealed events. Nothing here
uses runner code beyond those builders and the format functions, so the runner is checked against independently
built data.

`PlatformHub` extends the fake hub of the sync suites (signature gate, signed answers, outbox rules, contract
validation of every answer) with `GET /v1/device/state`, `/v1/device/platform-log`, `/v1/device/logs`,
`/v1/device/wraps` and `/v1/device/pull`, answering from a `World`. Every answer is validated against `contracts/`.

`Rig.env()` is the async context: migrated store, pool, the device identity of `rdev`, the hub on an ASGI transport and
a daemon-like context (`config.platform_root_key` set).
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from agent_runner.crypto import event as event_format
from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.log import vectors as v
from agent_runner.crypto.log.entries import entry_hash
from agent_runner.crypto.vectors import fixed
from agent_runner.encoding import b64u_encode
from agent_runner.host import Host
from agent_runner.identity import Device, DeviceKeys, Identity
from agent_runner.store.pool import open_pool
from agent_runner.sync.outbox import open_item
from agent_runner.sync.runtime import SyncOptions, SyncRuntime, runtime

from ..sync.fakehub import FakeHub, utc_ms

Json = dict[str, Any]
DEVICE = "rdev"
ROOT = v.sig_pk("root")
HUB_URL = "http://127.0.0.1:9"


def pid(label: str) -> str:
    return v.pid(label)


def device_keys(label: str = DEVICE) -> DeviceKeys:
    return DeviceKeys(kem=fixed("log/" + label + "/kem"), sig=fixed("log/" + label + "/sig"))


def scope_key(scope: str, scope_id: str, epoch: int) -> bytes:
    return v.scope_key(scope, scope_id, epoch)


@dataclass
class OrgSpec:
    """One org of the world: its log builder and its workgroups."""

    label: str
    log: v.LogBuilder
    default_wg: str
    founder: str = "alice"

    @property
    def org(self) -> str:
        return self.log.org


def platform_log(device: str = DEVICE) -> v.PlatformBuilder:
    """Root, an invitation for `device` and its certification (seq 2)."""
    log = v.PlatformBuilder()
    log.add("root", pub=ROOT)
    found = v.secrets(device)
    log.add("device_invite", invite_id=found.invite_id, pk_inv=found.pk_inv, expires=v.EXPIRES)
    log.certify(device)
    return log


CERT_SEQ = 2


def org_log(
    label: str, founder: str = "alice", device: str | None = DEVICE, extra_wg: bool = True, private: bool = True
) -> OrgSpec:
    """genesis by `founder`; a second workgroup; `bob` (read access on it); `device` assigned to the default wg."""
    builder = v.LogBuilder(org=pid(f"{label}/org"))
    default_wg = pid(f"{label}/wg/default")
    builder.genesis(founder, default_wg=default_wg, private=private)  # 0 (private: platform access, ADR-026)
    if extra_wg:
        builder.add("wg_create", founder, wg=pid(f"{label}/wg/one"), epoch=1)  # 1
        builder.invite_user(founder, "bob", wgs={pid(f"{label}/wg/one"): "read"})  # 2
        builder.accept("bob")  # 3
    if device is not None:
        builder.add("device_assign", founder, device=pid(device), wg=default_wg, platform_seq=CERT_SEQ)
    return OrgSpec(label=label, log=builder, default_wg=default_wg, founder=founder)


def wrap_for(wrapper: str, scope: str, scope_id: str, epoch: int, recipient: str = DEVICE) -> Json:
    """A stored wrap (as `GET /v1/device/wraps` lists it)."""
    made = v.make_wrap(wrapper, scope, scope_id, epoch, recipient)
    return {**made, "wrapped_by": pid(wrapper), "created_at": "2026-11-21T12:00:00.000Z"}


def seal(
    org: OrgSpec,
    author: str,
    kind: str,
    *,
    scope: str = "wg",
    scope_id: str | None = None,
    epoch: int = 1,
    lh: int | None = None,
    label: str = "e",
    body: Json | None = None,
    key: bytes | None = None,
    **extra: str,
) -> Json:
    """A sealed event of `org` by `author` (wg scope of the default workgroup unless stated)."""
    target = scope_id or (org.default_wg if scope == "wg" else org.org)
    wg = target if scope == "wg" else extra.pop("wg", "")
    header = event_format.new_header(
        id=pid(f"event/{org.label}/{label}"),
        org=org.org,
        scope=scope,
        scope_id=target,
        epoch=epoch,
        author=pid(author),
        kind=kind,
        ts="2026-11-21T12:30:00.000Z",
        lh=len(org.log.entries) - 1 if lh is None else lh,
        wg=wg,
        **extra,
    )
    plaintext = body_json(body if body is not None else {"text": "Grüße " + label, "attachments": []})
    return event_format.seal_event(key or scope_key(scope, target, epoch), header, plaintext, v.identity(author).sig)


def prompt(org: OrgSpec, author: str = "alice", label: str = "p1", **kw: Any) -> Json:
    return seal(org, author, "prompt", label=label, task=pid(f"task/{label}"), **kw)


@dataclass
class World:
    platform: v.PlatformBuilder = field(default_factory=platform_log)
    orgs: dict[str, OrgSpec] = field(default_factory=dict)
    wraps: list[Json] = field(default_factory=list)
    events: dict[str, list[tuple[int, Json]]] = field(default_factory=dict)
    state: str = "active"
    # hub behaviour switches
    offered: dict[str, Json] = field(default_factory=dict)
    """org -> head offered in the state (default: the real head)."""
    served: dict[str, list[Json]] = field(default_factory=dict)
    """org -> entries served by /device/logs (default: the real log)."""
    served_platform: list[Json] | None = None
    page_size: int = 1000
    pull_page: int = 1000
    listed: list[str] | None = None
    """org IDs listed in the state (default: every org)."""

    def add_org(self, spec: OrgSpec) -> OrgSpec:
        self.orgs[spec.org] = spec
        return spec

    def add_event(self, scope_id: str, seq: int, sealed: Json) -> None:
        self.events.setdefault(scope_id, []).append((seq, sealed))
        self.events[scope_id].sort(key=lambda item: item[0])

    def entries(self, org: str) -> list[Json]:
        return self.served.get(org, self.orgs[org].log.entries)

    def head(self, entries: list[Json]) -> Json:
        return {"seq": len(entries) - 1, "hash": entry_hash(entries[-1])}


class PlatformHub(FakeHub):
    """FakeHub with the device read endpoints of 03-api.md section 6.1."""

    world: World
    hold: dict[str, list[Json]]

    def page(self, entries: list[Json], query: dict[str, list[str]], schema: str) -> tuple[int, Json]:
        self.check(
            schema.replace("response", "request"),
            {k: (vals if k == "cursor" else vals[0]) for k, vals in query.items()},
        )
        after = int(query["after"][0]) if "after" in query else -1
        limit = min(int(query.get("limit", ["200"])[0]), self.world.page_size)
        chosen = [e for e in entries if e["body"]["seq"] > after][:limit]
        last = chosen[-1]["body"]["seq"] if chosen else None
        more = last is not None and last < entries[-1]["body"]["seq"]
        answer = {
            "entries": [{"seq": e["body"]["seq"], "entry": e} for e in chosen],
            "head": self.world.head(entries),
            "next_after": last if more else None,
        }
        self.check(schema, answer)
        return 200, answer

    def route(self, method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
        parts = urlsplit(target)
        query = parse_qs(parts.query, keep_blank_values=True)
        world = self.world
        if method != "GET":
            return super().route(method, target, body)
        routes: dict[str, Callable[[], tuple[int, dict[str, Any]]]] = {
            "/v1/device/state": self.state_answer,
            "/v1/device/platform-log": lambda: self.page(
                world.served_platform if world.served_platform is not None else world.platform.entries,
                query,
                "api/device-platform-log.response.json",
            ),
            "/v1/device/logs": lambda: self.logs(query),
            "/v1/device/wraps": self.wraps,
            "/v1/device/pull": lambda: self.pull(query),
        }
        found = routes.get(parts.path)
        return found() if found is not None else super().route(method, target, body)

    def logs(self, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        org = query["org"][0]
        if org not in self.world.orgs:
            return 404, {"error": "not_found"}
        return self.page(self.world.entries(org), query, "api/device-logs.response.json")

    def wraps(self) -> tuple[int, dict[str, Any]]:
        answer = {"wraps": list(self.world.wraps)}
        self.check("api/device-wraps-get.response.json", answer)
        return 200, answer

    def state_answer(self) -> tuple[int, dict[str, Any]]:
        world = self.world
        if world.state == "pending_certification":
            answer: Json = {"state": "pending_certification", "hub_time": utc_ms(self.wall())}
        else:
            answer = self.active_state()
            listed = world.listed if world.listed is not None else list(world.orgs)
            answer["orgs"] = [
                {
                    "org_id": org,
                    "log_head": world.offered[org] if org in world.offered else world.head(world.entries(org)),
                }
                for org in listed
            ]
        self.check("api/device-state.response.json", answer)
        return 200, answer

    def pull(self, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        self.check(
            "api/device-pull.request.json", {k: (vals if k == "cursor" else vals[0]) for k, vals in query.items()}
        )
        cursors = dict(item.split(":") for item in query.get("cursor", []))
        limit = min(int(query.get("limit", ["200"])[0]), self.world.pull_page)
        found = [
            (scope_id, seq, sealed)
            for scope_id, after in cursors.items()
            for seq, sealed in self.world.events.get(scope_id, [])
            if seq > int(after)
        ]
        chosen = found[:limit]
        answer = {
            "events": [
                {"scope_id": s, "scope_seq": q, "received_at": "2026-11-21T12:31:00.000Z", "event": e}
                for s, q, e in chosen
            ],
            "more": len(found) > limit,
        }
        self.check("api/device-pull.response.json", answer)
        return 200, answer

    def targets(self, path: str) -> list[str]:
        return [r.target for r in self.requests if urlsplit(r.target).path == path]


@dataclass
class Env:
    context: Any
    hub: PlatformHub
    world: World
    clock: Any

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
        """The bodies of the audit outbox items, in order (opened with the storage key)."""
        rows = await self.rows("SELECT origin_seq, item_kind, body_ct FROM runner.outbox ORDER BY origin_seq")
        return [open_item(self.sync.storage_key, seq, kind, bytes(blob)) for seq, kind, blob in rows if kind == "audit"]


@dataclass
class Clock:
    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)
    on_sleep: Callable[[float], None] | None = None

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep is not None:
            self.on_sleep(seconds)
        await asyncio.sleep(0)


@dataclass
class Rig:
    dsn: str

    @asynccontextmanager
    async def env(self, world: World | None = None, *, enrolled: bool = True, root: str = ROOT) -> AsyncIterator[Env]:
        world = world or World()
        keys = device_keys()
        device_id = pid(DEVICE)
        hub = PlatformHub(
            device_id=device_id, device_public=keys.signing_key().public_key().public_bytes_raw(), host="127.0.0.1:9"
        )
        hub.world = world
        hub.wall = lambda: 1_795_000_000.0
        device = Device(
            device_id=device_id,
            kind="platform",
            hub_url=HUB_URL,
            hub_key=b64u_encode(hub.public),
            enrolled_at="2026-11-21T00:00:00.000Z",
        )
        clock = Clock()
        transport = httpx.ASGITransport(app=hub)  # type: ignore[arg-type]
        options = SyncOptions(transport=transport, wall=hub.wall, random=lambda: 0.5)
        host = Host(env={}, clock=clock, sleep=clock.sleep)
        pool = await open_pool(self.dsn, max_size=4)
        context = SimpleNamespace(
            identity=Identity(device=device, keys=keys) if enrolled else None,
            host=host,
            dsn=self.dsn,
            pool=pool,
            log=io.StringIO(),
            sync_options=options,
            config=SimpleNamespace(platform_root_key=root, operator_github_org=None, operator_github_token_file=None),
        )
        context.stopping = asyncio.Event()
        try:
            yield Env(context=context, hub=hub, world=world, clock=clock)
        finally:
            built = getattr(context, "sync_runtime", None)
            if built is not None:
                await built.close()
            await pool.close()
        assert hub.violations == [], hub.violations


def standard_world(*labels: str) -> World:
    """A world with the given orgs (default: one org `a`) and the wraps of their default workgroup and org key."""
    world = World()
    for label in labels or ("a",):
        spec = world.add_org(org_log(label))
        world.wraps.append(wrap_for(spec.founder, "wg", spec.default_wg, 1))
        world.wraps.append(wrap_for(spec.founder, "org", spec.org, 1))
    return world


def dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True)
