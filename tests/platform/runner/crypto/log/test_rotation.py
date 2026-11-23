"""Copied from agent-core tests/platform/hub/log/test_rotation.py at 07f6d21b261a.

U-HUB-log: the rotate set R and the atomic append batch (agent_hub.log.rotation, agent_hub.log.batch)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest

from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log import rotation, verify
from agent_runner.crypto.log.batch import check_batch
from agent_runner.crypto.log.state import ORG, WG
from agent_runner.crypto.log.vectors import (
    ORG as ORG_ID,
)
from agent_runner.crypto.log.vectors import (
    START,
    W0,
    W1,
    W2,
    LogBuilder,
    edge_builder,
    make_wrap,
    pid,
    sig_pk,
)

ALICE = sig_pk("alice")
NOW = START + timedelta(hours=1)


def _states(entries: list[dict[str, object]]) -> verify.VerifiedLog:
    return verify.verify_log(entries, ALICE)


def _r(log: LogBuilder) -> frozenset[tuple[str, str]]:
    verified = _states(log.entries)
    return rotation.required_set(verified.states[-2], verified.state, rotation.entry_subject(log.entries[-1]["body"]))


def test_required_set_for_each_trigger() -> None:
    remove = edge_builder()
    remove.add("remove", "alice", subject=pid("bob"))
    assert _r(remove) == {(ORG, ORG_ID), (WG, W0), (WG, W1), (WG, W2)}
    unassign = edge_builder()
    unassign.add("device_unassign", "alice", device=pid("dev2"), wg=W1)
    assert _r(unassign) == {(ORG, ORG_ID), (WG, W1)}
    ungrant = edge_builder()
    ungrant.add("ungrant", "alice", subject=pid("bob"), wg=W2)
    assert _r(ungrant) == {(WG, W2)}
    lonely = edge_builder()
    lonely.add("remove", "alice", subject=pid("device"))
    assert _r(lonely) == frozenset()


def test_required_set_is_empty_for_entries_that_do_not_reduce_reading() -> None:
    log = edge_builder()
    log.add("grant", "alice", subject=pid("carol"), wg=W1, access="read")
    assert _r(log) == frozenset()
    log.add("role", "alice", subject=pid("carol"), role="member")
    assert _r(log) == frozenset()
    log.add("rotate", "alice", scope="wg", scope_id=W1, epoch=2)
    assert _r(log) == frozenset()


def test_entry_subject_and_json() -> None:
    assert rotation.entry_subject({"by": "a", "subject": "s"}) == "s"
    assert rotation.entry_subject({"by": "a", "device": "d"}) == "d"
    assert rotation.entry_subject({"by": "a"}) == "a"
    assert rotation.as_json(frozenset({(WG, "b"), (ORG, "a")})) == [
        {"scope": "org", "scope_id": "a"},
        {"scope": "wg", "scope_id": "b"},
    ]


def _batch(*steps: Callable[[LogBuilder], object]) -> tuple[verify.VerifiedLog, list[dict[str, object]]]:
    base = edge_builder()
    count = len(base.entries)
    for step in steps:
        step(base)
    return _states(base.entries[:count]), base.entries[count:]


def test_genesis_batch_needs_the_self_wraps() -> None:
    log = LogBuilder()
    log.genesis()
    own = [make_wrap("alice", "org", ORG_ID, 1, "alice"), make_wrap("alice", "wg", W0, 1, "alice")]
    result = check_batch(None, log.entries, own, wrapped_by=pid("alice"), founder=ALICE, now=NOW)
    assert result.log.head.seq == 0
    assert result.required == frozenset()
    assert len(result.wraps) == 2
    with pytest.raises(CryptoError, match="rotation_required"):
        check_batch(None, log.entries, own[:1], wrapped_by=pid("alice"), founder=ALICE, now=NOW)


def test_batch_with_excluded_recipient_and_pending_leave() -> None:
    base, append = _batch(
        lambda b: b.add("remove", "bob", subject=pid("bob")),
        lambda b: b.add("rotate", "alice", scope="wg", scope_id=W1, epoch=2),
    )
    wraps = [make_wrap("alice", "wg", W1, 2, "alice")]
    with pytest.raises(CryptoError, match="rotation_required"):
        check_batch(base, append, wraps, wrapped_by=pid("alice"), now=NOW)
    result = check_batch(base, append, wraps, wrapped_by=pid("alice"), now=NOW, excluded=[pid("dev2")])
    assert result.required == frozenset()
    assert result.log.state.pending == {(ORG, ORG_ID), (WG, W0), (WG, W2)}


def test_batch_with_two_triggers_needs_the_union() -> None:
    base, append = _batch(
        lambda b: b.add("ungrant", "alice", subject=pid("bob"), wg=W1),
        lambda b: b.add("device_unassign", "alice", device=pid("dev2"), wg=W1),
        lambda b: b.add("rotate", "alice", scope="org", scope_id=ORG_ID, epoch=2),
        lambda b: b.add("rotate", "alice", scope="wg", scope_id=W1, epoch=2),
    )
    wraps = [make_wrap("alice", "org", ORG_ID, 2, r) for r in ("alice", "bob", "carol")]
    wraps.append(make_wrap("alice", "wg", W1, 2, "alice"))
    result = check_batch(base, append, wraps, wrapped_by=pid("alice"), now=NOW)
    assert result.required == {(ORG, ORG_ID), (WG, W1)}


def test_batch_refuses_a_duplicate_rotate() -> None:
    base, append = _batch(
        lambda b: b.add("ungrant", "alice", subject=pid("bob"), wg=W1),
        lambda b: b.add("rotate", "alice", scope="wg", scope_id=W1, epoch=2),
        lambda b: b.add("rotate", "alice", scope="wg", scope_id=W1, epoch=3),
    )
    with pytest.raises(CryptoError, match="rotation_required"):
        check_batch(base, append, [], wrapped_by=pid("alice"), now=NOW)
