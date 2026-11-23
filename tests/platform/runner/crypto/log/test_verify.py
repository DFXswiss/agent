"""Copied from agent-core tests/platform/hub/log/test_verify.py at 07f6d21b261a.

U-HUB-log, U-HUB-rights: L1-L10, the transition of every entry type, derived rights, the Spike D malicious-hub
cases and the property "incremental state = state rebuilt from the whole log"."""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from agent_runner.crypto import wrap
from agent_runner.crypto.encoding import parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log import platform, verify
from agent_runner.crypto.log.entries import Head, check_extends, entry_hash, sign_log_entry
from agent_runner.crypto.log.rotation import entry_subject, required_set
from agent_runner.crypto.log.state import ORG, WG, OrgState
from agent_runner.crypto.log.vectors import (
    EXPIRES,
    PDEV_SEQ,
    QDEV_SEQ,
    START,
    W0,
    W1,
    W2,
    LogBuilder,
    identity,
    main_builder,
    make_wrap,
    pid,
    secrets,
    sig_pk,
)
from agent_runner.crypto.log.vectors import (
    ORG as ORG_ID,
)
from agent_runner.crypto.log.wraps import check_wrap

ALICE = sig_pk("alice")


def test_final_rights_of_the_main_log(main_log: verify.VerifiedLog) -> None:
    state = main_log.state
    alice, bob, carol, device, pdev = (pid(x) for x in ("alice", "bob", "carol", "device", "pdev"))
    assert state.admins() == [alice]
    assert state.is_admin(alice)
    assert not state.is_admin(bob)
    assert state.member(bob) is None
    assert state.principals[bob].removed_seq == 24
    assert state.wg_access(alice, W0) == "write"
    assert state.wg_access(alice, W2) == "write"
    assert state.wg_access(device, W1) is None
    assert state.wg_access(alice, pid("wg/none")) is None
    assert state.can_write_wg(alice, W1)
    assert not state.can_write_wg(alice, W2)
    assert state.assigned(device, W1)
    assert state.assigned(pdev, W1)
    assert not state.assigned(device, W0)
    assert state.device_wgs(device) == [W1]
    assert state.device_wgs(bob) == []
    assert state.can_read(device, ORG, ORG_ID)
    assert not state.can_read(device, ORG, pid("org/other"))
    assert not state.can_read(carol, ORG, ORG_ID)
    assert state.readers(WG, W1) == sorted([alice, device, pdev])
    assert state.readers(WG, W1, excluded=[pdev]) == sorted([alice, device])
    assert state.readable_scopes(device) == sorted([(ORG, ORG_ID), (WG, W1)])
    assert state.epoch(ORG, ORG_ID) == 3
    assert state.epoch(WG, W0) == 4
    assert state.epoch(WG, "x") == 0
    assert state.epoch_authors[(WG, W1, 2)] == alice
    assert main_log.head == Head(26, entry_hash(main_log.entries[26]))


def test_derived_rights_along_the_log(main_log: verify.VerifiedLog) -> None:
    bob, device = pid("bob"), pid("device")
    assert main_log.state_at(3).wg_access(bob, W0) == "write"  # every member writes the default workgroup
    assert main_log.state_at(3).wg_access(bob, W1) == "read"  # from invite.wgs at accept
    assert main_log.state_at(10).is_admin(bob)
    assert main_log.state_at(10).wg_access(bob, W2) is None  # admin has no implicit workgroup access
    assert main_log.state_at(12).can_write_wg(bob, W1)
    assert main_log.state_at(17).wg_access(bob, W1) is None
    assert main_log.state_at(5).device_wgs(device) == [W1]
    assert main_log.state_at(22).wg_access(pid("alice"), W2) == "write"  # creating admin
    assert main_log.state_at(24).pending == frozenset({(ORG, ORG_ID), (WG, W0)})
    assert main_log.state_at(25).pending == frozenset({(WG, W0)})
    assert main_log.state.pending == frozenset()
    assert main_log.state_at(13).principals[pid("pdev")].platform_seq == PDEV_SEQ
    with pytest.raises(CryptoError, match="log_fork"):
        main_log.state_at(27)


def test_epoch_holders(main_log: verify.VerifiedLog) -> None:
    holders = main_log.epoch_holders(WG, W1, 1)
    assert pid("bob") in holders
    assert pid("pdev") in holders
    assert pid("carol") not in holders
    assert pid("bob") not in main_log.epoch_holders(WG, W1, 2)
    assert main_log.epoch_holders(WG, W1, 9) == frozenset()


def _verify(entries: list[dict[str, object]], state: platform.PlatformState | None = None) -> verify.VerifiedLog:
    return verify.verify_log(entries, ALICE, platform=state)


def _refused(code: str, entries: list[dict[str, object]], state: platform.PlatformState | None = None) -> None:
    with pytest.raises(CryptoError) as refused:
        _verify(entries, state)
    assert refused.value.code == code


def test_platform_device_needs_the_platform_state(main: LogBuilder) -> None:
    _refused("log_rights", main.entries[:14])


def test_platform_device_assigned_to_a_second_workgroup(main: LogBuilder, plat_state: platform.PlatformState) -> None:
    log = LogBuilder(entries=main.entries[:14])
    log.add("device_assign", "alice", device=pid("pdev"), wg=W0, platform_seq=PDEV_SEQ)
    assert _verify(log.entries, plat_state).state.device_wgs(pid("pdev")) == sorted([W0, W1])
    other = LogBuilder(entries=main.entries[:14])
    other.add("device_assign", "alice", device=pid("pdev"), wg=W0, platform_seq=QDEV_SEQ)
    _refused("log_rights", other.entries, plat_state)


def test_org_device_cannot_be_assigned_as_platform_device(main: LogBuilder, plat_state: platform.PlatformState) -> None:
    log = LogBuilder(entries=main.entries[:6])
    log.add("device_assign", "alice", device=pid("device"), wg=W0, platform_seq=PDEV_SEQ)
    _refused("log_rights", log.entries, plat_state)
    platform_device = LogBuilder(entries=main.entries[:14])
    platform_device.add("device_assign", "alice", device=pid("pdev"), wg=W0)
    _refused("log_rights", platform_device.entries, plat_state)


def test_hub_append_of_a_certified_platform_device(main: LogBuilder, plat_state: platform.PlatformState) -> None:
    base = _verify(main.entries[:13], plat_state)
    now = START + timedelta(hours=1)
    after = verify.append_entries(
        base, [main.entries[13]], founder="", platform=plat_state, now=now, platform_access=True
    )
    assert after.state.assigned(pid("pdev"), W1)
    with pytest.raises(CryptoError, match="log_rights"):  # fail-closed without platform access (ADR-026)
        verify.append_entries(base, [main.entries[13]], founder="", platform=plat_state, now=now)


def test_platform_device_removed_and_assigned_again(main: LogBuilder, plat_state: platform.PlatformState) -> None:
    log = main.copy()
    log.add("remove", "alice", subject=pid("pdev"))
    log.add("device_assign", "alice", device=pid("pdev"), wg=W1, platform_seq=PDEV_SEQ)
    state = _verify(log.entries, plat_state).state
    assert state.principals[pid("pdev")].joined_seq == 28
    assert state.assigned(pid("pdev"), W1)


def test_device_invitation_may_name_the_default_workgroup() -> None:
    log = LogBuilder()
    log.genesis()
    log.invite_device("alice", "device", [W0])
    log.accept("device", "device")
    assert _verify(log.entries).state.device_wgs(pid("device")) == [W0]


def test_rejoin_after_leaving_starts_from_the_new_invitation() -> None:
    log = LogBuilder()
    log.genesis()
    log.add("wg_create", "alice", wg=W1, epoch=1)
    log.invite_user("alice", "bob", wgs={W1: "write"})
    log.accept("bob")
    log.add("remove", "bob", subject=pid("bob"))
    log.invite_user("alice", "erin", role="admin")
    log.accept("bob", code_label="erin")
    state = _verify(log.entries).state
    bob = state.principals[pid("bob")]
    assert bob.active
    assert bob.joined_seq == 6
    assert bob.role == "admin"
    assert state.wg_access(pid("bob"), W1) is None


def test_former_user_cannot_rejoin_as_a_device() -> None:
    log = LogBuilder()
    log.genesis()
    log.invite_user("alice", "bob")
    log.accept("bob")
    log.add("remove", "alice", subject=pid("bob"))
    log.invite_device("alice", "erin")
    log.accept("bob", "device", code_label="erin")
    _refused("log_accept", log.entries)


def test_accept_by_other_author(main: LogBuilder) -> None:
    log = LogBuilder(entries=main.entries[:3])
    found = secrets("bob")
    body = log.body(
        "accept", "alice", invite_id=found.invite_id, subject=pid("bob"), kind="user", pub=identity("bob").public()
    )
    log.entries.append(sign_log_entry(body, identity("bob").sig, found.signing))
    _refused("log_accept", log.entries)


def test_invite_expiry_only_in_append_mode(main: LogBuilder) -> None:
    base = _verify(main.entries[:3])
    before = START + timedelta(days=2)
    assert verify.append_entries(base, [main.entries[3]], founder="", now=before).state.member(pid("bob"))
    with pytest.raises(CryptoError, match="invite_expired"):
        verify.append_entries(base, [main.entries[3]], founder="", now=parse_time(EXPIRES))


def test_append_a_new_org_and_an_empty_append(main: LogBuilder) -> None:
    created = verify.append_entries(None, main.entries[:2], founder=ALICE)
    assert created.head.seq == 1
    assert verify.append_entries(created, [], founder="").head == created.head
    with pytest.raises(CryptoError, match="log_empty"):
        verify.append_entries(None, [], founder=ALICE)
    with pytest.raises(CryptoError, match="seq_conflict"):
        verify.append_entries(None, main.entries[1:2], founder=ALICE)
    with pytest.raises(CryptoError, match="log_genesis"):
        verify.append_entries(None, main.entries[:1], founder=sig_pk("bob"))


def test_needs_refresh_and_require_lh(main_log: verify.VerifiedLog) -> None:
    assert not verify.needs_refresh(main_log, 26)
    assert verify.needs_refresh(main_log, 27)
    verify.require_lh(main_log, 0)
    with pytest.raises(CryptoError, match="log_fork"):
        verify.require_lh(main_log, 99)


def test_role_of_a_device_cannot_change(main: LogBuilder) -> None:
    log = LogBuilder(entries=main.entries[:6])
    log.add("role", "alice", subject=pid("device"), role="admin")
    _refused("log_rights", log.entries)


def test_ungrant_without_grant_and_wg_create_twice(main: LogBuilder) -> None:
    log = LogBuilder(entries=main.entries[:6])
    log.add("ungrant", "alice", subject=pid("device"), wg=W1)  # a device has no grant
    _refused("log_rights", log.entries)
    removed = LogBuilder(entries=main.entries[:20])
    removed.add("ungrant", "alice", subject=pid("carol"), wg=W1)
    _refused("log_rights", removed.entries)
    twice = LogBuilder(entries=main.entries[:6])
    twice.add("wg_create", "alice", wg=W1, epoch=1)
    _refused("log_rights", twice.entries)
    org_id = LogBuilder(entries=main.entries[:6])
    org_id.add("wg_create", "alice", wg=ORG_ID, epoch=1)
    _refused("log_rights", org_id.entries)


def test_rotate_rights(main: LogBuilder) -> None:
    writer = LogBuilder(entries=main.entries[:13])
    writer.add("rotate", "bob", scope="wg", scope_id=W1, epoch=2)  # bob has write on W1 since seq 12
    assert _verify(writer.entries[:12] + writer.entries[12:]).state.epoch(WG, W1) == 2
    other_org = LogBuilder(entries=main.entries[:4])
    other_org.add("rotate", "alice", scope="org", scope_id=W0, epoch=2)
    _refused("log_rights", other_org.entries)
    unknown = LogBuilder(entries=main.entries[:4])
    unknown.add("rotate", "alice", scope="wg", scope_id=W2, epoch=2)
    _refused("log_rights", unknown.entries)
    admin = LogBuilder(entries=main.entries[:4])
    admin.add("wg_create", "alice", wg=W2, epoch=1)
    admin.add("role", "alice", subject=pid("bob"), role="admin")
    admin.add("rotate", "bob", scope="wg", scope_id=W2, epoch=2)  # admin without access may rotate
    assert _verify(admin.entries).state.epoch_authors[(WG, W2, 2)] == pid("bob")


# --- Spike D: malicious hub (tests/test_keyflow.py of spikes/passkey-crypto as template) ---


def test_malicious_hub_substitutes_invitee_key(main: LogBuilder) -> None:
    log = LogBuilder(entries=main.entries[:3])
    found = secrets("bob")
    body = log.body(
        "accept", "bob", invite_id=found.invite_id, subject=pid("bob"), kind="user", pub=identity("hub").public()
    )
    log.entries.append(sign_log_entry(body, identity("hub").sig, identity("hub").sig))
    _refused("log_accept", log.entries)


def test_malicious_hub_injects_member_or_invite(main: LogBuilder) -> None:
    invite = LogBuilder(entries=main.entries[:4])
    invite.add(
        "invite",
        "alice",
        signer="hub",
        invite_id=secrets("erin").invite_id,
        pk_inv=secrets("erin").pk_inv,
        kind="user",
        role="admin",
        wgs={},
        expires=EXPIRES,
    )
    _refused("log_signature", invite.entries)
    member = LogBuilder(entries=main.entries[:4])
    member.add("grant", "bob", subject=pid("bob"), wg=W1, access="write")  # bob signs, but is no admin
    _refused("log_rights", member.entries)
    hub_member = LogBuilder(entries=main.entries[:4])
    hub_member.accept("erin")  # no invitation for this code in the log
    _refused("invite_not_in_log", hub_member.entries)


def test_malicious_hub_breaks_the_chain_or_rolls_back(main: LogBuilder) -> None:
    reordered = [*main.entries[:3], main.entries[4], main.entries[3]]
    _refused("log_chain", reordered)
    dropped = [*main.entries[:3], *main.entries[4:6]]
    _refused("log_chain", dropped)
    verified = _verify(main.entries[:10])
    with pytest.raises(CryptoError, match="log_rollback"):
        check_extends(verified.head, main.entries[:9])


def test_malicious_hub_swaps_wraps(main_log: verify.VerifiedLog) -> None:
    for_device = make_wrap("alice", "wg", W1, 2, "device")
    swapped = {**for_device, "recipient_id": pid("pdev")}  # the wrap re-addressed to another recipient
    with pytest.raises(CryptoError, match="signature_invalid"):
        check_wrap(swapped, pid("alice"), main_log)
    with pytest.raises(CryptoError, match="decrypt_failed"):
        wrap.unwrap_key(
            identity("pdev").kem, scope="wg", scope_id=W1, epoch=2, recipient=pid("pdev"), wrap=for_device["wrap"]
        )


# --- Property: incremental state = rebuilt state ---


class RandomLog:
    """Builds a random valid log: every step picks an entry type and valid arguments from the current state; removals
    and ungrants are followed by their rotate set R."""

    def __init__(self, seed: int) -> None:
        self.rnd = random.Random(seed)
        self.seed = seed
        self.log = LogBuilder()
        self.log.genesis()
        self.users: list[str] = ["alice"]
        self.devices: list[str] = []
        self.wgs: list[str] = [W0]
        self.verified = _verify(self.log.entries)

    @property
    def state(self) -> OrgState:
        return self.verified.state

    def _sync(self) -> None:
        self.verified = verify.append_entries(self.verified, self.log.entries[len(self.verified.entries) :], founder="")

    def _active(self, labels: list[str]) -> list[str]:
        return [label for label in labels if self.state.member(pid(label)) is not None]

    def _open_wgs(self, default: bool = True) -> list[str]:
        return [wg for wg in self.wgs if self.state.writable(wg) and (default or wg != W0)]

    def _rotate_lost(self, admin: str) -> None:
        self._sync()
        lost = required_set(self.verified.states[-2], self.state, entry_subject(self.log.entries[-1]["body"]))
        for scope, scope_id in sorted(lost):
            self.log.add("rotate", admin, scope=scope, scope_id=scope_id, epoch=self.state.epoch(scope, scope_id) + 1)
            self._sync()

    def invite_user(self, admin: str, label: str) -> None:
        wgs = {wg: self.rnd.choice(["read", "write"]) for wg in self._open_wgs(False) if self.rnd.random() < 0.5}
        self.log.invite_user(admin, label, role=self.rnd.choice(["admin", "member"]), wgs=wgs)
        self.log.accept(label)
        self.users.append(label)

    def invite_device(self, admin: str, label: str) -> None:
        self.log.invite_device(admin, label, [wg for wg in self._open_wgs() if self.rnd.random() < 0.5])
        self.log.accept(label, "device")
        self.devices.append(label)

    def wg_create(self, admin: str, label: str) -> None:
        wg = pid("wg/" + label)
        self.log.add("wg_create", admin, wg=wg, epoch=1)
        self.wgs.append(wg)

    def grant(self, admin: str, _label: str) -> None:
        users, wgs = self._active(self.users), self._open_wgs(False)
        if wgs:
            user, wg = pid(self.rnd.choice(users)), self.rnd.choice(wgs)
            access = "write" if self.state.access.get((user, wg)) == "read" else "read"
            self.log.add("grant", admin, subject=user, wg=wg, access=access)

    def ungrant(self, admin: str, _label: str) -> None:
        granted = sorted(self.state.access)
        if granted:
            user, wg = self.rnd.choice(granted)
            self.log.add("ungrant", admin, subject=user, wg=wg)
            self._rotate_lost(admin)

    def assignment(self, admin: str, _label: str) -> None:
        devices = self._active(self.devices)
        if devices:
            device, wg = pid(self.rnd.choice(devices)), self.rnd.choice(self._open_wgs())
            kind = "device_unassign" if self.state.assigned(device, wg) else "device_assign"
            self.log.add(kind, admin, device=device, wg=wg)
            if kind == "device_unassign":
                self._rotate_lost(admin)

    def remove(self, admin: str, _label: str) -> None:
        subject = self.rnd.choice([*self._active(self.users), *self._active(self.devices)])
        if self.state.is_admin(pid(subject)) and len(self.state.admins()) == 1:
            return
        leave = subject == admin or (subject in self.users and self.rnd.random() < 0.3)
        self.log.add("remove", subject if leave else admin, subject=pid(subject))
        if not leave:
            self._rotate_lost(admin)

    def role(self, admin: str, _label: str) -> None:
        others = [user for user in self._active(self.users) if user != admin]
        if others:
            user = pid(self.rnd.choice(others))
            self.log.add("role", admin, subject=user, role="member" if self.state.is_admin(user) else "admin")

    def archive(self, admin: str, _label: str) -> None:
        wgs = self._open_wgs(False)
        if wgs:
            self.log.add("wg_archive", admin, wg=self.rnd.choice(wgs))

    def rotate(self, admin: str, _label: str) -> None:
        scope, scope_id = self.rnd.choice(self.state.scopes())
        self.log.add("rotate", admin, scope=scope, scope_id=scope_id, epoch=self.state.epoch(scope, scope_id) + 1)

    def build(self, steps: int = 60) -> LogBuilder:
        actions = [
            self.invite_user,
            self.invite_device,
            self.wg_create,
            self.grant,
            self.ungrant,
            self.assignment,
            self.remove,
            self.role,
            self.archive,
            self.rotate,
        ]
        for step in range(steps):
            admin = self.rnd.choice(
                [pid_label for pid_label in self._active(self.users) if self.state.is_admin(pid(pid_label))]
            )
            self.rnd.choice(actions)(admin, f"p{self.seed}-{step}")
            self._sync()
        return self.log


def _random_log(seed: int) -> LogBuilder:
    return RandomLog(seed).build()


@pytest.mark.parametrize("seed", range(12))
def test_incremental_state_equals_rebuilt_state(seed: int) -> None:
    log = _random_log(seed)
    whole = _verify(log.entries)
    incremental: verify.VerifiedLog | None = None
    rnd = random.Random(1000 + seed)
    index = 0
    while index < len(log.entries):
        size = rnd.randint(1, 4)
        incremental = verify.append_entries(incremental, log.entries[index : index + size], founder=ALICE)
        index += size
    assert incremental is not None
    assert incremental.states == whole.states
    assert [s.projection() for s in incremental.states] == [s.projection() for s in whole.states]
    state = None
    for entry in log.entries:
        state = verify.apply(state, entry, founder=ALICE)
        assert state == whole.states[state.seq]


def test_random_logs_cover_every_entry_type() -> None:
    kinds = {e["body"]["type"] for seed in range(12) for e in _random_log(seed).entries}
    assert kinds >= {"genesis", "invite", "accept", "role", "wg_create", "wg_archive", "grant", "ungrant"}
    assert kinds >= {"device_assign", "device_unassign", "remove", "rotate"}


def test_main_builder_is_stable() -> None:
    assert main_builder().entries == main_builder().entries


def test_rejoining_device_may_bring_new_keys() -> None:
    log = LogBuilder()
    log.genesis()
    log.invite_device("alice", "device")
    log.accept("device", "device")
    log.add("remove", "alice", subject=pid("device"))
    log.invite_device("alice", "erin")
    log.accept("device", "device", code_label="erin", key_label="dev2")
    assert _verify(log.entries).state.principals[pid("device")].sig == sig_pk("dev2")


def test_rejoining_user_keeps_the_identity() -> None:
    log = LogBuilder()
    log.genesis()
    log.invite_user("alice", "bob")
    log.accept("bob")
    log.add("remove", "alice", subject=pid("bob"))
    log.invite_user("alice", "erin")
    log.accept("bob", code_label="erin", key_label="hub")
    _refused("log_accept", log.entries)
