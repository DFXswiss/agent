"""Copied from agent-core tests/platform/hub/log/test_admin_read.py at 56db2db59b0e.

U-HUB-rights (KOR-34, ADR-058): every admin of an org reads every workgroup by inheritance.

The inherited read is effective access (`wg_access`), entitles to the workgroup key (readers, W2, catch-up with every
epoch), is lost by a demotion (R, rotation in the same batch; self-demotion owes it like a leaver) and never widens the
credential scope (`explicit_access`)."""

from __future__ import annotations

from typing import Any, cast

import pytest

from agent_runner.crypto.encoding import parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log import catchup, verify
from agent_runner.crypto.log.batch import check_batch
from agent_runner.crypto.log.rotation import required_set
from agent_runner.crypto.log.state import WG
from agent_runner.crypto.log.vectors import EDGE_COUNT, W0, W1, W2, LogBuilder, edge_builder, make_wrap, pid, sig_pk, ts
from agent_runner.crypto.log.wraps import WrapSlot, cred_readers

ALICE = sig_pk("alice")
CAROL, BOB, DEV2 = pid("carol"), pid("bob"), pid("dev2")


@pytest.fixture
def edge() -> LogBuilder:
    """alice (founder, admin), bob (member: W1 and W2 read), carol (admin without explicit access), dev2 on W1."""
    return edge_builder()


def _log(builder: LogBuilder) -> verify.VerifiedLog:
    return verify.verify_log(builder.entries, ALICE)


def test_an_admin_reads_every_workgroup_without_writing(edge: LogBuilder) -> None:
    state = _log(edge).state
    for wg in (W1, W2):  # W2 is archived: still readable
        assert state.wg_access(CAROL, wg) == "read"
        assert state.inherited(CAROL, wg)
        assert state.explicit_access(CAROL, wg) is None
        assert state.can_read(CAROL, WG, wg)
        assert CAROL in state.readers(WG, wg)
        assert not state.can_write_wg(CAROL, wg)
    assert state.wg_access(CAROL, W0) == "write"
    assert not state.inherited(CAROL, W0)
    assert state.wg_access(CAROL, pid("wg/none")) is None
    assert not state.inherited(CAROL, pid("wg/none"))
    assert not state.inherited(BOB, W1)  # a member reads W1 explicitly
    assert not state.inherited(DEV2, W1)  # devices never inherit
    listed = cast(list[dict[str, Any]], state.projection()["workgroups"])
    workgroups = {wg["wg_id"]: wg for wg in listed}
    assert workgroups[W1]["inherited"] == [CAROL]
    assert workgroups[W1]["access"][CAROL] == "read"
    assert workgroups[W0]["inherited"] == []


def test_explicit_access_wins_and_survives_a_demotion(edge: LogBuilder) -> None:
    edge.add("grant", "alice", subject=CAROL, wg=W1, access="write")
    state = _log(edge).state
    assert state.wg_access(CAROL, W1) == "write"
    assert not state.inherited(CAROL, W1)
    edge.add("ungrant", "alice", subject=CAROL, wg=W1)  # back to the inherited read: nothing lost
    log = _log(edge)
    assert log.state.inherited(CAROL, W1)
    assert required_set(log.states[-2], log.state, CAROL) == frozenset()
    edge.add("grant", "alice", subject=CAROL, wg=W1, access="read")  # an explicit read survives the demotion
    edge.add("role", "alice", subject=CAROL, role="member")
    log = _log(edge)
    assert log.state.wg_access(CAROL, W1) == "read"
    assert not log.state.inherited(CAROL, W1)
    assert required_set(log.states[-2], log.state, CAROL) == frozenset({(WG, W2)})


def test_a_demotion_owes_the_inherited_workgroups(edge: LogBuilder) -> None:
    before = _log(edge).state
    edge.add("role", "alice", subject=CAROL, role="member")
    after = _log(edge).state
    assert required_set(before, after, CAROL) == frozenset({(WG, W1), (WG, W2)})
    assert after.pending == frozenset()  # the demoting admin rotates in the same batch
    assert after.wg_access(CAROL, W1) is None


def test_a_self_demotion_is_owed_by_the_others(edge: LogBuilder) -> None:
    edge.add("role", "carol", subject=CAROL, role="member")
    log = _log(edge)
    assert log.state.pending == frozenset({(WG, W1), (WG, W2)})
    # the batch needs no rotate (the subject could not rotate what it no longer reads)
    entries = edge.entries[EDGE_COUNT:]
    result = check_batch(
        verify.verify_log(edge.entries[:EDGE_COUNT], ALICE), entries, [], wrapped_by=CAROL, now=parse_time(ts(60))
    )
    assert result.required == frozenset()
    assert result.log.state.pending == frozenset({(WG, W1), (WG, W2)})


def test_a_demotion_by_another_admin_must_rotate(edge: LogBuilder) -> None:
    base = verify.verify_log(edge.entries, ALICE)
    edge.add("role", "alice", subject=CAROL, role="member")
    with pytest.raises(CryptoError, match="rotation_required"):
        check_batch(base, edge.entries[EDGE_COUNT:], [], wrapped_by=pid("alice"), now=parse_time(ts(60)))


def test_a_new_workgroup_is_wrapped_for_every_admin(edge: LogBuilder) -> None:
    base = verify.verify_log(edge.entries, ALICE)
    w3 = pid("wg/three")
    edge.add("wg_create", "alice", wg=w3, epoch=1)
    entries = edge.entries[EDGE_COUNT:]
    alone = [make_wrap("alice", "wg", w3, 1, "alice")]
    with pytest.raises(CryptoError, match="rotation_required"):
        check_batch(base, entries, alone, wrapped_by=pid("alice"), now=parse_time(ts(60)))
    both = [*alone, make_wrap("alice", "wg", w3, 1, "carol")]
    result = check_batch(base, entries, both, wrapped_by=pid("alice"), now=parse_time(ts(60)))
    assert result.log.state.inherited(CAROL, w3)


def test_catch_up_gives_an_inheriting_admin_the_history(edge: LogBuilder) -> None:
    edge.add("rotate", "alice", scope="wg", scope_id=W1, epoch=2)
    edge.add("rotate", "alice", scope="wg", scope_id=W1, epoch=3)
    log = _log(edge)
    state = log.state
    assert list(catchup.catch_up_epochs(state, WG, W1, CAROL)) == [1, 2, 3]
    assert list(catchup.catch_up_epochs(state, WG, W1, BOB)) == [3]  # explicit members: current epoch only
    assert list(catchup.catch_up_epochs(state, "org", state.org, CAROL)) == [1]
    missing = catchup.missing_wraps(log, DEV2, [WrapSlot(WG, W1, 3, CAROL)])
    assert [slot for slot in missing if slot.scope == WG and slot.recipient == CAROL] == [
        WrapSlot(WG, W1, 1, CAROL),
        WrapSlot(WG, W1, 2, CAROL),
    ]


def test_the_inherited_read_never_widens_the_credential_scope(edge: LogBuilder) -> None:
    state = _log(edge).state
    # dev2 is assigned to W1, which carol reads only as admin: not eligible for carol's CK
    assert cred_readers([state], CAROL) == frozenset({CAROL})
    assert DEV2 in cred_readers([state], BOB)  # bob reads W1 explicitly
