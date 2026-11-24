"""Test kit of the KOR-50 DFX key suites (ADR-080 section 7): an issuing platform device, an org device and the
model double with the OpenRouter key management API.

- `issuer_env(dsn, double)`: the RUN-03 rig (`reconcile/rig.py`) - the platform device `rdev` with a real store and
  sync runtime - plus the configuration of an issuer: the management key file, the platform key file and the model
  double as `MODEL_UPSTREAM_OPENROUTER`.
- `org_context(dsn)`: an org device `odev` with its own store and a verified copy of the platform log (`rdev` and
  `pdev2` certified, `qdev` certified then decertified) and of two orgs: `o`, which `odev` serves (it accepted its
  device invitation), and `x`, which it does not. Everything is built with the reference builders of the crypto copy
  (`crypto.log.vectors`), independently of the code under test.
- `double()`: a started model double with the management token, a scenario of OpenRouter-shaped answers (each with
  `COST` USD) and the platform key as its configured access.
"""

from __future__ import annotations

import io
import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from psycopg import sql

from agent_doubles.model.access import Access
from agent_doubles.model.scenario import parse_scenario
from agent_doubles.model.server import ModelDouble
from agent_runner.crypto.log import vectors as v
from agent_runner.crypto.log.platform import verify_platform_log
from agent_runner.crypto.log.verify import verify_log
from agent_runner.identity import Device, DeviceKeys, Identity
from agent_runner.orgs.book import ATTRIBUTE as BOOK_ATTRIBUTE
from agent_runner.orgs.book import OrgBook
from agent_runner.store.pool import open_pool
from agent_runner.sync.outbox import open_item

from ..reconcile.rig import ROOT, Env, Rig, device_keys, pid

Json = dict[str, Any]
MANAGEMENT = "test-management-key-MGMTKEYMARKER-0123456789abcdef"
PLATFORM = "test-platform-key-PLATKEYMARKER-0123456789abcdef"
COST = 0.25
ISSUER = pid("rdev")
ODEV = "odev"
ORG = pid("o/org")
OTHER_ORG = pid("x/org")
REQUEST = "01930050-0000-7000-8000-000000000001"
REQUEST_TWO = "01930050-0000-7000-8000-000000000002"
NOW = 1_795_000_000.0
QUESTION: Json = {"model": "m", "max_tokens": 10, "messages": [{"role": "user", "content": "q"}]}
FALLBACK: Json = {"model_fallback": {"api": 1}}


def odev_keys() -> DeviceKeys:
    return device_keys(ODEV)


def write_secret(path: Path, text: str) -> Path:
    path.write_text(text + "\n", encoding="ascii")
    os.chmod(path, 0o600)
    return path


@contextmanager
def double(steps: int = 20) -> Iterator[ModelDouble]:
    scenario = parse_scenario(
        {
            "name": "dfx-keys",
            "openrouter": {},
            "steps": [
                {"id": f"s{i}", "reply": {"content": [{"type": "text", "text": "ok"}], "cost": COST}}
                for i in range(steps)
            ],
        }
    )
    with ModelDouble(scenario, access=Access("openrouter", PLATFORM), management_token=MANAGEMENT) as started:
        yield started


def platform_entries() -> list[Json]:
    """root; `rdev` certified (seq 2); `pdev2` certified (seq 4); `qdev` certified (seq 6) and decertified (seq 7)."""
    log = v.PlatformBuilder()
    log.add("root", pub=ROOT)
    for label in ("rdev", "pdev2", "qdev"):
        found = v.secrets(label)
        log.add("device_invite", invite_id=found.invite_id, pk_inv=found.pk_inv, expires=v.EXPIRES)
        log.certify(label)
    log.add("device_decertify", device=pid("qdev"))
    return log.entries


def org_entries(label: str, device: str | None) -> list[Json]:
    """genesis `alice`; with `device` its invitation and acceptance (an org device member)."""
    builder = v.LogBuilder(org=pid(f"{label}/org"))
    wg = pid(f"{label}/wg/default")
    builder.genesis("alice", default_wg=wg, private=False)
    if device is not None:
        builder.invite_device("alice", device, wgs=[wg])
        builder.accept(device, kind="device")
    return builder.entries


def org_book(device: str = ODEV, platform: list[Json] | None = None) -> OrgBook:
    """The verified copies of an org device (the platform log and the orgs `o` and `x`)."""
    keys = device_keys(device)
    book = OrgBook(device_id=pid(device), kem_public=keys.kem_public, sig_public=keys.sig_public, root=ROOT, kind="org")
    verified = verify_platform_log(platform if platform is not None else platform_entries(), ROOT)
    book.platform = verified
    book.logs = {
        ORG: verify_log(org_entries("o", device), v.sig_pk("alice"), platform=verified.state),
        OTHER_ORG: verify_log(org_entries("x", None), v.sig_pk("alice"), platform=verified.state),
    }
    book.loaded = True
    return book


@asynccontextmanager
async def org_context(dsn: str, *, platform_key_file: Path | None = None) -> AsyncIterator[Any]:
    """A daemon-like context of the org device `odev` (its own store; no sync runtime is needed)."""
    keys = odev_keys()
    device = Device(
        device_id=pid(ODEV),
        kind="org",
        hub_url="http://127.0.0.1:9",
        hub_key="",
        enrolled_at="2026-11-24T00:00:00.000Z",
    )
    pool = await open_pool(dsn, max_size=2)
    context = SimpleNamespace(
        identity=Identity(device=device, keys=keys),
        pool=pool,
        log=io.StringIO(),
        config=SimpleNamespace(platform_root_key=ROOT, openrouter_key_file=platform_key_file),
    )
    setattr(context, BOOK_ATTRIBUTE, org_book())
    try:
        yield context
    finally:
        await pool.close()


@dataclass
class Issuing:
    env: Env
    double: ModelDouble

    @property
    def context(self) -> Any:
        return self.env.context

    async def items(self, kind: str) -> list[Json]:
        """The bodies of the outbox items of `kind`, in order."""
        rows = await self.env.rows("SELECT origin_seq, item_kind, body_ct FROM runner.outbox ORDER BY origin_seq")
        key = self.env.sync.storage_key
        return [open_item(key, seq, found, bytes(blob)) for seq, found, blob in rows if found == kind]


@asynccontextmanager
async def issuer_env(
    dsn: str, started: ModelDouble, tmp: Path, *, management: str | None = MANAGEMENT
) -> AsyncIterator[Issuing]:
    """The platform device `rdev` as an issuer against the double (`management` None: no key file configured)."""
    async with Rig(dsn).env() as env:
        config = env.context.config
        config.openrouter_management_key_file = (
            None if management is None else write_secret(tmp / "management.key", management)
        )
        config.openrouter_key_file = write_secret(tmp / "platform.key", PLATFORM)
        config.model_upstream_openrouter = started.url
        config.dfx_key_max_headroom_micro_usd = 5_000_000
        yield Issuing(env, started)


def request_entry(request_id: str = REQUEST, *, device: str = ODEV, kem: str | None = None, **extra: Any) -> Json:
    return {
        "request_id": request_id,
        "org_id": ORG,
        "device_id": pid(device),
        "device_kem": kem if kem is not None else device_keys(device).kem_public,
        **extra,
    }


def key_entry(key_ref: str, *, headroom: Any = 1.0, disabled: bool = False, revoke: bool = False) -> Json:
    return {
        "key_ref": key_ref,
        "org_id": ORG,
        "device_id": pid(ODEV),
        "headroom_usd": headroom,
        "disabled": disabled,
        "revoke": revoke,
    }


def issuer_state(requests: list[Json] | None = None, keys: list[Json] | None = None) -> Json:
    return {**FALLBACK, "dfx_keys": {"requests": requests or [], "keys": keys or []}}


def held_entry(issued: Json) -> Json:
    """The org device's `dfx_key_held` entry for a `dfx_key_issued` body (what the hub delivers)."""
    return {
        "org_id": issued["org_id"],
        "key_ref": issued["key_ref"],
        "envelope": issued["envelope"],
        "issuer_device_id": ISSUER,
    }


async def store_text(pool: Any) -> str:
    """Every row of every table of the `runner` schema as text (the attack scans look for key material in it)."""
    async with pool.connection() as conn:
        cursor = await conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'runner' ORDER BY table_name"
        )
        tables = [str(row[0]) for row in await cursor.fetchall()]
        out: list[str] = []
        for table in tables:
            cursor = await conn.execute(sql.SQL("SELECT t::text FROM runner.{} AS t").format(sql.Identifier(table)))
            out.extend(str(row[0]) for row in await cursor.fetchall())
    return "\n".join(out)
