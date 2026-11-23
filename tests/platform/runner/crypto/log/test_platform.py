"""Copied from agent-core tests/platform/hub/log/test_platform.py at e051302eeb8e.

U-HUB-log (platform part): P1-P4, certification state, platform access (agent_hub.log.platform)."""

from __future__ import annotations

import pytest

from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log import platform, verify
from agent_runner.crypto.log.entries import Head
from agent_runner.crypto.log.vectors import (
    EXPIRES,
    ORG,
    PDEV_RECERT_SEQ,
    PDEV_SEQ,
    W1,
    LogBuilder,
    PlatformBuilder,
    pid,
    recertified_builder,
    secrets,
    sig_pk,
)

ROOT = sig_pk("root")


def test_certification_state(plat: PlatformBuilder) -> None:
    log = platform.verify_platform_log(plat.entries, ROOT)
    state = log.state
    assert state.status(pid("pdev")) == platform.CERTIFIED
    assert state.status(pid("qdev")) == platform.DECERTIFIED
    assert state.status(pid("erin")) == platform.UNKNOWN
    assert state.valid(2)
    assert not state.valid(7)
    assert not state.valid(3)
    assert state.certification(2) is not None
    assert state.certification(3) is None
    ended = state.certification(7)
    assert ended is not None
    assert ended.decertified_seq == 8
    assert log.states[4].has_role(pid("alice"), "inviter")
    assert not state.has_role(pid("alice"), "inviter")
    assert log.states[5].platform_access(ORG, private=False)
    assert not state.platform_access(ORG, private=False)
    assert state.platform_access(ORG, private=True)
    assert not log.states[0].platform_access(pid("org/other"), private=False)
    assert state.head_value() == Head(10, state.head)
    assert state.invites[secrets("platform-user").invite_id].kind == "user"


def test_decertified_device_can_be_certified_again(plat: PlatformBuilder) -> None:
    log = PlatformBuilder(plat.entries)
    log.add(
        "device_invite", invite_id=secrets("qdev-again").invite_id, pk_inv=secrets("qdev-again").pk_inv, expires=EXPIRES
    )
    log.certify("qdev", code_label="qdev-again")
    state = platform.verify_platform_log(log.entries, ROOT).state
    assert state.status(pid("qdev")) == platform.CERTIFIED
    assert state.devices[pid("qdev")] == 12
    ended = state.certification(7)
    assert ended is not None
    assert ended.decertified_seq == 8


def test_append_platform(plat: PlatformBuilder) -> None:
    first = platform.append_platform(None, plat.entries[:3], ROOT)
    assert first.state.seq == 2
    second = platform.append_platform(first, plat.entries[3:], ROOT)
    assert second.states == platform.verify_platform_log(plat.entries, ROOT).states
    assert platform.append_platform(second, [], ROOT).state == second.state
    with pytest.raises(CryptoError, match="log_empty"):
        platform.append_platform(None, [], ROOT)
    with pytest.raises(CryptoError, match="seq_conflict"):
        platform.append_platform(None, plat.entries[1:2], ROOT)


def test_root_key_pinning(plat: PlatformBuilder) -> None:
    with pytest.raises(CryptoError, match="platform_root_mismatch"):
        platform.verify_platform_log(plat.entries, sig_pk("hub"))


def test_recertified_device_does_not_revive_its_old_certification(main: LogBuilder) -> None:
    recert = platform.verify_platform_log(recertified_builder().entries, ROOT).state
    assert recert.status(pid("pdev")) == platform.CERTIFIED
    assert not recert.valid(PDEV_SEQ)
    assert recert.valid(PDEV_RECERT_SEQ)
    log = verify.verify_log(main.entries, sig_pk("alice"), platform=recert)  # the history stays valid
    assert recert.excluded(log.state) == {pid("pdev")}
    assert pid("pdev") not in log.state.readers("wg", W1, recert.excluded(log.state))
    again = LogBuilder(entries=main.entries)
    again.add("remove", "alice", subject=pid("pdev"))
    again.add("device_assign", "alice", device=pid("pdev"), wg=W1, platform_seq=PDEV_RECERT_SEQ)
    fresh = verify.verify_log(again.entries, sig_pk("alice"), platform=recert)
    assert recert.excluded(fresh.state) == frozenset()
    assert fresh.state.principals[pid("pdev")].sig == sig_pk("pdev-new")
