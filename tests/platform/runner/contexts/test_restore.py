"""U-RUN-contexts: restore of the original conversation into a new sandbox and the reported emergency path
(ADR-072 section 7; review KOR-33 nos. 3, 6, 10, 15-18, 20, 22)."""

from __future__ import annotations

import io
import json
from dataclasses import replace
from typing import Any

import pytest

from agent_runner.contexts import archive as archive_format
from agent_runner.contexts import restore as restore_module
from agent_runner.contexts import store
from agent_runner.contexts.checkpoint import Checkpointer
from agent_runner.contexts.restore import (
    FALLBACK,
    RESTORED,
    STALE_TEXT,
    Candidate,
    Ref,
    Restored,
    RestoreFailed,
    Restorer,
    choose,
    compatible,
    order_key,
    parse_ref,
    restored_line,
)
from agent_runner.crypto import context as crypto
from agent_runner.crypto.encoding import P, b64u_decode, b64u_encode, canonical
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.keys import ed25519_private, ed25519_sign
from agent_runner.guestlink.errors import GuestRefused
from agent_runner.prompts.resume import RESTART_LINE, listed, restarted
from agent_runner.reconcile import wire as wire_checks
from agent_runner.sessions.start import Starter
from agent_runner.sync.http import HubRefusal

from ..prompts.kit import CLAUDE_ID, add_prompt, pull, run
from ..reconcile.rig import DEVICE, Rig, device_keys, pid, scope_key
from ..sessions.kit import ORG, TASK, WG, desired, session_id, wire
from .kit import TURN, CKit, archive, ckit, context_json, transport_error
from .test_checkpoint import due_now, objects, observe

TS = "2026-11-21T12:00:00.000Z"


async def checkpointed(env: Any, data: bytes | None = None) -> tuple[CKit, str]:
    """A committed checkpoint of session 1; returns the kit and the object ID."""
    ck = await ckit(env)
    sid = await ck.pk.add_session(state="idle")
    ck.guest.archives[sid] = data if data is not None else archive()
    assert await Checkpointer(ck.contexts).take(await due_now(ck, sid)) == "taken"
    assert await Checkpointer(ck.contexts).upload_round() == 1
    [obj] = await objects(ck)
    return ck, obj.object_id


async def forget_cache(ck: CKit) -> None:
    """As on another device: nothing of the context area is local."""
    for obj in await objects(ck):
        ck.contexts.cache.remove(obj.object_id)
    async with ck.pk.env.pool.connection() as conn:
        await conn.execute("DELETE FROM runner.context_object")


def name_context(ck: CKit, n: int, object_id: str, **entry: Any) -> None:
    item = {
        **wire(desired(n)),
        "context": {"object_id": object_id, "session_id": session_id(1), "created_at": TS, "size": 1},
        **entry,
    }
    ck.pk.kit.session_env.desired = {"sessions": [item]}


async def start(ck: CKit, n: int = 2) -> dict[str, Any]:
    report = await Starter(ck.pk.kit.session_env).start(desired(n))
    assert report.started, report
    return next(
        value for name, value in ck.guest.calls if name == "create_session" and value["session_id"] == session_id(n)
    )


def sign(manifest: dict[str, Any], key: Any = None) -> str:
    signer = key or device_keys().signing_key()
    return b64u_encode(ed25519_sign(signer, P + crypto.CONTEXT + canonical(manifest)))


def forge(
    ck: CKit,
    *,
    data: bytes | None = None,
    pieces: list[bytes] | None = None,
    epoch: int = 1,
    signer: Any = None,
    object_id: str | None = None,
    **overrides: Any,
) -> str:
    """A committed checkpoint at the hub built here (any content, validly signed unless `signer` says otherwise)."""
    object_id = object_id or uuid7()
    salt = bytes(range(32))
    key = crypto.context_key(scope_key("wg", WG, epoch), salt, object_id)
    if pieces is None:
        made: list[bytes] = []
        archive_format.compress_chunks(
            io.BytesIO(data if data is not None else archive()), lambda _i, piece: made.append(piece)
        )
        pieces = made
    sealed = [crypto.seal_chunk(key, object_id, index, piece) for index, piece in enumerate(pieces)]
    manifest: dict[str, Any] = {
        "v": 1,
        "object": object_id,
        "org": ORG,
        "wg": WG,
        "task": TASK,
        "session": session_id(1),
        "author": pid(DEVICE),
        "epoch": epoch,
        "salt": b64u_encode(salt),
        "chunks": len(sealed),
        "size": sum(len(chunk) for chunk in sealed),
        "digest": crypto.digest(sealed),
        "ts": TS,
    }
    manifest.update(overrides)
    for index, chunk in enumerate(sealed):
        ck.store.chunks[(object_id, index)] = b64u_encode(chunk)
    ck.store.committed[object_id] = {
        "manifest": manifest,
        "sig": sign(manifest, signer),
        "created_at": TS,
        "completed_at": TS,
    }
    return object_id


async def fallback_reasons(ck: CKit) -> list[str]:
    return [str(item["detail"]["reason"]) for item in await ck.audits(FALLBACK)]


def test_a_new_session_continues_the_conversation_from_the_hub_checkpoint_on_another_device(rig: Rig) -> None:
    async def body(env: Any) -> None:
        data = archive()
        ck, object_id = await checkpointed(env, data)
        await forget_cache(ck)
        name_context(ck, 2, object_id)
        request = await start(ck)
        note = request["restore"].pop("note")
        assert request["restore"] == {"claude_session_id": CLAUDE_ID, "object_id": object_id}
        assert "resume_claude_session_id" not in request
        assert note.startswith("This is your own earlier conversation of this task")
        assert "in a new sandbox on the same VM;" in note  # review no. 22: not "a new VM" on the same VM
        assert "your workspace was restored as it was" in note
        assert STALE_TEXT not in note
        # the harness file never reaches a resumed model: the sentence goes to the guest's notice, not the harness
        markdown = request["harness"]["context_markdown"]
        assert note not in markdown
        assert RESTART_LINE[:30] not in markdown
        # the guest gets the verified archive exactly as it was exported
        assert ck.guest.staged[session_id(2)] == data
        names = ck.guest.names()
        assert names.index("stage_context") < names.index("create_session")
        # review no. 16: the audit waits until the guest reports that the restored agent starts
        assert await ck.audits(RESTORED) == []
        await observe(ck, session_id(2), state="agent_starting", at=TS)
        await observe(ck, session_id(2), state="agent_starting", at=TS)  # once only
        detail = {"object_id": object_id, "workspace": "full", "stale": False}
        assert [item["detail"] for item in await ck.audits(RESTORED)] == [detail]
        [cached] = await objects(ck)
        assert (cached.object_id, cached.source, cached.state) == (object_id, "fetched", "committed")
        assert not list(ck.contexts.scratch.iterdir())
        # the next restore of the same checkpoint reads the cache only
        gets = len([t for m, t in ck.hub.targets if m == "GET"])
        name_context(ck, 3, object_id)
        assert (await start(ck, 3))["restore"]["object_id"] == object_id
        assert len([t for m, t in ck.hub.targets if m == "GET"]) == gets

    run(rig, body)


def test_the_local_cache_restores_without_the_hub_and_a_newer_one_wins(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, object_id = await checkpointed(env)
        ck.hub.errors = [transport_error()] * 10  # the hub is not reachable at all
        ck.pk.kit.session_env.desired = {"sessions": [wire(desired(2))]}
        assert (await start(ck))["restore"]["object_id"] == object_id
        ck.hub.errors = []
        # the hub names an older checkpoint of the same session (an older UUIDv7): the newer local one is restored
        older = forge(ck, object_id="01930000-0000-7000-8000-000000000001")
        name_context(ck, 3, older)
        assert (await start(ck, 3))["restore"]["object_id"] == object_id
        # a newer one of the same session at the hub wins over the cached one
        newer = forge(ck)
        name_context(ck, 4, newer)
        assert (await start(ck, 4))["restore"]["object_id"] == newer

    run(rig, body)


def _stored(object_id: str, session: str, *, ts: str = TS, created: Any = None, state: str = "committed") -> Any:
    return store.StoredObject(
        object_id=object_id,
        task_id=TASK,
        session_id=session,
        org_id=ORG,
        wg_id=WG,
        source="local",
        state=state,
        manifest={"ts": ts},
        sig="x",
        bytes=1,
        uploaded=1,
        attempts=0,
        created_at=None,  # type: ignore[arg-type]
        session_created_at=created,
    )


def test_choosing_and_parsing_the_checkpoint() -> None:
    """Review no. 10: the order of the hub - the session first (own, source, then by its creation), then UUIDv7."""
    early, late = parse_time_of("2026-11-20T00:00:00.000Z"), parse_time_of("2026-11-22T00:00:00.000Z")

    def key(object_id: str, session: str | None, time: Any, source: str | None = None) -> Any:
        return order_key(object_id=object_id, session_id=session, own="own", source=source, session_time=time)

    assert choose([]) is None
    a = Candidate("o1", key("o1", "s1", early), None)
    b = Candidate("o2", key("o2", "s2", late), None)
    assert choose([a, b]) is b  # the later session
    c = Candidate("o0", key("o0", "s1", early, source="s1"), None)
    assert choose([a, b, c]) is c  # the source session beats a later unrelated one
    d = Candidate("o9", key("o9", "own", early, source="s1"), None)
    assert choose([a, b, c, d]) is d  # the session's own checkpoint first
    newer = Candidate("o3", key("o3", "s1", early), None)
    assert choose([a, newer]) is newer  # within a session the newer object
    cached = Candidate("o1", key("o1", "s1", early), _stored("o1", "s1"))
    assert choose([a, cached]) is cached  # the cached copy of the same object
    assert choose([cached, a]) is cached
    assert parse_ref(None) is None
    assert parse_ref({"object_id": session_id(1), "created_at": TS}) == Ref(session_id(1), TS)
    full = {"object_id": session_id(1), "created_at": TS, "session_id": session_id(2), "size": 1}
    assert parse_ref(full) == Ref(session_id(1), TS, session_id(2))
    for bad in (
        "x",
        {"object_id": "x", "created_at": TS},
        {"object_id": session_id(1), "created_at": 5},
        {"object_id": session_id(1), "created_at": TS, "session_id": "x"},
    ):
        with pytest.raises(wire_checks.ResponseInvalid):
            parse_ref(bad)
    context = archive_format.parse_context(json.dumps(context_json()).encode())
    assert compatible(context, "9.9.9")
    newer_transcript = replace(context, max_version=(2, 1, 331))
    assert not compatible(newer_transcript, "2.1.330")
    assert compatible(newer_transcript, "2.1.331")
    assert compatible(newer_transcript, None)
    assert compatible(newer_transcript, "unknown")
    other = archive_format.parse_context(json.dumps(context_json(agent_kind="other")).encode())
    assert not compatible(other, None)
    restored = Restored(object_id="o", context=context, manifest={"ts": TS})
    assert "on a VM of another device" in restored_line(restored, where="other_device", stale=False)
    assert "on another VM of this device" in restored_line(restored, where="other_vm", stale=False)
    assert restored_line(restored, where="same_vm", stale=True).endswith(STALE_TEXT)
    partial = replace(context, workspace_omitted=3)
    line = restored_line(Restored("o", partial, {"ts": TS}), where="same_vm", stale=False)
    assert "3 entries of it could not be saved" in line
    one = replace(context, workspace="empty", workspace_omitted=1)
    assert "1 entry of it could not be saved" in restored_line(
        Restored("o", one, {"ts": TS}), where="same_vm", stale=False
    )
    empty = replace(context, workspace="empty")
    assert "your workspace was empty" in restored_line(Restored("o", empty, {"ts": TS}), where="same_vm", stale=False)
    big = replace(context, workspace="omitted_too_large", workspace_omitted=4)
    assert "too large" in restored_line(Restored("o", big, {"ts": TS}), where="same_vm", stale=False)


def parse_time_of(text: str) -> Any:
    from agent_runner.crypto.encoding import parse_time  # noqa: PLC0415

    return parse_time(text)


def _corrupt(ck: CKit, object_id: str) -> str:
    raw = bytearray(b64u_decode(ck.store.chunks[(object_id, 0)]))
    raw[20] ^= 1  # AEAD fails before digest and size are compared
    ck.store.chunks[(object_id, 0)] = b64u_encode(bytes(raw))
    return object_id


def _drop_chunk(ck: CKit, object_id: str) -> str:
    del ck.store.chunks[(object_id, 0)]
    return object_id


def _bad_ct(ck: CKit, object_id: str) -> str:
    ck.store.chunks[(object_id, 0)] = "!!"
    return object_id


def _truncated(ck: CKit, _object_id: str) -> str:
    pieces: list[bytes] = []
    archive_format.compress_chunks(io.BytesIO(archive()), lambda _i, piece: pieces.append(piece))
    return forge(ck, pieces=[pieces[0][:-4]])


CASES: dict[str, tuple[Any, str]] = {
    "corrupt_chunk": (_corrupt, "context_invalid"),
    "missing_chunk": (_drop_chunk, "context_unavailable"),
    "bad_signature": (lambda ck, _o: forge(ck, signer=ed25519_private(bytes(32))), "context_invalid"),
    "user_author": (lambda ck, _o: forge(ck, author=pid("alice")), "context_invalid"),
    "other_task": (lambda ck, _o: forge(ck, task=session_id(55)), "context_invalid"),
    "digest": (lambda ck, _o: forge(ck, digest=b64u_encode(bytes(32))), "context_invalid"),
    "size_big": (lambda ck, _o: forge(ck, size=1_000_000), "context_invalid"),
    "size_small": (lambda ck, _o: forge(ck, size=29), "context_invalid"),
    "bad_ct": (_bad_ct, "context_invalid"),
    "truncated": (_truncated, "context_invalid"),
    # KOR-33 E2E: a checkpoint without a complete turn made the restored start fail; now the emergency path
    "no_complete_turn": (lambda ck, _o: forge(ck, data=archive(transcript=b'{"type":"user"}\n')), "context_invalid"),
    "not_a_tar": (lambda ck, _o: forge(ck, data=b"plain text, not a tar archive" * 20), "context_invalid"),
    "archive_of_other_task": (lambda ck, _o: forge(ck, data=archive(task_id=session_id(56))), "context_invalid"),
    "archive_of_other_session": (
        lambda ck, _o: forge(ck, data=archive(session_id=session_id(57))),
        "context_invalid",
    ),
    "no_key": (lambda ck, _o: forge(ck, epoch=2), "context_unavailable"),
    "other_agent": (lambda ck, _o: forge(ck, data=archive(agent_kind="other-agent")), "version_incompatible"),
    "newer_transcript": (
        lambda ck, _o: forge(ck, data=archive(transcript=TURN + b'{"type":"user","version":"9.0.0"}\n')),
        "version_incompatible",
    ),
    "unknown_object": (lambda _ck, _o: session_id(58), "context_unavailable"),
}


@pytest.mark.parametrize("case", [*CASES, "malformed_entry"])
def test_an_unusable_checkpoint_takes_the_reported_emergency_path(rig: Rig, case: str) -> None:
    async def body(env: Any) -> None:
        ck = await ckit(env)
        object_id = forge(ck)
        if case == "malformed_entry":
            name_context(ck, 2, object_id, context={"object_id": "x"})
            reason = "context_invalid"
        else:
            make, reason = CASES[case]
            name_context(ck, 2, make(ck, object_id))
        request = await start(ck)
        assert "restore" not in request
        assert request["harness"]["context_markdown"].endswith(RESTART_LINE.format(task_id=TASK))
        assert await fallback_reasons(ck) == [reason]
        assert await objects(ck) == []  # nothing of a refused download stays
        assert not list(ck.contexts.scratch.iterdir())

    run(rig, body)


def test_an_interrupted_upload_is_never_restored(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit(env)
        sid = await ck.pk.add_session(state="idle")
        ck.guest.archives[sid] = archive()
        ck.store.refuse["commit"] = (503, "unavailable")
        assert await Checkpointer(ck.contexts).take(await due_now(ck, sid)) == "taken"
        [obj] = await objects(ck)
        assert obj.object_id not in ck.store.committed  # every chunk, but no commit
        await forget_cache(ck)  # another device
        name_context(ck, 2, obj.object_id)
        request = await start(ck)
        assert "restore" not in request
        assert await fallback_reasons(ck) == ["context_unavailable"]

    run(rig, body)


@pytest.mark.parametrize("failure", ["transport", "unavailable", "rate_limited"])
def test_a_transient_hub_failure_is_retried_with_backoff(rig: Rig, failure: str) -> None:
    """Review no. 3: a transport failure, `503 unavailable` or `429 rate_limited` is no reason to start fresh."""

    async def body(env: Any) -> None:
        ck = await ckit(env)
        object_id = forge(ck)
        error = transport_error() if failure == "transport" else HubRefusal(503, failure)
        ck.hub.errors = [error] * 5
        name_context(ck, 2, object_id)
        assert (await start(ck))["restore"]["object_id"] == object_id
        assert env.clock.sleeps[-5:] == [1.0, 2.0, 4.0, 8.0, 15.0]
        assert await fallback_reasons(ck) == []

    run(rig, body)


def test_retries_end_after_the_wait_and_not_found_ends_at_once(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit(env, wait_s=20.0)
        object_id = forge(ck)
        ck.hub.errors = [transport_error()] * 50
        name_context(ck, 2, object_id)
        await start(ck)
        assert await fallback_reasons(ck) == ["context_unavailable"]
        assert sum(env.clock.sleeps) <= 20.0
        assert env.clock.sleeps[-4:] == [1.0, 2.0, 4.0, 8.0]  # 15 more would pass the 20 s
        assert sum("fetch_failed" in line for line in ck.lines) == 5
        ck.hub.errors = []
        sleeps = len(env.clock.sleeps)
        name_context(ck, 3, session_id(70))  # unknown at the hub: 404
        await start(ck, 3)
        assert await fallback_reasons(ck) == ["context_unavailable", "context_unavailable"]
        assert len(env.clock.sleeps) == sleeps
        assert any("fetch_refused" in line and "code=not_found" in line for line in ck.lines)

    run(rig, body)


def test_a_refused_stage_and_a_failed_start_from_a_checkpoint_are_not_repeated(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit(env)
        object_id = forge(ck)
        name_context(ck, 2, object_id)
        ck.guest.fail["stage_context"] = GuestRefused("context_stage", 413, "size_exceeded")
        await start(ck)
        assert await fallback_reasons(ck) == ["restore_failed"]
        del ck.guest.fail["stage_context"]
        # a restored session whose agent does not start: that checkpoint is not used again on this device
        name_context(ck, 3, object_id)
        assert "restore" in await start(ck, 3)
        await observe(ck, session_id(3), state="failed", reason="agent_start_failed", at=TS)
        await observe(ck, session_id(3), state="failed", reason="oom", at=TS)  # no transition, no mark
        name_context(ck, 4, object_id)
        assert "restore" not in await start(ck, 4)
        assert await fallback_reasons(ck) == ["restore_failed", "restore_failed"]
        assert await ck.audits(RESTORED) == []  # the failed start never reported a restore

    run(rig, body)


def test_a_failure_after_the_start_window_does_not_mark_the_checkpoint(rig: Rig) -> None:
    """Review no. 15: only a failure inside the start window of a restored session marks its checkpoint."""

    async def body(env: Any) -> None:
        ck = await ckit(env)
        object_id = forge(ck)
        name_context(ck, 2, object_id)
        assert "restore" in await start(ck)
        await observe(ck, session_id(2), state="agent_starting", at=TS)
        await observe(ck, session_id(2), state="working", at=TS)
        await observe(ck, session_id(2), state="failed", reason="sandbox_failed", at=TS)
        name_context(ck, 3, object_id)
        assert (await start(ck, 3))["restore"]["object_id"] == object_id

    run(rig, body)


def test_a_refused_local_object_is_used_from_the_cache_and_kept_on_failure(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, object_id = await checkpointed(env)
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.context_object SET state = 'refused'")
        name_context(ck, 2, object_id)
        ck.guest.fail["stage_context"] = GuestRefused("context_stage", 409, "state_conflict")
        await start(ck)
        assert await fallback_reasons(ck) == ["restore_failed"]
        assert ck.contexts.cache.read_chunk(object_id, 0) is not None  # the refused local copy stays

    run(rig, body)


def test_a_cached_object_with_a_lost_chunk_reads_it_from_the_hub(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, object_id = await checkpointed(env)
        ck.contexts.cache.remove(object_id)
        ck.pk.kit.session_env.desired = {"sessions": [wire(desired(2))]}
        assert (await start(ck))["restore"]["object_id"] == object_id
        assert ("GET", f"/v1/device/contexts/{object_id}/chunks/0") in ck.hub.targets

    run(rig, body)


def test_without_a_checkpoint_only_a_continuing_session_takes_the_emergency_path(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit(env)
        first = add_prompt(env, 1, "first")
        follow = add_prompt(env, 2, "follow")
        await pull(env)
        senv = ck.pk.kit.session_env
        restorer = Restorer(ck.contexts)
        sid = await ck.pk.add_session(state="requested")
        session = await ck.pk.session(sid)

        def request(prompt: object = None) -> dict[str, Any]:
            found: dict[str, Any] = {"harness": {"context_markdown": "- Task: x\n"}}
            if prompt is not None:
                found["initial_prompt"] = prompt if not isinstance(prompt, str) else {"prompt_id": prompt, "text": "t"}
            return found

        # the task's first prompt, no prompt, an unknown prompt, a malformed one: a first session
        for prompt in (first, None, session_id(9), ["x"]):
            found = request(prompt)
            assert await restorer.amend(session, found) is None
            assert found["harness"]["context_markdown"] == "- Task: x\n"
        # a later prompt starts the session: it continues the task
        later = request(follow)
        assert await restorer.amend(session, later) == "context_missing"
        assert later["harness"]["context_markdown"] == "- Task: x\n" + RESTART_LINE.format(task_id=TASK)
        # the hub names the previous session (`resume_from`)
        ck.contexts.wait_s = 0.0  # session 7 is unknown here and never has a checkpoint
        senv.desired = {"sessions": [{**wire(desired(1)), "resume_from": session_id(7)}]}
        assert await restorer.amend(session, request()) == "context_missing"
        # never for a subagent session
        child = await ck.pk.session(await ck.pk.add_session(2, depth=1))
        assert await restorer.amend(child, request(follow)) is None
        assert not await restarted(senv, child, request(follow))
        assert listed(senv, session_id(3)) is None
        senv.desired = None
        assert listed(senv, sid) is None
        assert await fallback_reasons(ck) == ["context_missing", "context_missing"]

    run(rig, body)


def test_a_missing_epoch_key_and_the_place_of_a_restore(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit(env)
        object_id = forge(ck)
        name_context(ck, 2, object_id)
        session = await ck.pk.session(await ck.pk.add_session(2, state="requested"))
        async with env.pool.connection() as conn:
            await conn.execute("DELETE FROM runner.scope_key WHERE scope = 'wg'")
        found: dict[str, Any] = {"harness": {"context_markdown": "x"}}
        assert await Restorer(ck.contexts).amend(session, found) == "context_unavailable"
        context = archive_format.parse_context(json.dumps(context_json()).encode())
        restorer = Restorer(ck.contexts)
        elsewhere = Restored("o", context, {"author": pid("another-device"), "session": session_id(1)})
        assert await restorer._where(session, elsewhere) == "other_device"
        unknown = Restored("o", context, {"author": ck.contexts.device_id, "session": session_id(88)})
        assert await restorer._where(session, unknown) == "other_vm"
        assert restore_module._moment("not a time") == restore_module._EARLIEST

    run(rig, body)


def test_a_session_without_a_vm_cannot_be_staged(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck = await ckit(env)
        object_id = forge(ck)
        name_context(ck, 1, object_id)
        sid = await ck.pk.add_session(state="requested")
        async with env.pool.connection() as conn:
            await conn.execute("UPDATE runner.session SET vm_id = NULL")
        found: dict[str, Any] = {"harness": {"context_markdown": "x"}, "resume_claude_session_id": CLAUDE_ID}
        assert await Restorer(ck.contexts).amend(await ck.pk.session(sid), found) == "restore_failed"
        # an author that is no ID (a hub must not serve it; the check does not rely on that)
        with pytest.raises(RestoreFailed, match="context_invalid"):
            Restorer(ck.contexts)._author_key(await ck.pk.session(sid), 5, 1)

    run(rig, body)


def test_a_refused_stage_of_a_cached_checkpoint_keeps_the_cache(rig: Rig) -> None:
    async def body(env: Any) -> None:
        ck, object_id = await checkpointed(env)
        ck.pk.kit.session_env.desired = {"sessions": [wire(desired(2))]}
        ck.guest.fail["stage_context"] = GuestRefused("context_stage", 409, "state_conflict")
        await start(ck)
        assert await fallback_reasons(ck) == ["restore_failed"]
        assert ck.contexts.cache.read_chunk(object_id, 0) is not None

    run(rig, body)


def test_the_author_must_have_been_assigned_at_the_manifest_epoch() -> None:
    """Review no. 17: an earlier assignment does not authorise a checkpoint of a later epoch."""
    from types import SimpleNamespace  # noqa: PLC0415

    key = b64u_encode(bytes(range(32)))

    def state(epoch: int, assigned: bool) -> Any:
        device = SimpleNamespace(kind="device", sig=key)
        return SimpleNamespace(
            epoch=lambda _scope, _wg: epoch,
            principals={"dev": device},
            assigned=lambda author, wg: assigned and author == "dev" and wg == WG,
        )

    log = SimpleNamespace(states=[state(1, True), state(2, False)])
    book = SimpleNamespace(log=lambda _org: log, excluded=lambda _org: frozenset({"gone"}))
    restorer = Restorer(SimpleNamespace(sessions=SimpleNamespace(book=book)))  # type: ignore[arg-type]
    session = SimpleNamespace(org_id=ORG, wg_id=WG)
    assert restorer._author_key(session, "dev", 1) == bytes(range(32))  # type: ignore[arg-type]
    for author, epoch, reason in (
        ("dev", 2, "context_invalid"),
        ("dev", 3, "context_unavailable"),
        ("gone", 1, "context_invalid"),
    ):
        with pytest.raises(RestoreFailed, match=reason):
            restorer._author_key(session, author, epoch)  # type: ignore[arg-type]
