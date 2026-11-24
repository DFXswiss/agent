"""Copied from tests/platform/hub/log/test_agents.py.

U-HUB-rights (ADR-082): agents as sub-principals of their users in the membership log, their rights through
`acting`, their removal with the user, their return, and their place among the credential readers."""

from __future__ import annotations

import pytest

from agent_runner.crypto.encoding import parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log import platform, rotation, verify, wraps
from agent_runner.crypto.log.batch import check_batch
from agent_runner.crypto.log.state import ORG, WG
from agent_runner.crypto.log.vectors import (
    AGENTS_COUNT,
    ALICE_CERT_SEQ,
    DAVE_CERT_SEQ,
    EDGE_COUNT,
    W0,
    W1,
    W2,
    LogBuilder,
    agent_member,
    agents_builder,
    edge_builder,
    make_wrap,
    other_org_builder,
    pid,
    sig_pk,
    ts,
)
from agent_runner.crypto.log.vectors import ORG as ORG_ID

ALICE = sig_pk("alice")


def _verify(entries: list[dict[str, object]], plat: platform.PlatformState | None = None) -> verify.VerifiedLog:
    return verify.verify_log(entries, ALICE, platform=plat)


def _prefix(count: int) -> LogBuilder:
    return LogBuilder(entries=agents_builder().entries[:count])


def test_an_agent_has_exactly_the_rights_of_its_user() -> None:
    log = _verify(agents_builder().entries)
    alice, bob, bot, bot2, bot5 = (pid(x) for x in ("alice", "bob", "bot", "bot2", "bot5"))
    created = log.state_at(1)
    assert created.access == {(alice, W1): "write"}  # the agent created W1 for its user
    assert created.acting(bot) == alice
    assert created.acting(alice) == alice
    assert created.acting(pid("nobody")) == pid("nobody")
    assert created.is_admin(bot)
    assert created.admins() == [alice]
    assert created.explicit_access(bot, W1) == "write"
    assert created.can_write_wg(bot, W1)
    assert created.readers(WG, W1) == sorted([alice, bot])
    granted = log.state_at(7)
    assert granted.can_write_wg(bot2, W1)
    assert not granted.is_admin(bot2)
    assert not granted.inherited(bot2, W1)
    assert not granted.assigned(bot2, W1)
    assert granted.device_wgs(bot2) == []
    final = log.state
    assert final.agent_of(alice) == final.principals[bot]
    assert final.agent_of(pid("carol")) is None
    assert final.member(bot2) is None
    assert final.readable_scopes(bot5) == final.readable_scopes(bob) == sorted([(ORG, ORG_ID), (WG, W0)])


def test_the_agent_of_an_admin_reads_by_inheritance() -> None:
    log = edge_builder()
    log.agent_add("carol", "bot3", "carol", 1)
    state = _verify(log.entries).state
    bot3 = pid("bot3")
    assert state.explicit_access(bot3, W1) is None
    assert state.wg_access(bot3, W1) == "read"
    assert state.inherited(bot3, W1)
    assert bot3 in state.readers(WG, W1)
    assert state.admins() == sorted([pid("alice"), pid("carol")])


def test_an_agent_admits_with_the_anchor_of_its_user(plat_state: platform.PlatformState) -> None:
    log = LogBuilder()
    log.genesis(agent="bot")
    log.admit("bot", "dave", DAVE_CERT_SEQ, ALICE_CERT_SEQ)
    dave = _verify(log.entries, plat_state).state.principals[pid("dave")]
    assert (dave.active, dave.consented) == (True, False)
    prefix = verify.verify_log(log.entries[:1], ALICE, platform=plat_state)
    wraps_for_dave = [make_wrap("bot", "org", ORG_ID, 1, "dave"), make_wrap("bot", "wg", W0, 1, "dave")]
    result = check_batch(prefix, log.entries[1:], wraps_for_dave, wrapped_by=pid("bot"), platform=plat_state)
    assert result.log.state.member(pid("dave")) is not None
    other = LogBuilder()
    other.genesis(agent="bot")
    other.admit("bot", "dave", DAVE_CERT_SEQ, DAVE_CERT_SEQ)  # the anchor of another user
    with pytest.raises(CryptoError, match="log_admit"):
        _verify(other.entries, plat_state)


@pytest.mark.parametrize("by", ["bob", "bot5"])
def test_an_agent_removed_with_its_user_comes_back_by_agent_add(by: str) -> None:
    log = _prefix(16)
    log.accept("bob", code_label="bob-again")
    log.agent_add(by, "bot5", "bob", 2)
    state = _verify(log.entries).state
    assert state.agent_of(pid("bob")) == state.principals[pid("bot5")]
    assert state.principals[pid("bot5")].joined_seq == 17


def test_an_agent_is_removed_with_its_user_and_not_revoked() -> None:
    state = _verify(agents_builder().entries[:15]).state
    bot5 = state.principals[pid("bot5")]
    assert (bot5.removed_seq, bot5.removed_with_user) == (14, True)
    assert wraps.revoked_agents([state]) == {pid("bot2"), pid("bot3")}
    assert state.agent_of(pid("bob")) is None


def test_who_may_remove_an_agent_or_its_user() -> None:
    for by, subject, pending in (
        ("carol", "bot4", False),  # the user removes its agent
        ("bot4", "bot4", True),  # the agent leaves
        ("bot4", "carol", True),  # the user leaves through its agent
        ("bot", "carol", False),  # the agent of an admin removes a member
        ("alice", "bot4", False),  # an admin removes the agent of a member
    ):
        log = _prefix(17)
        log.add("remove", by, subject=pid(subject))
        before, after = _verify(log.entries[:17]).state, _verify(log.entries).state
        assert after.member(pid("bot4")) is None
        assert bool(after.pending - before.pending) is pending, (by, subject)
    refused = _prefix(17)
    refused.add("remove", "bot5", subject=pid("bot4"))
    with pytest.raises(CryptoError, match="log_rights"):
        _verify(refused.entries)


def test_an_admin_demoting_itself_through_its_agent_owes_nothing_in_the_batch() -> None:
    log = edge_builder()
    log.agent_add("carol", "bot3", "carol", 1)
    count = len(log.entries)
    log.add("role", "bot3", subject=pid("carol"), role="member")
    before = _verify(log.entries[:count])
    body = log.entries[count]["body"]
    assert isinstance(body, dict)
    assert rotation.self_owed(before.state, body)
    result = check_batch(before, log.entries[count:], [], wrapped_by=pid("bot3"))
    assert result.required == frozenset()
    assert result.log.state.pending == {(WG, W1), (WG, W2)}
    assert count == EDGE_COUNT + 1


def test_credential_readers_over_several_logs() -> None:
    first = _verify(agents_builder().entries).state
    second = _verify(other_org_builder().entries).state
    assert wraps.cred_readers([first, second], pid("bob")) == {pid("bob"), pid("bot5")}
    assert wraps.cred_readers([second], pid("bob")) == {pid("bob")}
    assert wraps.cred_readers([first, second], pid("alice")) == {pid("alice"), pid("bot6")}
    assert wraps.revoked_agents([second]) == frozenset()
    assert wraps.revoked_agents([]) == frozenset()


def test_genesis_and_accept_record_the_agent_that_signed_them() -> None:
    state = _verify(agents_builder().entries[:6]).state
    assert state.principals[pid("alice")].join_agent == pid("bot")
    assert state.principals[pid("bob")].join_agent == pid("bot2")
    assert state.principals[pid("carol")].join_agent is None
    assert set(agent_member("bot2", "bob", 1)) == {"id", "pub", "gen", "cert"}


def test_the_agents_log_is_appended_at_the_hub() -> None:
    entries = agents_builder().entries
    log = verify.append_entries(None, entries, founder=ALICE, now=parse_time(ts(60)))
    assert log.head.seq == AGENTS_COUNT - 1
    assert log.states == _verify(entries).states


def test_the_rotation_of_a_removed_or_replaced_agent_is_owed_by_its_user() -> None:
    log = _verify(agents_builder().entries)
    carol, bob = pid("carol"), pid("bob")
    assert log.state_at(10).agent_owed == {}
    assert log.state_at(11).agent_owed == {(ORG, ORG_ID): carol, (WG, W0): carol}  # carol removed bot3
    assert log.state_at(13).agent_owed == {(ORG, ORG_ID): bob, (WG, W0): bob, (WG, W1): bob}  # bot2 replaced
    assert log.state_at(17).agent_owed == log.state_at(16).agent_owed  # an agent that leaves owes nothing
    assert log.state.agent_owed == {(WG, W1): bob}  # alice's rotations cleared the others
    assert log.state.projection()["agent_owed"] == [{"scope": WG, "scope_id": W1, "user": bob}]
    assert "agent_owed" not in log.state_at(10).projection()


def test_a_removal_records_whether_the_owner_revoked_the_agent() -> None:
    state = _verify(agents_builder().entries).state
    flags = {label: state.principals[pid(label)].removed_by_owner for label in ("bot", "bot2", "bot3", "bot4", "bot5")}
    assert flags == {"bot": False, "bot2": False, "bot3": True, "bot4": True, "bot5": False}
    log = _prefix(17)
    log.add("remove", "alice", subject=pid("bot4"))  # an admin: the agent only leaves this org
    removed = _verify(log.entries).state
    assert not removed.principals[pid("bot4")].removed_by_owner
    assert pid("bot4") not in wraps.revoked_agents([removed])
