"""Copied from agent-core tests/platform/hub/log/test_wraps.py at 3d02f01a237c.

U-HUB-wraps: W1-W3 and catch-up (agent_hub.log.wraps, agent_hub.log.catchup)."""

from __future__ import annotations

import pytest

from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log import catchup, platform, verify, wraps
from agent_runner.crypto.log.vectors import (
    ORG,
    PDEV_RECERT_SEQ,
    W0,
    W1,
    LogBuilder,
    admit_builder,
    make_wrap,
    pid,
    recertified_builder,
    sig_pk,
)


def slot(scope: str, scope_id: str, epoch: int, recipient: str) -> wraps.WrapSlot:
    return wraps.WrapSlot(scope, scope_id, epoch, pid(recipient))


def test_may_wrap(main_log: verify.VerifiedLog) -> None:
    alice, device = pid("alice"), pid("device")
    assert wraps.may_wrap(main_log, slot("wg", W1, 2, "device"), alice)
    assert wraps.may_wrap(main_log, slot("wg", W1, 2, "device"), device)  # W1 self
    assert not wraps.may_wrap(main_log, slot("wg", W1, 0, "device"), alice)
    assert not wraps.may_wrap(main_log, slot("cred", W1, 1, "device"), alice)
    assert not wraps.may_wrap(main_log, slot("wg", W0, 4, "device"), alice)  # device not assigned to W0
    assert not wraps.may_wrap(main_log, slot("wg", W1, 2, "pdev"), alice, [pid("pdev")])
    assert wraps.may_wrap(main_log, slot("org", ORG, 3, "pdev"), device)


def test_cred_readers_and_rules(main_log: verify.VerifiedLog) -> None:
    states = [main_log.state]
    assert wraps.cred_readers(states, pid("alice")) == {pid("alice"), pid("device"), pid("pdev")}
    assert wraps.cred_readers(states, pid("alice"), [pid("pdev")]) == {pid("alice"), pid("device")}
    assert wraps.cred_readers(states, pid("bob")) == {pid("bob")}
    assert wraps.cred_readers([main_log.state_at(14)], pid("bob")) == {pid("bob"), pid("pdev"), pid("device")}
    owner = slot("cred", pid("alice"), 1, "device")
    assert wraps.may_wrap_cred(states, owner, pid("alice"), 1)
    assert not wraps.may_wrap_cred(states, owner, pid("alice"), 0)
    assert not wraps.may_wrap_cred(states, wraps.WrapSlot("wg", pid("alice"), 1, pid("device")), pid("alice"), 1)


def test_wrap_object_format(main_log: verify.VerifiedLog) -> None:
    good = make_wrap("alice", "wg", W1, 2, "device")
    assert wraps.check_wrap(good, pid("alice"), main_log) == slot("wg", W1, 2, "device")
    with pytest.raises(CryptoError, match="header_invalid"):
        wraps.check_wrap({**good, "epoch": True}, pid("alice"), main_log)
    with pytest.raises(CryptoError, match="header_invalid"):
        wraps.check_wrap({**good, "epoch": "2"}, pid("alice"), main_log)
    with pytest.raises(CryptoError, match="wrap_unauthorized"):
        wraps.check_cred_wrap(good, pid("hub"), [main_log], 1)


def test_wrap_slot(main_log: verify.VerifiedLog) -> None:
    first, second = slot("org", ORG, 1, "alice"), slot("wg", W1, 1, "alice")
    assert sorted([second, first]) == [first, second]
    assert first.to_json() == {"scope": "org", "scope_id": ORG, "epoch": 1, "recipient_id": pid("alice")}


def test_catch_up_for_a_new_member(main_log: verify.VerifiedLog) -> None:
    # after accept (seq 3) bob holds the package keys; alice wraps for him on her next sync
    early = verify.VerifiedLog(main_log.entries[:4], main_log.states[:4])
    existing = {slot("org", ORG, 1, "alice"), slot("wg", W0, 1, "alice"), slot("wg", W1, 1, "alice")}
    plan = catchup.missing_wraps(early, pid("alice"), existing)
    assert plan == sorted([slot("org", ORG, 1, "bob"), slot("wg", W0, 1, "bob"), slot("wg", W1, 1, "bob")])
    assert catchup.missing_wraps(early, pid("erin"), existing) == []
    assert catchup.missing_cred_wraps([early], pid("bob"), pid("bob"), current_epoch=1, existing=[]) == [
        slot("cred", pid("bob"), 1, "bob")
    ]


def test_cred_wrap_signer_prefers_the_active_principal(main: LogBuilder) -> None:
    recert = platform.verify_platform_log(recertified_builder().entries, sig_pk("root")).state
    old_org = LogBuilder(entries=main.entries)
    old_org.add("remove", "alice", subject=pid("pdev"))
    org_a = verify.verify_log(old_org.entries, sig_pk("alice"), platform=recert)
    other = LogBuilder(org=pid("org/b"))
    other.genesis()
    other.add("device_assign", "alice", device=pid("pdev"), wg=W0, platform_seq=PDEV_RECERT_SEQ)
    org_b = verify.verify_log(other.entries, sig_pk("alice"), platform=recert)
    new_key = make_wrap("pdev-new", "cred", pid("alice"), 1, "alice")
    assert wraps.check_cred_wrap(new_key, pid("pdev"), [org_a, org_b], 1, require_active=True).recipient == pid("alice")
    old_key = make_wrap("pdev", "cred", pid("alice"), 1, "alice")
    with pytest.raises(CryptoError, match="signature_invalid"):
        wraps.check_cred_wrap(old_key, pid("pdev"), [org_a, org_b], 1)
    with pytest.raises(CryptoError, match="wrap_unauthorized"):
        wraps.check_cred_wrap(old_key, pid("pdev"), [org_a], 1, require_active=True)


def test_an_org_that_admitted_the_owner_gets_no_credential_key(plat_state: platform.PlatformState) -> None:
    admitted = verify.verify_log(admit_builder().entries, sig_pk("alice"), platform=plat_state)
    bob = admitted.state.principals[pid("bob")]
    assert (bob.active, bob.consented, admitted.state.wg_access(pid("bob"), W1)) == (True, False, "write")
    assert wraps.cred_readers([admitted.state], pid("bob")) == {pid("bob")}
    assert wraps.cred_readers([admitted.state], pid("alice")) == {pid("alice"), pid("device"), pid("pdev")}
    with pytest.raises(CryptoError, match="wrap_unauthorized"):
        wraps.check_cred_wrap(make_wrap("bob", "cred", pid("bob"), 1, "device"), pid("bob"), [admitted], 1)
