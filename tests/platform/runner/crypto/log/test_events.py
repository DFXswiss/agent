"""Copied from agent-core tests/platform/hub/log/test_events.py at 3d02f01a237c.

U-HUB-events (rules part): E1-E9, kind matrix, grace period, sizes (agent_hub.log.events)."""

from __future__ import annotations

from datetime import timedelta
from typing import cast

import pytest

from agent_runner.crypto import event as event_format
from agent_runner.crypto.encoding import b64u_encode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log import events, verify
from agent_runner.crypto.log.state import ORG as ORG_SCOPE
from agent_runner.crypto.log.vectors import ORG, START, W0, W1, W2, LogBuilder, make_event, pid, sig_pk

NOW = START + timedelta(hours=5)


def _event(
    author: str, kind: str, scope: str, scope_id: str, epoch: int, /, *, lh: int, **extra: str
) -> dict[str, object]:
    return make_event(
        author, kind, scope=scope, scope_id=scope_id, epoch=epoch, lh=lh, label=f"{author}-{kind}-{lh}", **extra
    )


def test_matrix_rows_are_unique_and_complete() -> None:
    keys = [(rule.kind, rule.scope, author) for rule in events.KIND_RULES for author in rule.authors]
    assert len(keys) == len(set(keys))
    assert {rule.right for rule in events.KIND_RULES} == set(events.RIGHTS)
    assert events.kind_rule("prompt", "wg", "user").right == events.WG_WRITE
    with pytest.raises(CryptoError, match="kind_not_allowed"):
        events.kind_rule("prompt", "wg", "device")
    assert events.SIZE_LIMITS == {"default_ct_bytes": 262144, "session.terminal": 65536}


def test_rights_per_rule(main_log: verify.VerifiedLog) -> None:
    state = main_log.state
    header = {"author": pid("alice"), "scope_id": W1, "ref": pid("alice")}
    assert events.has_right(events.kind_rule("org.profile", "org", "user"), state, header)
    assert events.has_right(events.kind_rule("member.profile", "org", "user"), state, header)
    assert not events.has_right(events.kind_rule("member.profile", "org", "user"), state, {**header, "ref": pid("bob")})
    assert not events.has_right(
        events.kind_rule("member.profile", "org", "user"), state, {**header, "author": pid("bob")}
    )
    assert events.has_right(events.kind_rule("prompt", "wg", "user"), state, header)
    device = {**header, "author": pid("device")}
    assert events.has_right(events.kind_rule("session.output", "wg", "device"), state, device)
    assert not events.has_right(events.kind_rule("session.output", "wg", "device"), state, {**device, "scope_id": W0})


def test_header_rules(main_log: verify.VerifiedLog) -> None:
    state = main_log.state
    rule = events.kind_rule("device.profile", "org", "user")
    base = {"wg": "", "ref": pid("device"), "scope_id": ORG, "author": pid("alice")}
    events.check_header_rules(rule, base, state)
    with pytest.raises(CryptoError, match="header_invalid"):
        events.check_header_rules(rule, {**base, "ref": "tasks.list"}, state)
    with pytest.raises(CryptoError, match="header_invalid"):
        events.check_header_rules(rule, {**base, "wg": W1}, state)


def test_reader_accepts_old_epochs_but_hub_does_not(main_log: verify.VerifiedLog) -> None:
    old = _event("alice", "settings.workgroup", "wg", W1, 1, lh=26, wg=W1)
    header, rule = events.check_event(old, main_log)
    assert header["epoch"] == 1
    assert rule.kind == "settings.workgroup"
    with pytest.raises(CryptoError, match="epoch_stale"):
        events.check_event_hub(old, main_log, now=NOW, rotated_at=None, session_device=None)


def test_grace_period_uses_the_given_clock(main_log: verify.VerifiedLog) -> None:
    state = main_log.state
    header = {"scope": "wg", "scope_id": W1, "author": pid("device"), "epoch": 1}
    rotated = START + timedelta(minutes=18)
    events.check_epoch(state, header, now=rotated + events.GRACE, rotated_at=rotated)
    events.check_epoch(state, {**header, "epoch": 2}, now=NOW, rotated_at=None)
    with pytest.raises(CryptoError, match="epoch_stale"):
        events.check_epoch(state, header, now=rotated + events.GRACE + timedelta(milliseconds=1), rotated_at=rotated)
    with pytest.raises(CryptoError, match="epoch_stale"):
        events.check_epoch(state, header, now=rotated, rotated_at=None)
    with pytest.raises(CryptoError, match="epoch_stale"):  # a removed author is never covered
        events.check_epoch(state, {**header, "author": pid("bob")}, now=rotated, rotated_at=rotated)
    with pytest.raises(CryptoError, match="epoch_stale"):  # nor an excluded (decertified) one
        events.check_epoch(state, header, now=rotated, rotated_at=rotated, excluded=[pid("device")])
    with pytest.raises(CryptoError, match="epoch_stale"):  # only the previous epoch
        events.check_epoch(
            state, {**header, "scope": ORG_SCOPE, "scope_id": ORG, "epoch": 1}, now=rotated, rotated_at=rotated
        )


def test_size_is_checked_before_the_signature(main_log: verify.VerifiedLog) -> None:
    sealed = _event("device", "session.terminal", "wg", W1, 2, lh=26, wg=W1, task=pid("task"), session=pid("session"))
    big = {**sealed, "ct": b64u_encode(bytes(event_format.MAX_TERMINAL_CT_BYTES + 1))}
    with pytest.raises(CryptoError, match="size_exceeded"):
        events.check_event(big, main_log)
    events.check_event(sealed, main_log, session_device=pid("device"))


def test_event_object_format(main_log: verify.VerifiedLog) -> None:
    sealed = _event("alice", "org.profile", "org", ORG, 3, lh=26)
    with pytest.raises(CryptoError, match="header_invalid"):
        events.check_event({**sealed, "extra": 1}, main_log)
    with pytest.raises(CryptoError, match="header_invalid"):
        events.check_event({**sealed, "header": {**cast(dict[str, object], sealed["header"]), "alg": "none"}}, main_log)


def test_hub_accepts_current_epoch_and_refuses_lost_rights(main_log: verify.VerifiedLog) -> None:
    current = _event("alice", "org.profile", "org", ORG, 3, lh=26)
    assert events.check_event_hub(current, main_log, now=NOW, rotated_at=None, session_device=None)["epoch"] == 3
    earlier = _event("alice", "org.profile", "org", ORG, 1, lh=5)
    events.check_event(earlier, main_log)  # readers verify at lh
    removed = _event("carol", "org.profile", "org", ORG, 1, lh=8)
    events.check_event(removed, main_log)  # carol was admin at lh 8
    with pytest.raises(CryptoError, match="author_not_writer"):
        events.check_event_hub(removed, main_log, now=NOW, rotated_at=None, session_device=None)


def test_replay() -> None:
    sealed = _event("alice", "org.profile", "org", ORG, 3, lh=26)
    assert events.check_replay(None, sealed) is False
    assert events.check_replay(sealed, dict(sealed)) is True
    with pytest.raises(CryptoError, match="event_conflict"):
        events.check_replay(sealed, {**sealed, "sig": sealed["ct"]})


def test_devices_do_not_write_into_archived_workgroups() -> None:
    log = LogBuilder()
    log.genesis()
    log.add("wg_create", "alice", wg=W2, epoch=1)
    log.invite_device("alice", "device", [W2])
    log.accept("device", "device")
    log.add("wg_archive", "alice", wg=W2)
    verified = verify.verify_log(log.entries, sig_pk("alice"))
    sealed = _event("device", "operation.result", "wg", W2, 1, lh=4, wg=W2, ref=pid("op"))
    with pytest.raises(CryptoError, match="author_not_writer"):
        events.check_event(sealed, verified)
    events.check_event(_event("device", "operation.result", "wg", W2, 1, lh=3, wg=W2, ref=pid("op")), verified)
