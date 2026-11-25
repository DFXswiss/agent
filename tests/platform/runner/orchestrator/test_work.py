"""U-RUN-orchestrator: the reconcile work of the main orchestrator in a session (05-runner.md section 17.1; ADR-082
sections 6.2-6.3 and 9.6-9.8) - keys drawn in the VM and registered signed by the device, the user's signed handover
checked under the task owner's key from the verified log before it reaches the VM.

Attack cases (KOR-57): a handover signed by another member, by a key that is no member's (the hub's), with an altered
ciphertext, for another session, pairing or user, or of an owner the verified log does not know, never reaches the
guest.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agent_runner.crypto.encoding import b64u_encode
from agent_runner.crypto.log import vectors as v
from agent_runner.guestlink.errors import GuestRefused, GuestUnreachable
from agent_runner.orchestrator import hints, reconcile, store, work
from agent_runner.orchestrator.env import OrchestratorEnv, install, orchestrator_for
from agent_runner.reconcile.hints import wake
from agent_runner.sessions import records
from agent_runner.sync.http import HubRefusal, HubTransportError

from .kit import HUB_KEY, HUB_URL, ORG, OTHER, OWNER, ROOT_KEY, TASK, VM, WG, Kit, principal, run, session_id

Json = dict[str, Any]


async def ready(k: Kit, **session: Any) -> str:
    await k.add_vm()
    return await k.add_session(**session)


async def registered(k: Kit) -> tuple[str, str]:
    sid = await ready(k)
    assert await work.keys(k.env, sid) == "registered"
    return sid, k.guest.pairing_id


def view_target(sid: str) -> str:
    return f"/v1/device/sessions/{sid}/orchestrator"


# -- the work list -----------------------------------------------------------------------------------------------


def test_work_items_take_only_well_formed_items() -> None:
    sid, pairing = session_id(1), session_id(9)
    desired = {
        "orchestrators": [
            {"session_id": sid, "step": "keys", "pairing_id": None},
            {"session_id": sid, "step": "handover", "pairing_id": pairing},
            {"session_id": sid, "step": "handover", "pairing_id": None},
            {"session_id": sid, "step": "revoked", "pairing_id": pairing},
            {"session_id": sid, "step": "revoked", "pairing_id": None},
            {"session_id": sid, "step": "keys", "pairing_id": "x"},
            {"session_id": "nope", "step": "keys", "pairing_id": None},
            {"session_id": sid, "step": "other", "pairing_id": None},
            "junk",
        ]
    }
    assert work.work_items(desired) == [
        work.Item(sid, "keys", None),
        work.Item(sid, "handover", pairing),
        work.Item(sid, "revoked", pairing),
    ]
    assert work.work_items({}) == []
    assert work.work_items({"orchestrators": {"x": 1}}) == []


# -- keys --------------------------------------------------------------------------------------------------------


def test_keys_are_drawn_in_the_vm_and_registered_with_the_device_signature(store_dsn: str) -> None:
    async def body(k: Kit) -> None:
        sid, pairing = await registered(k)
        [(asked, request)] = k.guest.of("keys")
        assert asked == sid
        assert request == {
            "hub_url": HUB_URL,
            "hub_key": {"id": work.HUB_KEY_ID, "public_key": HUB_KEY},
            "platform_root_key": ROOT_KEY,
        }
        [body] = k.hub.registered()  # the hub double verified pop, fingerprint and device_sig
        assert body["session_id"] == sid
        assert body["pairing_id"] == pairing
        assert set(body) == {"session_id", "pairing_id", "pub", "pop", "fp", "device_sig"}
        assert await k.rows("SELECT pairing_id::text, state FROM runner.orchestrator") == [(pairing, "registered")]
        assert f"registered session_id={sid}" in k.lines[-1]
        # new keys of the same session replace the pairing
        k.guest.pairing_id = session_id(8)
        assert await work.keys(k.env, sid) == "registered"
        assert await k.rows("SELECT pairing_id::text FROM runner.orchestrator") == [(session_id(8),)]

    run(store_dsn, body)


def test_the_guest_draws_fresh_keys_whenever_the_hub_lists_keys_again(store_dsn: str) -> None:
    """The hub lists `keys` only while no pairing of the session is live there: an expired registration or a handover
    that was not claimed in time gets new keys (review KOR-57 b no. 2); the guest refuses while it is claimed."""

    async def body(k: Kit) -> None:
        sid, pairing = await registered(k)
        k.hub.views[view_target(sid)] = k.signed_view(sid, pairing)
        assert await work.handover(k.env, sid, pairing) == "handed_over"
        assert await work.handover(k.env, sid, pairing) == "handed_over"
        assert len(k.guest.of("handover")) == 1
        k.guest.pairing_id = session_id(8)
        assert await work.keys(k.env, sid) == "registered"
        assert len(k.guest.of("keys")) == 2
        assert await k.rows("SELECT pairing_id::text, state FROM runner.orchestrator") == [
            (session_id(8), "registered")
        ]
        k.guest.fail["keys"] = GuestRefused("orchestrator", 409, "state_conflict")
        assert await work.keys(k.env, sid) == "keys_failed"

    run(store_dsn, body)


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("unknown", "unknown_session"),
        ("subagent", "not_main"),
        ("suspended", "not_running"),
        ("no_vm", "not_running"),
        ("vm_stopped", "not_running"),
        ("vm_gone", "not_running"),
        ("org_device", "not_platform"),
        ("not_acting", "org_not_acting"),
        ("team_org", "not_private"),
        ("owner_unknown", "owner_unknown"),
        ("owner_agent", "owner_unknown"),
        ("other_user", "not_sole_user"),
        ("removed_user", None),
        ("no_root_key", "root_key_missing"),
    ],
)
def test_keys_only_for_a_running_main_session_of_a_private_org_on_a_platform_device(
    store_dsn: str, change: str, reason: str
) -> None:
    async def body(k: Kit) -> None:
        sid = session_id(1)
        if change != "vm_gone":
            await k.add_vm(state="stopped" if change == "vm_stopped" else "running")
        if change != "unknown":
            await k.add_session(
                depth=1 if change == "subagent" else 0,
                state="suspended" if change == "suspended" else "idle",
                vm_id=None if change == "no_vm" else VM,
            )
        k.book.org_device = change == "org_device"
        k.book.acting = change != "not_acting"
        k.book.state.private = change != "team_org"
        if change == "owner_unknown":
            k.book.state.members = {OTHER: principal("bob")}
        if change == "owner_agent":
            k.book.state.members = {OWNER: principal("alice", kind="agent")}
        if change == "other_user":
            k.book.state.members[OTHER] = principal("bob")
        if change == "removed_user":  # a former user and the owner's agent leave the owner the only user
            k.book.state.members[OTHER] = replace(principal("bob"), removed_seq=3)
            k.book.state.members[session_id(5)] = principal("agent", kind="agent")
            assert await work.keys(k.env, sid) == "registered"
            return
        if change == "no_root_key":
            k.env.platform_root_key = None
        assert await work.keys(k.env, sid) == reason
        assert k.guest.calls == []
        assert k.hub.requests == []
        assert f"orchestrator_skipped session_id={sid} reason={reason}" in k.lines[-1]

    run(store_dsn, body)


def bad_fp(answer: Json) -> Json:
    return {**answer, "fp": b64u_encode(bytes(16))}


def bad_pop(answer: Json) -> Json:
    return {**answer, "pop": b64u_encode(bytes(64))}


def extra(answer: Json) -> Json:
    return {**answer, "priv": "x"}


def bad_pub(answer: Json) -> Json:
    return {**answer, "pub": {"kem": "x"}}


@pytest.mark.parametrize("change", [bad_fp, bad_pop, extra, bad_pub])
def test_keys_the_guest_answers_wrongly_are_not_registered(store_dsn: str, change: Any) -> None:
    async def body(k: Kit) -> None:
        sid = await ready(k)
        k.guest.keys_answer = change
        assert await work.keys(k.env, sid) == "keys_invalid"
        assert k.hub.requests == []
        assert await k.rows("SELECT 1 FROM runner.orchestrator") == []

    run(store_dsn, body)


@pytest.mark.parametrize(
    "failure",
    [
        ("guest", GuestUnreachable("orchestrator")),
        ("guest", GuestRefused("orchestrator", 409, "state_conflict")),
        ("hub", HubRefusal(409, "orchestrator_unavailable", {}, None)),
        ("hub", HubTransportError("hub_unreachable")),
    ],
)
def test_keys_failures_are_logged_and_store_nothing(store_dsn: str, failure: tuple[str, Exception]) -> None:
    async def body(k: Kit) -> None:
        sid = await ready(k)
        where, error = failure
        if where == "guest":
            k.guest.fail["keys"] = error
        else:
            k.hub.fail[work.REGISTER] = error
        assert await work.keys(k.env, sid) == "keys_failed"
        assert f"error={type(error).__name__}" in k.lines[-1]
        assert await k.rows("SELECT 1 FROM runner.orchestrator") == []

    run(store_dsn, body)


def test_a_registration_answer_for_another_pairing_is_refused(store_dsn: str) -> None:
    async def body(k: Kit) -> None:
        sid = await ready(k)
        k.hub.echo = session_id(7)
        assert await work.keys(k.env, sid) == "register_invalid"
        assert await k.rows("SELECT 1 FROM runner.orchestrator") == []

    run(store_dsn, body)


# -- handover ----------------------------------------------------------------------------------------------------


def test_a_handover_signed_by_the_task_owner_goes_into_the_vm_unopened(store_dsn: str) -> None:
    async def body(k: Kit) -> None:
        sid, pairing = await registered(k)
        view = k.signed_view(sid, pairing)
        k.hub.views[view_target(sid)] = view
        assert await work.handover(k.env, sid, pairing) == "handed_over"
        [(asked, request)] = k.guest.of("handover")
        assert asked == sid
        owner = principal("alice")
        assert request == {
            "pairing_id": pairing,
            "ct": view["pairing"]["handover"]["ct"],
            "user_id": OWNER,
            "user_pub": {"kem": owner.kem, "sig": owner.sig},
        }
        assert await k.rows("SELECT state, handed_over_at IS NOT NULL FROM runner.orchestrator") == [
            ("handed_over", True)
        ]

    run(store_dsn, body)


def altered(k: Kit, sid: str, pairing: str) -> Json:
    view = k.signed_view(sid, pairing)
    view["pairing"]["handover"]["ct"] = b64u_encode(bytes(range(1, 65)))
    return view


ATTACKS: dict[str, tuple[Any, str]] = {
    "another_member": (lambda k, sid, p: k.signed_view(sid, p, signer="bob"), "refused_signature_invalid"),
    "hub_key": (lambda k, sid, p: k.signed_view(sid, p, signer="hub"), "refused_signature_invalid"),
    "altered_ct": (altered, "refused_signature_invalid"),
    "bad_sig": (
        lambda k, sid, p: {
            **k.signed_view(sid, p),
            "pairing": {**k.signed_view(sid, p)["pairing"], "handover": {"ct": "e30", "sig": "x"}},
        },
        "refused_signature_invalid",
    ),
    "other_session": (lambda k, sid, p: k.signed_view(sid, p, session_id=session_id(5)), "refused_view_invalid"),
    "other_user": (lambda k, sid, p: k.signed_view(sid, p, user_id=OTHER), "refused_view_invalid"),
    "other_pairing": (lambda k, sid, p: k.signed_view(sid, session_id(6)), "refused_view_invalid"),
    "no_handover": (
        lambda k, sid, p: {**k.signed_view(sid, p), "pairing": {**k.signed_view(sid, p)["pairing"], "handover": None}},
        "refused_view_invalid",
    ),
    "no_pairing": (lambda k, sid, p: k.signed_view(sid, p, pairing=None), "refused_view_invalid"),
    "ct_not_text": (
        lambda k, sid, p: {
            **k.signed_view(sid, p),
            "pairing": {**k.signed_view(sid, p)["pairing"], "handover": {"ct": 1, "sig": "x"}},
        },
        "refused_view_invalid",
    ),
    "sig_not_text": (
        lambda k, sid, p: {
            **k.signed_view(sid, p),
            "pairing": {**k.signed_view(sid, p)["pairing"], "handover": {"ct": "e30", "sig": None}},
        },
        "refused_view_invalid",
    ),
}


@pytest.mark.parametrize("attack", sorted(ATTACKS))
def test_a_handover_not_signed_by_the_task_owner_is_refused(store_dsn: str, attack: str) -> None:
    async def body(k: Kit) -> None:
        sid, pairing = await registered(k)
        make, outcome = ATTACKS[attack]
        k.hub.views[view_target(sid)] = make(k, sid, pairing)
        assert await work.handover(k.env, sid, pairing) == outcome
        assert k.guest.of("handover") == []
        assert any(line.startswith("runner.orchestrator handover_refused") for line in k.lines)
        assert await k.rows("SELECT state FROM runner.orchestrator") == [("registered",)]

    run(store_dsn, body)


def test_a_handover_of_an_owner_the_log_does_not_know_never_reaches_the_guest(store_dsn: str) -> None:
    async def body(k: Kit) -> None:
        sid, pairing = await registered(k)
        k.hub.views[view_target(sid)] = k.signed_view(sid, pairing)
        k.book.state.members = {OTHER: principal("bob")}
        assert await work.handover(k.env, sid, pairing) == "owner_unknown"
        assert k.guest.of("handover") == []
        assert k.hub.requests[-1][0] == "POST"  # not even the view was fetched

    run(store_dsn, body)


def test_a_handover_for_a_pairing_this_device_did_not_register_is_refused(store_dsn: str) -> None:
    async def body(k: Kit) -> None:
        sid = await ready(k)
        assert await work.handover(k.env, sid, session_id(4)) == "refused_not_ours"
        assert await work.keys(k.env, sid) == "registered"
        assert await work.handover(k.env, sid, session_id(4)) == "refused_not_ours"
        assert k.guest.of("handover") == []
        assert all(method == "POST" for method, _t, _b in k.hub.requests)

    run(store_dsn, body)


@pytest.mark.parametrize("where", ["hub", "guest"])
def test_handover_failures_keep_the_pairing_registered(store_dsn: str, where: str) -> None:
    async def body(k: Kit) -> None:
        sid, pairing = await registered(k)
        k.hub.views[view_target(sid)] = k.signed_view(sid, pairing)
        if where == "hub":
            k.hub.fail[view_target(sid)] = HubTransportError("hub_unreachable")
        else:
            k.guest.fail["handover"] = GuestUnreachable("orchestrator")
        assert await work.handover(k.env, sid, pairing) == "handover_failed"
        assert await k.rows("SELECT state FROM runner.orchestrator") == [("registered",)]
        # the next run passes it
        assert await work.handover(k.env, sid, pairing) == "handed_over"

    run(store_dsn, body)


# -- revocation and the end of the session ----------------------------------------------------------------------


def test_a_revoked_pairing_ends_the_orchestrator_in_the_vm(store_dsn: str) -> None:
    """Review KOR-57 b no. 8: replaced or revoked while the session goes on - no relay any more, the guest ends it."""

    async def body(k: Kit) -> None:
        sid, pairing = await registered(k)
        item = work.Item(sid, "revoked", pairing)
        assert await work.run_item(k.env, work.Item(sid, "revoked", session_id(8))) == "not_ours"
        assert await work.run_item(k.env, item) == "ended"
        assert k.guest.of("end") == [(sid, pairing)]
        assert await k.rows("SELECT state FROM runner.orchestrator") == [("ended",)]
        assert await work.run_item(k.env, item) == "not_ours"  # once
        async with k.env.sessions.transaction() as conn:
            await store.register(conn, sid, pairing, k.env.now())
        k.guest.fail["end"] = GuestUnreachable("orchestrator")
        assert await work.run_item(k.env, item) == "end_failed"
        assert await k.rows("SELECT state FROM runner.orchestrator") == [("ended",)]
        async with k.env.sessions.transaction() as conn:
            await store.register(conn, sid, pairing, k.env.now())
        async with k.env.db.connection() as conn:
            await conn.execute("UPDATE runner.vm SET state = 'stopped'")
        assert await work.run_item(k.env, item) == "ended"  # a VM that does not run is not asked
        assert len(k.guest.of("end")) == 2

    run(store_dsn, body)


def test_the_rows_of_ended_sessions_go_with_their_due_checkpoint(store_dsn: str) -> None:
    async def body(k: Kit) -> None:
        sid, pairing = await registered(k)
        other = await k.add_session(2)
        async with k.env.sessions.transaction() as conn:
            await store.register(conn, other, session_id(8), k.env.now())
            await store.register(conn, session_id(3), session_id(6), k.env.now())  # a session unknown here
            await records.update(conn, sid, state="stopped")
            await conn.execute(
                "INSERT INTO runner.context_session (session_id, task_id, org_id, wg_id, due_at)"
                " VALUES (%s, %s, %s, %s, now())",
                (sid, TASK, ORG, WG),
            )
        assert await work.sweep(k.env) == 2
        assert k.guest.of("end") == [(sid, pairing)]
        assert await k.rows("SELECT session_id::text FROM runner.orchestrator") == [(other,)]
        assert await k.rows("SELECT due_at FROM runner.context_session") == [(None,)]
        assert await work.sweep(k.env) == 0

    run(store_dsn, body)


# -- the step, the hint and the environment ---------------------------------------------------------------------


def test_the_reconcile_step_works_off_the_list(store_dsn: str) -> None:
    async def body(k: Kit) -> None:
        sid = await ready(k)
        context = SimpleNamespace()
        install(context, k.env)
        await reconcile.step(cast(Any, SimpleNamespace(desired=None, context=context)))
        await reconcile.step(cast(Any, SimpleNamespace(desired={"orchestrators": []}, context=None)))  # not enrolled
        assert k.guest.calls == []
        items = [{"session_id": sid, "step": "keys", "pairing_id": None}]
        await reconcile.step(cast(Any, SimpleNamespace(desired={"orchestrators": items}, context=context)))
        pairing = k.guest.pairing_id
        k.hub.views[view_target(sid)] = k.signed_view(sid, pairing)
        items = [{"session_id": sid, "step": "handover", "pairing_id": pairing}]
        await reconcile.step(cast(Any, SimpleNamespace(desired={"orchestrators": items}, context=context)))
        assert await k.rows("SELECT state FROM runner.orchestrator") == [("handed_over",)]
        assert [(s.order, s.name) for s in reconcile.STEPS] == [(55, "orchestrator")]

    run(store_dsn, body)


def test_the_hint_wakes_the_reconcile_run() -> None:
    assert {"session.orchestrator": wake} == hints.HANDLERS


def test_the_environment_of_a_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    assert orchestrator_for(None) is None
    service = SimpleNamespace(env=SimpleNamespace(wall=lambda: 1.0, log=lambda _t: None))
    sync = SimpleNamespace(client="hub-client")
    found: dict[str, Any] = {"service": None, "sync": sync}
    monkeypatch.setattr("agent_runner.sessions.service.service_for", lambda _c: found["service"])
    monkeypatch.setattr("agent_runner.sync.runtime.runtime", lambda _c: found["sync"])
    device = v.identity("rdev")
    context = SimpleNamespace(
        identity=SimpleNamespace(
            device=SimpleNamespace(hub_url="https://Hub.Example.org:443", hub_key=HUB_KEY),
            keys=SimpleNamespace(signing_key=lambda: device.sig),
        ),
        config=SimpleNamespace(platform_root_key=ROOT_KEY),
    )
    assert orchestrator_for(context) is None
    found["service"] = service
    found["sync"] = None
    assert orchestrator_for(context) is None
    found["sync"] = sync
    built = orchestrator_for(context)
    assert isinstance(built, OrchestratorEnv)
    assert (built.hub_url, built.hub_key, built.platform_root_key) == ("https://hub.example.org", HUB_KEY, ROOT_KEY)
    assert built.hub is cast(Any, "hub-client")
    assert orchestrator_for(context) is built
    assert built.now().timestamp() == 1.0
