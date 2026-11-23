"""U-RUN-inbox / orgs: the shared log vectors (`contracts/vectors/log.json`, `platform-log.json`) through the book.

Every negative `verify_log` / `verify_platform_log` vector is replayed through `OrgBook.extend_org` /
`extend_platform` (the path the runner uses), which must refuse it with exactly the vector's code. Two kinds of
vector do not apply to a platform device and are skipped with their reason: an empty log (the book is offered no
entries) and a founder key different from the genesis key (platform devices pin the genesis key on first use,
ADR-040).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from agent_runner.crypto.log import vectors as v
from agent_runner.orgs.book import OrgBook
from agent_runner.orgs.db import session

from ..reconcile.rig import Rig

VECTORS = Path(__file__).resolve().parents[4] / "contracts" / "vectors"
LOG = json.loads((VECTORS / "log.json").read_text())
PLATFORM = json.loads((VECTORS / "platform-log.json").read_text())


def cases(data: dict[str, Any], op: str) -> list[dict[str, Any]]:
    return [case for case in data["invalid"] if case["op"] == op]


def book_for_vectors(root: str) -> OrgBook:
    pub = v.pub("pdev")
    return OrgBook(device_id=v.pid("pdev"), kem_public=pub["kem"], sig_public=pub["sig"], root=root)


@pytest.mark.parametrize("case", cases(LOG, "verify_log"), ids=lambda case: case["name"])
def test_membership_log_vectors(rig: Rig, case: dict[str, Any]) -> None:
    context = v.Context(LOG["logs"])
    entries = context.entries(case["args"])
    if not entries:
        pytest.skip("empty log: the book is never offered an empty copy")
    genesis_key = entries[0].get("body", {}).get("pub", {}).get("sig") if isinstance(entries[0], dict) else None
    if genesis_key != case["args"]["founder"]:
        pytest.skip("founder pinned on first use by platform devices (ADR-040)")

    async def body() -> None:
        async with rig.env() as env, session(env.pool) as conn:
            ref = case["args"].get("platform")
            book = book_for_vectors(ref["root"] if ref else v.sig_pk("root"))
            if ref is not None:
                await book.extend_platform(conn, context.logs[ref["name"]][: ref["count"]])
            outcome = await book.extend_org(conn, entries[0]["body"]["org"], entries)
            assert outcome.error == case["error"]

    asyncio.run(body())


@pytest.mark.parametrize("case", cases(PLATFORM, "verify_platform_log"), ids=lambda case: case["name"])
def test_platform_log_vectors(rig: Rig, case: dict[str, Any]) -> None:
    context = v.Context(PLATFORM["logs"])
    entries = context.entries(case["args"])
    if not entries:
        pytest.skip("empty log: the book is never offered an empty copy")

    async def body() -> None:
        async with rig.env() as env, session(env.pool) as conn:
            outcome = await book_for_vectors(case["args"]["root"]).extend_platform(conn, entries)
            assert outcome.error == case["error"]

    asyncio.run(body())
