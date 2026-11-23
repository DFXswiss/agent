"""U-RUN-inbox / orgs: verified log copies and the rights library (`agent_runner.orgs.book`)."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log import vectors as v
from agent_runner.crypto.log.entries import Head, entry_hash, sign_log_entry
from agent_runner.orgs import book as book_module
from agent_runner.orgs.book import NOT_ASSIGNED, OrgBook, book_for, stop_key
from agent_runner.orgs.db import session

from ..reconcile.rig import CERT_SEQ, DEVICE, ROOT, Env, Rig, World, device_keys, org_log, pid, platform_log


def new_book(device: str = DEVICE, root: str = ROOT) -> OrgBook:
    keys = device_keys(device)
    return OrgBook(device_id=pid(device), kem_public=keys.kem_public, sig_public=keys.sig_public, root=root)


def run(rig: Rig, test: Callable[[Env, Any], Awaitable[None]]) -> None:
    async def body() -> None:
        async with rig.env(World()) as env, session(env.pool) as conn:
            await test(env, conn)

    asyncio.run(body())


def test_platform_copy_is_verified_stored_and_reloaded(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        book = new_book()
        await book.ensure_loaded(conn)
        assert (book.platform, book.status(), book.certification(), book.platform_head) == (
            None,
            "uncertified",
            None,
            None,
        )
        entries = platform_log().entries
        outcome = await book.extend_platform(conn, entries[:2])
        assert (outcome.added, outcome.error, book.status()) == (2, None, "uncertified")
        outcome = await book.extend_platform(conn, entries[2:])
        assert (outcome.added, book.certified, book.decertified) == (1, True, False)
        assert book.platform_head is not None
        assert book.platform_head.seq == 2
        again = new_book()
        await again.ensure_loaded(conn)
        await again.ensure_loaded(conn)
        assert again.certified
        assert again.platform_head == book.platform_head
        assert again.platform is not None
        assert len(again.platform.entries) == 3

    run(rig, test)


def test_certification_must_name_the_device_keys(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        log = v.PlatformBuilder(platform_log().entries[:2])
        log.certify(DEVICE, key_label="other")
        book = new_book()
        await book.extend_platform(conn, log.entries)
        assert (book.certification(), book.status()) == (None, "uncertified")

    run(rig, test)


def test_decertification_and_platform_stop(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        log = platform_log()
        log.add("device_decertify", device=pid(DEVICE))
        book = new_book()
        await book.extend_platform(conn, log.entries)
        assert (book.decertified, book.certified, book.status()) == (True, False, "decertified")
        healthy = new_book("pdev")
        await healthy.extend_platform(conn, [])
        async with conn.transaction():
            await healthy.stop(conn, None, "log_fork")
        assert healthy.status() == "stopped"
        reloaded = new_book()
        await reloaded.ensure_loaded(conn)
        assert reloaded.platform_stopped == "log_fork"
        assert not reloaded.certified

    run(rig, test)


def test_refused_platform_entry_keeps_the_verified_prefix_and_detects_a_fork(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        good = platform_log()
        book = new_book()
        bad = v.PlatformBuilder(good.entries[:2])
        bad.add(
            "user_invite",
            invite_id=v.secrets("x").invite_id,
            pk_inv=v.secrets("x").pk_inv,
            expires=v.EXPIRES,
            signer="mallory",
        )
        outcome = await book.extend_platform(conn, [good.entries[0], good.entries[1], bad.entries[2]])
        assert (outcome.added, outcome.error, outcome.alert) == (2, "log_signature", None)
        other = v.PlatformBuilder(good.entries[:2])
        other.add("user_invite", invite_id=v.secrets("y").invite_id, pk_inv=v.secrets("y").pk_inv, expires=v.EXPIRES)
        forked = v.PlatformBuilder(other.entries)
        forked.entries[2] = {**other.entries[2], "body": {**other.entries[2]["body"], "prev": "x" * 43}}
        outcome = await book.extend_platform(conn, [forked.entries[2]])
        assert (outcome.added, outcome.alert) == (0, "log_fork")
        outcome = await book.extend_platform(conn, ["not an entry"])
        assert (outcome.added, outcome.error, outcome.alert) == (0, "header_invalid", None)

    run(rig, test)


async def certified(conn: Any) -> OrgBook:
    book = new_book()
    await book.extend_platform(conn, platform_log().entries)
    return book


def test_org_copy_rights_and_scopes(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        book = await certified(conn)
        spec = org_log("a")
        org = spec.org
        outcome = await book.extend_org(conn, org, spec.log.entries)
        assert (outcome.added, outcome.error) == (5, None)
        assert book.acting_orgs() == [org]
        assert book.acting(org)
        assert book.member(org) is not None
        assert book.log(org) is not None
        assert book.assignments() == [(org, spec.default_wg)]
        assert book.pull_scopes() == {spec.default_wg: (org, "wg"), org: (org, "org")}
        assert book.org_of_wg(spec.default_wg) == org
        assert book.org_of_wg(pid("a/wg/one")) == org
        assert book.org_of_wg(pid("nowhere")) is None
        head = book.head(org)
        assert head is not None
        assert head.seq == 4
        assert book.author_may_write(org, spec.default_wg, pid("alice"), 4)
        assert book.author_may_write(org, spec.default_wg, pid("bob"), 4)  # default workgroup: every member writes
        assert not book.author_may_write(org, pid("a/wg/one"), pid("bob"), 4)  # read access only
        assert not book.author_may_write(org, spec.default_wg, pid("bob"), 2)  # not yet a member at lh 2
        assert not book.author_may_write(org, spec.default_wg, pid("alice"), 5)  # beyond the verified head
        assert book.assigned_at(org, spec.default_wg, pid(DEVICE), 4)
        assert not book.assigned_at(org, spec.default_wg, pid(DEVICE), 3)
        assert book.excluded(org) == frozenset()
        assert book.excluded(pid("unknown")) == frozenset()
        rows = await env.rows("SELECT seq, hash FROM runner.log_head WHERE org_id = %s", org)
        assert rows == [(4, head.hash)]
        again = new_book()
        await again.ensure_loaded(conn)
        assert again.acting_orgs() == [org]
        assert again.head(org) == head
        spec.log.add("wg_create", "alice", wg=pid("a/wg/two"), epoch=1)
        outcome = await again.extend_org(conn, org, spec.log.entries[5:])
        assert outcome.added == 1
        assert (await env.rows("SELECT seq FROM runner.log_head"))[0] == (5,)

    run(rig, test)


def test_a_new_org_must_assign_the_device(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        book = await certified(conn)
        spec = org_log("a", device=None)
        assert await book.extend_org(conn, spec.org, spec.log.entries) == type(
            await book.extend_org(conn, spec.org, [])
        )(0, NOT_ASSIGNED)
        foreign = org_log("b")
        outcome = await book.extend_org(conn, spec.org, foreign.log.entries)
        assert (outcome.added, outcome.error) == (0, "log_chain")
        broken = org_log("c")
        tampered = [*broken.log.entries[:4], {**broken.log.entries[4], "sig": broken.log.entries[3]["sig"]}]
        outcome = await book.extend_org(conn, broken.org, tampered)
        assert (outcome.added, outcome.error) == (0, "log_signature")
        assert book.logs == {}
        assert await env.rows("SELECT * FROM runner.log_entry") == []
        uncertified = new_book()
        assert (await uncertified.extend_org(conn, org_log("d").org, org_log("d").log.entries)).error == "log_rights"

    run(rig, test)


def test_org_membership_through_another_certification_is_not_acting(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        log = platform_log()
        log.add("device_decertify", device=pid(DEVICE))
        found = v.secrets("rdev-again")
        log.add("device_invite", invite_id=found.invite_id, pk_inv=found.pk_inv, expires=v.EXPIRES)
        log.certify(DEVICE, code_label="rdev-again")  # seq 5, same keys
        book = new_book()
        await book.extend_platform(conn, log.entries)
        spec = org_log("a")  # assigns the device through seq 2
        outcome = await book.extend_org(conn, spec.org, spec.log.entries)
        assert outcome.error == NOT_ASSIGNED
        assert book.certified
        assert book.acting_orgs() == []

    run(rig, test)


def test_refused_org_entries_keep_the_prefix_and_forks_are_reported(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        book = await certified(conn)
        spec = org_log("a")
        org = spec.org
        await book.extend_org(conn, org, spec.log.entries)
        good = spec.log.copy()
        good.add("wg_create", "alice", wg=pid("a/wg/two"), epoch=1)  # 5
        good.add("wg_create", "bob", wg=pid("a/wg/three"), epoch=1)  # 6: bob is no admin
        outcome = await book.extend_org(conn, org, good.entries[5:])
        assert (outcome.added, outcome.error, outcome.alert) == (1, "log_rights", None)
        assert book.head(org) == Head(5, entry_hash(good.entries[5]))
        other = spec.log.copy()
        other.add("wg_create", "alice", wg=pid("a/wg/four"), epoch=1)  # another seq 5
        other.add("wg_create", "alice", wg=pid("a/wg/five"), epoch=1)  # 6, continuing the other seq 5
        outcome = await book.extend_org(conn, org, other.entries[6:])
        assert (outcome.added, outcome.alert) == (0, "log_fork")

    run(rig, test)


def test_stopped_org_is_not_acting_and_stays_stopped(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        book = await certified(conn)
        spec = org_log("a")
        await book.extend_org(conn, spec.org, spec.log.entries)
        async with conn.transaction():
            await book.stop(conn, spec.org, "log_rollback")
        assert book.acting_orgs() == []
        assert book.log(spec.org) is None
        assert book.state_at(spec.org, 0) is None
        assert await env.rows("SELECT value FROM runner.sync_state WHERE key = %s", stop_key(spec.org)) == [
            ("log_rollback",)
        ]
        again = new_book()
        await again.ensure_loaded(conn)
        assert again.stopped == {spec.org: "log_rollback"}
        assert again.acting_orgs() == []

    run(rig, test)


def test_a_stored_copy_that_no_longer_verifies_fails_the_load(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        book = await certified(conn)
        spec = org_log("a")
        await book.extend_org(conn, spec.org, spec.log.entries)
        other_root = new_book(root=v.sig_pk("other-root"))
        with pytest.raises(CryptoError, match="platform_root_mismatch"):
            await other_root.ensure_loaded(conn)
        await conn.execute(
            "UPDATE runner.log_entry SET entry_text = replace(entry_text, '\"seq\":4', '\"seq\":5') WHERE seq = 4"
        )
        with pytest.raises(CryptoError, match="log_chain"):
            await new_book().ensure_loaded(conn)

    run(rig, test)


def test_decertified_devices_of_an_org_are_excluded(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        log = platform_log()
        found = v.secrets("pdev")
        log.add("device_invite", invite_id=found.invite_id, pk_inv=found.pk_inv, expires=v.EXPIRES)  # 3
        log.certify("pdev")  # 4
        log.add("device_decertify", device=pid("pdev"))  # 5
        book = new_book()
        await book.extend_platform(conn, log.entries)
        spec = org_log("a")
        spec.log.add("device_assign", "alice", device=pid("pdev"), wg=spec.default_wg, platform_seq=4)
        await book.extend_org(conn, spec.org, spec.log.entries)
        assert book.excluded(spec.org) == frozenset({pid("pdev")})
        assert not book.assigned_at(spec.org, spec.default_wg, pid("pdev"), 5)
        assert book.assigned_at(spec.org, spec.default_wg, pid(DEVICE), 5)

    run(rig, test)


def test_book_for_builds_one_book_per_context(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        book = book_for(env.context)
        assert book is book_for(env.context)
        assert (book.device_id, book.root) == (pid(DEVICE), ROOT)
        assert CERT_SEQ == 2

    run(rig, test)


def test_a_non_private_org_needs_platform_access(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        book = await certified(conn)
        spec = org_log("a", private=False)
        assert (await book.extend_org(conn, spec.org, spec.log.entries)).error == NOT_ASSIGNED
        log = platform_log()
        log.add("org_platform_access", org=spec.org, granted=True)
        await book.extend_platform(conn, log.entries[3:])
        assert (await book.extend_org(conn, spec.org, spec.log.entries)).error is None
        assert book.acting_orgs() == [spec.org]

    run(rig, test)


def test_an_org_may_not_claim_ids_of_another_verified_copy(rig: Rig) -> None:
    async def test(env: Env, conn: Any) -> None:
        book = await certified(conn)
        real = org_log("a")
        await book.extend_org(conn, real.org, real.log.entries)
        # a second org (invented by the hub) creating the real org's workgroup
        fake = org_log("f", founder="mallory")
        fake.log.add("wg_create", "mallory", wg=real.default_wg, epoch=1)
        outcome = await book.extend_org(conn, fake.org, fake.log.entries)
        assert outcome.error == "log_rights"
        assert book.org_of_wg(real.default_wg) == real.org
        assert book.pull_scopes()[real.default_wg] == (real.org, "wg")
        assert fake.org in book.logs  # the verified prefix (assigning the device) is kept, the clashing entry not
        assert book.logs[fake.org].head.seq == 4
        # a default workgroup that is the real org's ID
        twisted = v.LogBuilder(org=pid("t/org"))
        twisted.genesis("mallory", default_wg=real.org, private=True)
        assert (await book.extend_org(conn, twisted.org, twisted.entries)).error == "log_rights"
        # a principal ID of the real org with other keys (bob's ID, mallory's keys)
        imposter = org_log("i", founder="mallory", extra_wg=False)
        found = v.secrets("imp")
        imposter.log.add(
            "invite",
            "mallory",
            invite_id=found.invite_id,
            pk_inv=found.pk_inv,
            kind="user",
            role="member",
            wgs={},
            expires=v.EXPIRES,
        )
        body = imposter.log.body(
            "accept", "bob", invite_id=found.invite_id, subject=pid("bob"), kind="user", pub=v.pub("mallory")
        )
        imposter.log.entries.append(sign_log_entry(body, v.identity("mallory").sig, found.signing))
        outcome = await book.extend_org(conn, imposter.org, imposter.log.entries)
        assert outcome.added == 3
        assert outcome.error == "log_rights"
        # the same principals with the same keys in a second real org are fine
        second = org_log("b")
        assert (await book.extend_org(conn, second.org, second.log.entries)).error is None
        assert sorted(book.acting_orgs()) == sorted([real.org, fake.org, second.org, imposter.org])

    run(rig, test)


def test_refusal_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(book_module, "MAX_REFUSED", 2)
    book = new_book()
    assert book.remember_refusal(("a",))
    assert book.remember_refusal(("b",))
    assert not book.remember_refusal(("a",))
    assert book.remember_refusal(("c",))
    assert book.refused == {("c",)}
