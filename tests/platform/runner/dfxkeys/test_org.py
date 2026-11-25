"""U-RUN-dfxkey: the org device keeps its DFX key envelopes sealed in `runner.dfx_key` and opens them only for a
provisioning (ADR-081 section 7); `SessionEnv.platform_key` of the daemon (`model.dfxkey.platform_key`)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent_runner.crypto.log import vectors as v
from agent_runner.model import dfxkey
from agent_runner.model.dfxkey import HeldKeys, accept, org_key, platform_key, seal_envelope
from agent_runner.orgs.book import ATTRIBUTE as BOOK_ATTRIBUTE

from ..reconcile.rig import device_keys, pid
from .kit import (
    ISSUER,
    ODEV,
    ORG,
    OTHER_ORG,
    PLATFORM,
    REQUEST,
    odev_keys,
    org_book,
    org_context,
    platform_entries,
    store_text,
    write_secret,
)

SUB = "sk-or-v1-SUBKEYMARKER-0123456789abcdef0123456789abcdef"
SUB_TWO = "sk-or-v1-SUBKEYMARKER-second-0123456789abcdef"
KEY_REF = "c" * 64


def held(key: str = SUB, *, key_ref: str = KEY_REF, signer: str = "rdev", org_id: str = ORG) -> dict[str, str]:
    header = {
        "request_id": REQUEST,
        "org_id": org_id,
        "device_id": pid(ODEV),
        "device_kem": odev_keys().kem_public,
        "key_ref": key_ref,
        "issued_at": "2026-11-24T12:00:00.000Z",
        "issuer_device_id": ISSUER,
    }
    envelope = seal_envelope(header, key, device_keys(signer).signing_key())
    return {"org_id": org_id, "key_ref": key_ref, "envelope": envelope, "issuer_device_id": ISSUER}


async def rows(context: Any) -> list[tuple[Any, ...]]:
    async with context.pool.connection() as conn:
        cursor = await conn.execute("SELECT org_id::text, key_ref, issuer_device_id::text FROM runner.dfx_key")
        return list(await cursor.fetchall())


def test_an_accepted_envelope_is_stored_sealed_and_opened_per_provisioning(org_dsn: str) -> None:
    async def main() -> None:
        async with org_context(org_dsn) as context:
            assert org_key(context, ORG) is None  # nothing held before the first state
            entry = held()
            await accept(context, {"dfx_key_held": [entry]})
            assert await rows(context) == [(ORG, KEY_REF, ISSUER)]
            assert org_key(context, ORG) == SUB
            assert dfxkey.has_key(context, ORG)
            assert not dfxkey.has_key(context, OTHER_ORG)
            assert platform_key(context, ORG) == SUB
            assert platform_key(context) is None  # an org device without the session's org has no key
            assert platform_key(context, OTHER_ORG) is None
            log = context.log.getvalue()
            assert f"runner.dfxkey accepted org_id={ORG} key_ref={KEY_REF}" in log
            # the same state again changes nothing
            await accept(context, {"dfx_key_held": [entry]})
            assert context.log.getvalue() == log
            # a restarted daemon reads the envelope back from the store
            setattr(context, dfxkey.ATTRIBUTE, HeldKeys())
            assert org_key(context, ORG) is None
            await accept(context, {"dfx_key_held": [entry]})
            assert org_key(context, ORG) == SUB
            assert context.log.getvalue() == log
            # a new key replaces the old one
            await accept(context, {"dfx_key_held": [held(SUB_TWO, key_ref="d" * 64)]})
            assert await rows(context) == [(ORG, "d" * 64, ISSUER)]
            assert org_key(context, ORG) == SUB_TWO
            # the plaintext is never stored or logged
            stored = await store_text(context.pool)
            assert "SUBKEYMARKER" not in stored
            assert "SUBKEYMARKER" not in context.log.getvalue()
            # no longer listed: dropped
            await accept(context, {"dfx_key_held": []})
            assert await rows(context) == []
            assert org_key(context, ORG) is None
            assert f"runner.dfxkey dropped org_id={ORG}" in context.log.getvalue()

    asyncio.run(main())


def test_a_forged_envelope_never_replaces_a_held_one_and_is_logged_once(org_dsn: str) -> None:
    async def main() -> None:
        async with org_context(org_dsn) as context:
            await accept(context, {"dfx_key_held": [held()]})
            forged = held(SUB_TWO, key_ref="e" * 64, signer="hub")
            for _ in range(2):
                await accept(context, {"dfx_key_held": [forged]})
            assert await rows(context) == [(ORG, KEY_REF, ISSUER)]
            assert org_key(context, ORG) == SUB
            assert context.log.getvalue().count(f"runner.dfxkey refused org_id={ORG} reason=signature") == 1
            assert forged["envelope"] not in context.log.getvalue()

    asyncio.run(main())


def test_malformed_listings_are_ignored_and_an_older_hub_holds_nothing(org_dsn: str) -> None:
    async def main() -> None:
        async with org_context(org_dsn) as context:
            entry = held()
            malformed: list[Any] = [
                "x",
                {**entry, "extra": 1},
                {**entry, "key_ref": 5},
                {**entry, "org_id": "not-a-uuid"},
            ]
            await accept(context, {"dfx_key_held": [*malformed, entry]})
            assert await rows(context) == [(ORG, KEY_REF, ISSUER)]
            await accept(context, {"dfx_key_held": "nonsense"})
            assert await rows(context) == []
            await accept(context, {"dfx_key_held": [entry]})
            await accept(context, {})  # a hub without the field
            assert await rows(context) == []

    asyncio.run(main())


def test_a_held_key_is_unusable_once_its_issuer_is_decertified(org_dsn: str) -> None:
    async def main() -> None:
        async with org_context(org_dsn) as context:
            await accept(context, {"dfx_key_held": [held()]})
            log = v.PlatformBuilder(platform_entries())
            log.add("device_decertify", device=ISSUER)
            setattr(context, BOOK_ATTRIBUTE, org_book(platform=log.entries))
            assert org_key(context, ORG) is None
            assert f"runner.dfxkey unusable org_id={ORG} reason=issuer_not_platform" in context.log.getvalue()

    asyncio.run(main())


def test_a_platform_device_uses_its_platform_key_file(tmp_path: Path) -> None:
    key_file = write_secret(tmp_path / "platform.key", PLATFORM)
    context = SimpleNamespace(
        identity=SimpleNamespace(device=SimpleNamespace(kind="platform")),
        config=SimpleNamespace(openrouter_key_file=key_file),
    )
    assert platform_key(context) == PLATFORM
    assert platform_key(context, ORG) == PLATFORM
    context.config.openrouter_key_file = None
    assert platform_key(context, ORG) is None


def test_held_repr_hides_the_envelope() -> None:
    entry = held()
    found = dfxkey.Held(entry["org_id"], entry["key_ref"], entry["issuer_device_id"], entry["envelope"])
    assert entry["envelope"] not in repr(found)
    assert "<sealed>" in repr(found)
