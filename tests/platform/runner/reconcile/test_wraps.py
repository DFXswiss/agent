"""U-RUN-inbox / reconcile step 3: key wraps (W1-W3, unwrap, sealed storage, audit of refusals)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent_runner.crypto import wrap as wrap_format
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log import vectors as v
from agent_runner.crypto.log.entries import entry_hash
from agent_runner.orgs.book import OrgBook
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import load_scope_key
from agent_runner.reconcile import wraps
from agent_runner.reconcile.engine import Engine, Run
from agent_runner.reconcile.reconcile import STEPS

from .rig import CERT_SEQ, DEVICE, Env, Rig, device_keys, pid, scope_key, standard_world, wrap_for


async def once(env: Env) -> Run:
    built = getattr(env.context, "reconcile_engine", None)
    if built is None:
        built = Engine(env.context, STEPS)
        env.context.reconcile_engine = built
    run = await built.run_once()
    assert run is not None
    return run


async def held(env: Env) -> list[tuple[str, str, int, str]]:
    return [
        (str(a), str(b), int(c), str(d))
        for a, b, c, d in await env.rows(
            "SELECT scope, scope_id::text, epoch, source FROM runner.scope_key ORDER BY scope, scope_id, epoch"
        )
    ]


async def rejections(env: Env) -> list[tuple[str, Any]]:
    return [
        (a["target_id"], a["detail"]["code"]) for a in await env.audits() if a["action"] == "runner.inbound_rejected"
    ]


def test_wraps_are_verified_unwrapped_and_stored_sealed(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        async with rig.env(world) as env:
            env.sync.pusher.wake.clear()
            run = await once(env)
            assert run.halted is None
            assert env.sync.pusher.wake.is_set()
            assert await held(env) == [("org", spec.org, 1, "wrap"), ("wg", spec.default_wg, 1, "wrap")]
            async with session(env.pool) as conn:
                key = await load_scope_key(conn, env.sync.storage_key, "wg", spec.default_wg, 1)
            assert key == scope_key("wg", spec.default_wg, 1)
            env.sync.pusher.wake.clear()
            assert await wraps.sync_wraps(run) == 0
            assert not env.sync.pusher.wake.is_set()

    asyncio.run(body())


def forged_for_other_key(spec_wg: str) -> dict[str, Any]:
    """A wrap with a valid signature by alice whose ciphertext is sealed to another device's key."""
    sealed = wrap_format.wrap_key(
        v.identity("pdev").kem_public(), scope="wg", scope_id=spec_wg, epoch=2, recipient=pid(DEVICE), key=bytes(32)
    )
    signature = wrap_format.sign_wrap(
        v.identity("alice").sig, scope="wg", scope_id=spec_wg, epoch=2, recipient=pid(DEVICE), wrap=sealed
    )
    return {
        "scope": "wg",
        "scope_id": spec_wg,
        "epoch": 2,
        "recipient_id": pid(DEVICE),
        "wrap": sealed,
        "sig": signature,
        "wrapped_by": pid("alice"),
        "created_at": "2026-11-21T12:00:00.000Z",
    }


def test_refused_wraps_are_audited_once_and_never_stored(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        spec.log.add("rotate", "alice", scope="wg", scope_id=spec.default_wg, epoch=2)
        one = pid("a/wg/one")
        other_recipient = wrap_for("alice", "wg", spec.default_wg, 2, recipient="pdev")
        bad_signature = {**wrap_for("alice", "wg", spec.default_wg, 2), "wrapped_by": pid("bob")}
        not_entitled = wrap_for("alice", "wg", one, 1)  # the device is not assigned to wg/one
        unknown_wrapper = {**wrap_for("mallory", "wg", spec.default_wg, 2), "wrapped_by": pid("mallory")}
        malformed = {**wrap_for("alice", "wg", spec.default_wg, 2), "epoch": 0}
        world.wraps += [
            other_recipient,
            bad_signature,
            not_entitled,
            unknown_wrapper,
            forged_for_other_key(spec.default_wg),
        ]
        async with rig.env(world) as env:
            await once(env)
            # "not entitled" for this device is judged after the logs were refreshed, in the same run
            expected = [
                (f"wg:{spec.default_wg}:2", "wrap_unauthorized"),
                (f"wg:{spec.default_wg}:2", "signature_invalid"),
                (f"wg:{one}:1", "wrap_unauthorized"),
                (f"wg:{spec.default_wg}:2", "wrap_unauthorized"),
                (f"wg:{spec.default_wg}:2", "decrypt_failed"),
            ]
            assert await rejections(env) == expected
            await once(env)
            assert ("wg", spec.default_wg, 2, "wrap") not in await held(env)
            assert await rejections(env) == expected
            book: OrgBook = env.context.org_book
            kem = device_keys().kem_key()
            for item in (malformed, {**malformed, "epoch": True}, {**malformed, "scope": "x", "epoch": 1}, "text"):
                with pytest.raises(CryptoError, match="header_invalid"):
                    wraps.check(book, kem, item if isinstance(item, dict) else {})
            with pytest.raises(CryptoError, match="header_invalid"):
                wraps.check(book, kem, {**wrap_for("alice", "wg", spec.default_wg, 2), "wrapped_by": "nobody"})
            world.wraps = ["not a wrap"]  # type: ignore[list-item]
            env.hub.check = lambda *_args: None  # type: ignore[method-assign]
            await once(env)
            assert (await rejections(env))[-1] == ("::", "header_invalid")

    asyncio.run(body())


def test_a_wrap_ahead_of_its_log_entry_is_checked_again_when_the_log_advances(rig: Rig) -> None:
    """SCN-08 (E2E-26): a refusal of a wrap for this device holds only for the log state it was judged against: once
    the org's log advances, the wrap is checked again and stored (HOT-04: the refusal itself is judged against the
    refreshed logs and audited once)."""

    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        one = pid("a/wg/one")
        world.wraps.append(wrap_for("alice", "wg", one, 1))
        # refusals the logs cannot change stay audited once: another recipient, a ciphertext for another key
        spec.log.add("rotate", "alice", scope="wg", scope_id=spec.default_wg, epoch=2)
        world.wraps.append(wrap_for("alice", "wg", spec.default_wg, 2, recipient="pdev"))
        world.wraps.append(forged_for_other_key(spec.default_wg))
        async with rig.env(world) as env:
            await once(env)  # refused against the hub's current log: audited once
            await once(env)
            assert ("wg", one, 1, "wrap") not in await held(env)
            first = await rejections(env)
            assert (f"wg:{one}:1", "wrap_unauthorized") in first
            assert len(first) == 3
            spec.log.add("device_assign", "alice", device=pid(DEVICE), wg=one, platform_seq=CERT_SEQ)
            await once(env)
            assert ("wg", one, 1, "wrap") in await held(env)
            assert await rejections(env) == first

    asyncio.run(body())


def test_a_refused_wrap_is_audited_once_while_the_logs_advance(rig: Rig) -> None:
    """A hub releasing log entries one by one cannot keep a refusal out of the audit, nor repeat it: once per wrap."""

    async def body() -> None:
        world = standard_world("a")
        spec = next(iter(world.orgs.values()))
        one = pid("a/wg/one")
        world.wraps.append(wrap_for("alice", "wg", one, 1))
        async with rig.env(world) as env:
            await once(env)
            assert await rejections(env) == [(f"wg:{one}:1", "wrap_unauthorized")]
            spec.log.add("rotate", "alice", scope="wg", scope_id=spec.default_wg, epoch=2)
            await once(env)  # another log state, still not entitled: checked again, not audited again
            assert await rejections(env) == [(f"wg:{one}:1", "wrap_unauthorized")]

    asyncio.run(body())


def test_wraps_of_orgs_not_acting_wait(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a", "b")
        a, b = world.orgs.values()
        world.wraps.append(wrap_for("alice", "wg", pid("nowhere/wg"), 1))
        async with rig.env(world) as env:
            await once(env)
            world.offered[a.org] = {"seq": 0, "hash": entry_hash(a.log.entries[0])}  # rollback: a stops
            a.log.add("rotate", "alice", scope="wg", scope_id=a.default_wg, epoch=2)
            world.wraps.append(wrap_for("alice", "wg", a.default_wg, 2))
            await once(env)
            slots = await held(env)
            assert ("wg", a.default_wg, 2, "wrap") not in slots
            assert len(slots) == 4
            assert [x[1] for x in await rejections(env)] == []
            assert b.org in env.context.org_book.acting_orgs()

    asyncio.run(body())


def test_credential_wraps(rig: Rig) -> None:
    async def body() -> None:
        world = standard_world("a")
        alice_ck = wrap_for("alice", "cred", pid("alice"), 1)
        carol_ck = wrap_for("carol", "cred", pid("carol"), 1)  # carol is in no org of the device
        bob_ck = wrap_for("bob", "cred", pid("bob"), 1)  # bob shares the default wg with the device
        bob_by_alice = {**wrap_for("alice", "cred", pid("bob"), 2), "wrapped_by": pid("alice")}  # not bob's holder
        world.wraps += [alice_ck, carol_ck, bob_ck, bob_by_alice]
        async with rig.env(world) as env:
            await once(env)
            slots = await held(env)
            assert ("cred", pid("alice"), 1, "wrap") in slots
            assert ("cred", pid("bob"), 1, "wrap") in slots
            refused = [
                (f"cred:{pid('carol')}:1", "wrap_unauthorized"),
                (f"cred:{pid('bob')}:2", "wrap_unauthorized"),
            ]
            assert await rejections(env) == refused  # judged against the refreshed logs of every acting org
            await once(env)
            assert await rejections(env) == refused
            fresh = OrgBook(device_id=pid(DEVICE), kem_public="", sig_public="", root="")
            with pytest.raises(wraps.Pending):
                wraps.check(fresh, device_keys().kem_key(), alice_ck)

    asyncio.run(body())
