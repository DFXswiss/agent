"""U-RUN-dfxkeys: what a compromised or confused hub cannot make the issuer do (ADR-081 section 7) - create keys
without room, a second live key for a device, more than `DFX_KEY_MAX_PER_ORG` live keys of an organization, or touch
a key that is not named `dfx-...` (the platform key, say); and the sweep of `dfx-...` keys nobody lists."""

from __future__ import annotations

import asyncio
from pathlib import Path

from agent_runner.dfxkeys.issuer import SWEEP_AFTER_S, Issuer, issuer_for

from .kit import (
    FALLBACK,
    NOW,
    OTHER_ORG,
    REQUEST,
    REQUEST_TWO,
    Issuing,
    double,
    issuer_env,
    issuer_state,
    key_entry,
    request_entry,
)
from .test_issuer import faulty, key_calls

REQUEST_THREE = "01930050-0000-7000-8000-000000000003"


def issuer(issuing: Issuing) -> Issuer:
    found = issuer_for(issuing.context)
    assert found is not None
    return found


async def issued_refs(issuing: Issuing) -> list[str]:
    return [item["key_ref"] for item in await issuing.items("dfx_key_issued")]


def test_no_key_without_room(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                found = issuer(issuing)
                without = {key: value for key, value in request_entry().items() if key != "headroom_usd"}
                await found.observe(issuer_state([without, request_entry(REQUEST_TWO, headroom_usd=0)]))
                for at in (NOW, NOW + 5):
                    await found.tick(at)
                assert started.keys.documents() == []
                log = issuing.env.log()
                assert log.count(f"request_refused request_id={REQUEST} reason=no_headroom") == 1
                assert log.count(f"request_refused request_id={REQUEST_TWO} reason=no_headroom") == 1
                # the hub grants room: the key is created with it
                await found.observe(issuer_state([request_entry(headroom_usd=2.0)]))
                await found.tick(NOW + 10)
                assert [doc["limit"] for doc in started.keys.documents()] == [2.0]

    asyncio.run(main())


def test_one_live_key_per_device_until_the_hub_revokes_it(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                found = issuer(issuing)
                # two requests for the same device in one state: only the first gets a key
                await found.observe(issuer_state([request_entry(), request_entry(REQUEST_TWO)]))
                await found.tick(NOW)
                [first] = await issued_refs(issuing)
                await found.tick(NOW + 5)
                assert len(started.keys.documents()) == 1
                log = issuing.env.log()
                assert log.count(f"request_refused request_id={REQUEST_TWO} reason=device_has_key") == 1
                # the hub lists the first key with `revoke`: the new request may be answered
                await found.observe(issuer_state([request_entry(REQUEST_TWO)], [key_entry(first, revoke=True)]))
                await found.tick(NOW + 10)
                refs = await issued_refs(issuing)
                assert len(refs) == 2
                assert started.keys.get(first) is None  # revoked in the same round
                assert [row[0] for row in await issuing.env.rows("SELECT key_ref FROM runner.dfx_key_issued")] == [
                    refs[1]
                ]

    asyncio.run(main())


def test_live_keys_per_organization_are_bounded(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                issuing.context.config.dfx_key_max_per_org = 2
                found = issuer(issuing)
                requests = [
                    request_entry(REQUEST, device="odev"),
                    request_entry(REQUEST_TWO, device="odev2"),
                    request_entry(REQUEST_THREE, device="odev3"),
                    request_entry("01930050-0000-7000-8000-000000000004", device="odev4", org=OTHER_ORG),
                ]
                await found.observe(issuer_state(requests))
                await found.tick(NOW)
                await found.tick(NOW + 5)
                assert len(await issued_refs(issuing)) == 3  # two of the org, one of the other org
                log = issuing.env.log()
                assert log.count(f"request_refused request_id={REQUEST_THREE} reason=org_limit") == 1

    asyncio.run(main())


def test_a_key_not_named_for_the_issuer_is_never_touched(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                platform, _ = started.keys.create({"name": "platform", "limit": None})
                found = issuer(issuing)
                await found.observe(issuer_state([], [key_entry(platform.hash, disabled=True, revoke=True)]))
                await found.tick(NOW)
                await found.observe(issuer_state([], [key_entry(platform.hash, revoke=True)]))
                await found.tick(NOW + 61)
                document = started.keys.get(platform.hash)
                assert document is not None
                assert (document.disabled, document.limit) == (False, None)
                assert await issuing.items("dfx_key_usage") == []
                assert key_calls(started, platform.hash) == 1  # read once for its name, then left alone
                assert issuing.env.log().count(f"key_refused key_ref={platform.hash} reason=foreign") == 1

    asyncio.run(main())


def test_the_sweep_deletes_dfx_keys_nobody_lists(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                stray, _ = started.keys.create({"name": "dfx-left-by-another-issuer", "limit": 1})
                listed, _ = started.keys.create({"name": "dfx-listed", "limit": 1})
                other, _ = started.keys.create({"name": "platform", "limit": None})
                found = issuer(issuing)
                # not elected (no `dfx_keys` in the state): no sweep at all
                await found.observe(FALLBACK)
                await found.tick(NOW)
                await found.tick(NOW + SWEEP_AFTER_S + 100)
                assert len(started.keys.documents()) == 3
                # elected: a key nobody lists is deleted after 10 min, a requested or listed one never
                state = issuer_state([request_entry()], [key_entry(listed.hash)])
                await found.observe(state)
                start = NOW + 1000
                await found.tick(start)
                [issued] = await issued_refs(issuing)
                await found.tick(start + SWEEP_AFTER_S - 60)
                assert started.keys.get(stray.hash) is not None
                await found.tick(start + SWEEP_AFTER_S)
                assert started.keys.get(stray.hash) is None
                assert started.keys.get(listed.hash) is not None
                assert started.keys.get(other.hash) is not None
                assert started.keys.get(issued) is not None
                # the issued key that the hub then forgets (neither requested nor listed) goes too, with its row
                await found.observe(issuer_state([], [key_entry(listed.hash)]))
                await found.tick(start + SWEEP_AFTER_S + 60)
                await found.tick(start + 2 * SWEEP_AFTER_S + 120)
                assert started.keys.get(issued) is None
                assert await issuing.env.rows("SELECT key_ref FROM runner.dfx_key_issued") == []
                log = issuing.env.log()
                assert log.count(f"orphan_swept key_ref={stray.hash}") == 1
                assert log.count(f"orphan_swept key_ref={issued}") == 1

    asyncio.run(main())


def test_sweep_failures_are_logged_once(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                stray, _ = started.keys.create({"name": "dfx-stray", "limit": 1})
                faults = faulty(issuing, {("GET", "/v1/keys"): 503})
                found = issuer(issuing)
                await found.observe(issuer_state())
                for minute in range(3):
                    await found.tick(NOW + 60 * minute)
                faults.faults = {("DELETE", "/v1/keys/"): 500}
                for minute in range(3, 16):
                    await found.tick(NOW + 60 * minute)
                assert started.keys.get(stray.hash) is not None
                faults.faults = {}
                await found.tick(NOW + 60 * 16)
                assert started.keys.get(stray.hash) is None
                log = issuing.env.log()
                assert log.count("sweep_failed reason=status_503") == 1
                assert log.count(f"delete_failed key_ref={stray.hash} reason=status_500") == 1

    asyncio.run(main())


def test_the_logged_memory_is_bounded(store_dsn: str, tmp_path: Path) -> None:
    async def main() -> None:
        with double() as started:
            async with issuer_env(store_dsn, started, tmp_path) as issuing:
                found = issuer(issuing)
                found.logged = {("x", str(i)) for i in range(10_000)}
                found.once(("y",), "bounded")
                assert found.logged == {("y",)}
                assert "runner.dfxkeys bounded" in issuing.env.log()

    asyncio.run(main())
