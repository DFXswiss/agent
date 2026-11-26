"""Copied from tests/platform/hub/log/test_platform.py.

U-HUB-log (platform part): P1-P4, certification state, platform access (agent_hub.log.platform)."""

from __future__ import annotations

import pytest

from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.identity import sign_identity
from agent_runner.crypto.log import platform, verify
from agent_runner.crypto.log.entries import Head
from agent_runner.crypto.log.vectors import (
    ALICE_CERT_SEQ,
    BOB_CERT_SEQ,
    BOB_RENEWED_SEQ,
    DAVE_CERT_SEQ,
    ERIN_INVITE_SEQ,
    EXPIRES,
    ORG,
    PDEV_RECERT_SEQ,
    PDEV_SEQ,
    PLATFORM_COUNT,
    PRE_SUPPORT_COUNT,
    W1,
    LogBuilder,
    PlatformBuilder,
    pid,
    pub,
    recertified_builder,
    secrets,
    sig_pk,
    uid,
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
    assert state.head_value() == Head(PLATFORM_COUNT - 1, state.head)
    assert state.invites[secrets("platform-user").invite_id].kind == "user"


def test_decertified_device_can_be_certified_again(plat: PlatformBuilder) -> None:
    log = PlatformBuilder(plat.entries)
    log.add(
        "device_invite", invite_id=secrets("qdev-again").invite_id, pk_inv=secrets("qdev-again").pk_inv, expires=EXPIRES
    )
    log.certify("qdev", code_label="qdev-again")
    state = platform.verify_platform_log(log.entries, ROOT).state
    assert state.status(pid("qdev")) == platform.CERTIFIED
    assert state.devices[pid("qdev")] == PLATFORM_COUNT + 1
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


def test_switches_and_operator_subscriptions(plat: PlatformBuilder) -> None:
    """ADR-079: the latest entry of a switch or an organization counts; without one a switch is off and an organization
    is not granted; an organization without an `org_platform_access` of its own follows `open_platform_access`."""
    log = platform.verify_platform_log(plat.entries, ROOT)
    other = pid("org/other")
    before = log.states[16]
    assert not before.policy("open_platform_access")
    assert not before.platform_access(other, private=False)
    assert not before.operator_subscribed(ORG)
    opened = log.states[17]
    assert opened.policy("open_platform_access")
    assert opened.platform_access(other, private=False)
    assert not opened.platform_access(ORG, private=False)  # its own `granted: false` wins over the switch
    assert log.states[18].operator_subscribed(ORG)
    assert not log.states[18].operator_subscribed(other)
    assert log.states[19].has_role(pid("alice"), "operator")
    state = log.state
    assert state.policy("open_platform_invites")
    assert not state.policy("open_platform_access")
    assert not state.platform_access(other, private=False)
    assert not state.operator_subscribed(ORG)
    projected = state.projection()
    assert projected["policies"] == {"open_platform_access": False, "open_platform_invites": True}
    assert projected["operator_subscriptions"] == {ORG: False}


def test_user_certifications(plat: PlatformBuilder) -> None:
    log = platform.verify_platform_log(plat.entries, ROOT)
    state = log.state
    assert state.user_certified(pid("alice"))
    assert state.user_certified(pid("bob"))
    assert not state.user_certified(pid("carol"))
    assert not log.states[BOB_RENEWED_SEQ - 1].user_certified(pid("bob"))
    assert state.users[pid("bob")] == BOB_RENEWED_SEQ
    ended = state.user_certifications[BOB_CERT_SEQ]
    assert (ended.user, ended.sig, ended.decertified_seq) == (pid("bob"), sig_pk("bob"), BOB_CERT_SEQ + 1)
    assert state.user_invites[ERIN_INVITE_SEQ] == secrets("erin-platform").invite_id
    assert state.invites[secrets("erin-platform").invite_id].ts == plat.entries[ERIN_INVITE_SEQ]["body"]["ts"]
    assert state.projection()["users"] == [
        {"user": user, "certify_seq": seq, "certified": True}
        for user, seq in sorted(
            {
                pid("alice"): ALICE_CERT_SEQ,
                pid("dave"): DAVE_CERT_SEQ,
                pid("bob"): BOB_RENEWED_SEQ,
                pid("sam"): PRE_SUPPORT_COUNT,
                pid("tom"): PRE_SUPPORT_COUNT + 2,
            }.items()
        )
    ]
    again = PlatformBuilder(plat.entries)
    again.add("user_decertify", user=pid("dave"))
    again.add("user_certify", user=pid("dave"), pub=pub("dave"))
    renewed = platform.verify_platform_log(again.entries, ROOT).state
    assert renewed.users[pid("dave")] == PLATFORM_COUNT + 1
    assert renewed.user_certifications[DAVE_CERT_SEQ].decertified_seq == PLATFORM_COUNT


def test_identity_anchors(plat: PlatformBuilder) -> None:
    state = platform.verify_platform_log(plat.entries, ROOT).state
    alice, bob, dave, erin = pid("alice"), pid("bob"), pid("dave"), uid("erin")
    assert state.identity_anchored(ALICE_CERT_SEQ, alice, pub("alice"), "")
    assert state.identity_anchored(ALICE_CERT_SEQ, alice, pub("alice"), "", current=True)
    # an ended certification anchors for readers (history), not at the hub
    assert state.identity_anchored(BOB_CERT_SEQ, bob, pub("bob"), "")
    assert not state.identity_anchored(BOB_CERT_SEQ, bob, pub("bob"), "", current=True)
    assert state.identity_anchored(BOB_RENEWED_SEQ, bob, pub("hub"), "", current=True)
    assert not state.identity_anchored(ALICE_CERT_SEQ, alice, pub("dave"), "")  # other keys
    assert not state.identity_anchored(ALICE_CERT_SEQ, dave, pub("alice"), "")  # other user
    proof = sign_identity(secrets("erin-platform").signing, erin, pub("erin"))
    assert not state.identity_anchored(ALICE_CERT_SEQ, alice, pub("alice"), proof)  # a certification takes no proof
    # the identity proof of a platform invitation
    assert state.identity_anchored(ERIN_INVITE_SEQ, erin, pub("erin"), proof)
    assert state.identity_anchored(ERIN_INVITE_SEQ, erin, pub("erin"), proof, current=True)
    assert not state.identity_anchored(ERIN_INVITE_SEQ, erin, pub("erin"), "")
    assert not state.identity_anchored(ERIN_INVITE_SEQ, erin, pub("dave"), proof)
    assert not state.identity_anchored(
        ERIN_INVITE_SEQ, dave, pub("erin"), sign_identity(secrets("erin-platform").signing, dave, pub("erin"))
    )  # not the ID derived from the invitation
    other = sign_identity(secrets("platform-user").signing, erin, pub("erin"))
    assert not state.identity_anchored(ERIN_INVITE_SEQ, erin, pub("erin"), other)
    # neither a certification nor a user invitation
    assert not state.identity_anchored(PDEV_SEQ, pid("pdev"), pub("pdev"), "")
    assert not state.identity_anchored(PLATFORM_COUNT, alice, pub("alice"), "")


def test_users_and_devices_never_share_an_id(plat: PlatformBuilder) -> None:
    device_as_user = PlatformBuilder(plat.entries)
    device_as_user.add("user_certify", user=pid("qdev"), pub=pub("qdev"))
    with pytest.raises(CryptoError, match="log_rights"):
        platform.verify_platform_log(device_as_user.entries, ROOT)
    never_certified = PlatformBuilder(plat.entries)
    never_certified.add("user_decertify", user=pid("carol"))
    with pytest.raises(CryptoError, match="log_rights"):
        platform.verify_platform_log(never_certified.entries, ROOT)


def test_support_role_is_bound_to_the_certification_valid_at_the_grant(plat: PlatformBuilder) -> None:
    """ADR-096: a decertified person leaves the roster; a renewed certification needs a new grant."""
    log = PlatformBuilder(plat.entries)
    log.add("user_decertify", user=pid("sam"))
    left = platform.verify_platform_log(log.entries, ROOT).state
    assert pid("sam") not in left.support_roster()
    assert pid("sam") in left.support_authors()
    log.add("user_certify", user=pid("sam"), pub=pub("sam"))
    renewed = platform.verify_platform_log(log.entries, ROOT).state
    assert pid("sam") not in renewed.support_roster()
    log.add("role_revoke", subject=pid("sam"), role="support")
    log.add("role_grant", subject=pid("sam"), role="support")
    again = platform.verify_platform_log(log.entries, ROOT).state
    assert again.support_roster()[pid("sam")].sig == sig_pk("sam")
    assert again.support_bound[pid("sam")] == len(log.entries) - 3
