"""Org-device test kit of the RUN-06 suites: an org with a device invitation, its sealed package and a hub that serves
the anonymous lookup, the org enrollment and the device read endpoints.

`OrgWorld` extends the RUN-03 `World` (`reconcile/rig.py`): an org `o` founded by `alice` (not private) with the
workgroup `one`, `bob` (write on `one`) and a device invitation of `odev` for `one`; the package is sealed with the code
of `odev` and pins the log head right after the `invite` entry. Everything is built with the reference builders of the
crypto copy (`crypto.log.vectors`), independently of the runner code under test.

`OrgHub` answers `POST /v1/invites/lookup` without signatures and `POST /v1/device/enroll` signed with the key in the
body (it applies the `accept` to the org log like the hub's append); afterwards it authenticates the enrolled device
like `PlatformHub`. Every request and answer is validated against `contracts/`.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from agent_runner.crypto.invite import format_code, seal_package
from agent_runner.crypto.log import vectors as v
from agent_runner.crypto.log.entries import entry_hash
from agent_runner.crypto.log.platform import verify_platform_log
from agent_runner.crypto.log.verify import verify_log
from agent_runner.encoding import b64u_decode, b64u_encode
from agent_runner.host import Host
from agent_runner.identity import Device, DeviceKeys, Identity
from agent_runner.store.cluster import ClusterTools
from agent_runner.store.pool import open_pool
from agent_runner.sync.runtime import SyncOptions

from ..reconcile.rig import HUB_URL, ROOT, Clock, Env, OrgSpec, PlatformHub, World, device_keys, pid
from ..sync.fakehub import Received

Json = dict[str, Any]
ODEV = "odev"
WALL = 1_795_000_000.0
HOST = "127.0.0.1:9"


def code_text(label: str = ODEV) -> str:
    return format_code(v.code(label))


def org_spec(*, platform_device: bool = False, invite: bool = True) -> OrgSpec:
    """genesis `alice`; workgroup `one`; `bob` with write on `one`; the device invitation of `odev` for `one`
    (package head); with `platform_device` the platform device `rdev` is assigned to the default workgroup after it."""
    builder = v.LogBuilder(org=pid("o/org"))
    default_wg = pid("o/wg/default")
    one = pid("o/wg/one")
    builder.genesis("alice", default_wg=default_wg, private=False)  # 0
    builder.add("wg_create", "alice", wg=one, epoch=1)  # 1
    builder.invite_user("alice", "bob", wgs={one: "write"})  # 2
    builder.accept("bob")  # 3
    if invite:
        builder.invite_device("alice", ODEV, wgs=[one])  # 4
    if platform_device:
        builder.add("device_assign", "alice", device=pid("rdev"), wg=default_wg, platform_seq=2)
    return OrgSpec(label="o", log=builder, default_wg=default_wg)


ONE = pid("o/wg/one")


def package(spec: OrgSpec, *, head_seq: int = 4, keys: list[Json] | None = None, hub_public: bytes = b"") -> Json:
    entries = spec.log.entries
    return {
        "org": spec.org,
        "org_name": "Org Ö",
        "wg_names": {ONE: "Eins"},
        "founder": v.sig_pk("alice"),
        "head": {"seq": head_seq, "hash": entry_hash(entries[head_seq])},
        "keys": keys
        if keys is not None
        else [
            {"scope": "org", "scope_id": spec.org, "epoch": 1, "key": b64u_encode(v.scope_key("org", spec.org, 1))},
            {"scope": "wg", "scope_id": ONE, "epoch": 1, "key": b64u_encode(v.scope_key("wg", ONE, 1))},
            # not entitled: the default workgroup is not invited for the device
            {
                "scope": "wg",
                "scope_id": spec.default_wg,
                "epoch": 1,
                "key": b64u_encode(v.scope_key("wg", spec.default_wg, 1)),
            },
        ],
        "hub_key": {"id": "hub-test", "public_key": b64u_encode(hub_public)},
        "v": 1,
    }


@dataclass
class OrgWorld(World):
    spec: OrgSpec = field(default_factory=org_spec)
    lookup_status: int = 200
    lookup_body: object = None
    """None: the real answer; bytes: sent as is; otherwise JSON."""
    lookup_changes: Json = field(default_factory=dict)
    sealed: str | None = None
    enroll_answer: tuple[int, Json] | None = None
    accepted: list[Json] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.add_org(self.spec)


class OrgHub(PlatformHub):
    world: OrgWorld

    def __init__(self, world: OrgWorld) -> None:
        super().__init__(device_id="", device_public=bytes(32), host=HOST)
        self.world = world
        self.wall = lambda: WALL
        self.enrolled_body: Json | None = None

    def lookup_answer(self) -> tuple[int, object]:
        world = self.world
        if world.lookup_body is not None:
            return world.lookup_status, world.lookup_body
        entries = world.entries(world.spec.org)
        answer: Json = {
            "kind": "device",
            "scope": "org",
            "org_id": world.spec.org,
            "expires_at": v.EXPIRES,
            "registered": False,
            "package_ct": world.sealed or self.seal(),
            "log_head": world.head(entries),
            "log": entries,
        }
        answer.update(world.lookup_changes)
        answer = {k: val for k, val in answer.items() if val is not None}
        if world.lookup_status == 200:
            self.check("api/invites-lookup.response.json", answer)
        return world.lookup_status, answer

    def seal(self, spec: OrgSpec | None = None, label: str = ODEV, **changes: Any) -> str:
        return seal_package(v.secrets(label), package(spec or self.world.spec, hub_public=self.public, **changes))

    async def __call__(self, scope: dict[str, Any], receive: Callable[..., Any], send: Callable[..., Any]) -> None:
        if scope["raw_path"] == b"/v1/invites/lookup":
            message = await receive()
            body = json.loads(message.get("body", b"") or b"null")
            self.requests.append(Received("POST", "/v1/invites/lookup", body))
            self.check("api/invites-lookup.request.json", body)
            status, answer = self.lookup_answer()
            raw = answer if isinstance(answer, bytes) else json.dumps(answer).encode()
            await send({"type": "http.response.start", "status": status, "headers": []})
            await send({"type": "http.response.body", "body": raw})
            return
        await super().__call__(scope, receive, send)

    def verify(self, method: str, target: str, headers: dict[str, str], body: bytes) -> str | None:
        if target == "/v1/device/enroll":
            data = json.loads(body) if body else {}
            self.device_id = data.get("device_id", "")
            self.device_public = b64u_decode(data.get("pub", {}).get("sig", b64u_encode(bytes(32))))
        return super().verify(method, target, headers, body)

    def route(self, method: str, target: str, body: Any) -> tuple[int, dict[str, Any]]:
        if (method, target) == ("POST", "/v1/device/enroll"):
            return self.enroll(body)
        return super().route(method, target, body)

    def enroll(self, body: Json) -> tuple[int, Json]:
        self.check("api/device-enroll.request.json", body)
        world = self.world
        self.enrolled_body = body
        if world.enroll_answer is not None:
            return world.enroll_answer
        spec = world.spec
        entry = body["entry"]
        # the hub's append: L1-L8 of the whole log with the entry (the platform log of the world)
        platform = verify_platform_log(world.platform.entries, ROOT).state
        verify_log([*spec.log.entries, entry], v.sig_pk("alice"), platform=platform)
        spec.log.entries.append(entry)
        world.accepted.append(entry)
        answer = {"state": "active", "org_id": spec.org}
        self.check("api/device-enroll.response.json", answer)
        return 200, answer


def odev_keys() -> DeviceKeys:
    return device_keys(ODEV)


def cluster_tools(dsn: str, stops: list[Path]) -> ClusterTools:
    """Cluster operations that hand out the test database and record stops."""
    return ClusterTools(
        ensure=lambda path: "postgresql://unused",
        stop=stops.append,
        create_database=lambda admin, name: dsn,
        exists=lambda path: True,
    )


@asynccontextmanager
async def org_env(dsn: str, world: OrgWorld, hub: OrgHub) -> AsyncIterator[Env]:
    """A daemon-like context of the enrolled org device `odev` (the store holds what the enrollment wrote)."""
    keys = odev_keys()
    hub.device_id = pid(ODEV)
    hub.device_public = keys.signing_key().public_key().public_bytes_raw()
    device = Device(
        device_id=pid(ODEV),
        kind="org",
        hub_url=HUB_URL,
        hub_key=b64u_encode(hub.public),
        enrolled_at="2026-11-21T00:00:00.000Z",
    )
    clock = Clock()
    transport = httpx.ASGITransport(app=hub)  # type: ignore[arg-type]
    options = SyncOptions(transport=transport, wall=hub.wall, random=lambda: 0.5)
    pool = await open_pool(dsn, max_size=4)
    context = SimpleNamespace(
        identity=Identity(device=device, keys=keys),
        host=Host(env={}, clock=clock, sleep=clock.sleep),
        dsn=dsn,
        pool=pool,
        log=io.StringIO(),
        sync_options=options,
        config=SimpleNamespace(platform_root_key=ROOT),
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
