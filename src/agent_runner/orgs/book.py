"""Verified copies of the platform log and the membership logs, and the rights state of this device.

05-runner.md sections 2.2 and 2.4, 04-crypto.md sections 4.7, 4.8, 6.1, 6.2, 02-data-model.md section 4
(`log_entry`, `log_head`, `platform_log_entry`), ADR-023.

`OrgBook` is the runner's single view of what the logs entitle it to; later packages (sessions, prompts,
assignments, operations) use it as a library and never read the log tables themselves.

- The platform log is verified against `AGENT_RUNNER_PLATFORM_ROOT_KEY` (P1-P4). The device acts only while the
  verified platform log certifies **its own** keys (`device_certify` with this device ID and exactly these public
  keys) and no `device_decertify` has ended that certification (`certified`).
- A membership log is verified with L1-L8 (`crypto.log.verify.apply`). A platform device has no invitation package,
  so the founder key is pinned on first use: the genesis key of the first verified copy, which is accepted only if
  that copy assigns this device with its own certification (`device_assign` with `platform_seq`); afterwards L9
  (the stored head must be extended) protects the copy (decisions/RUN-03.md).
- An org is *acting* while its copy is verified, it is not stopped, the platform log is not stopped, the device is
  certified and is an active member of the org through its current certification. Only acting orgs are pulled,
  receive keys and are acted upon.
- Org devices (`kind="org"`, RUN-06, decisions/RUN-06.md) have no certification: the founder key is pinned by the
  invitation package at enrollment (`enroll.org` stores the verified copy), an org is acting while its copy is
  verified, neither it nor the platform copy is stopped and the device is an active member with exactly its own keys
  (its `accept`). A copy of an org the device never enrolled in is never started from the hub's answer.
- A rollback or fork stops the org (`stop`), persisted as `sync_state` key `org_stopped_<org hex>` (the platform
  log: `platform_stopped`), so it is alerted once and stays stopped across restarts until an operator clears it.

Entries are stored as canonical JSON text with their hash; on `load` every stored copy is verified again, so the
in-memory state is always derived from verified entries (a copy that no longer verifies, e.g. after a change of the
root key, fails the load: fail closed).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from psycopg import AsyncConnection

from agent_runner.crypto.encoding import canonical, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log.entries import Head
from agent_runner.crypto.log.platform import (
    Certification,
    PlatformState,
    VerifiedPlatformLog,
    apply_platform,
    verify_platform_log,
)
from agent_runner.crypto.log.state import ORG, WG, OrgState, Principal, Roster
from agent_runner.crypto.log.verify import VerifiedLog, apply, verify_log
from agent_runner.sync import outbox

ATTRIBUTE = "org_book"
STOP_PREFIX = "org_stopped_"
PLATFORM_STOP = "platform_stopped"
NOT_ASSIGNED = "device_not_assigned"
MAX_REFUSED = 10_000


@dataclass(frozen=True)
class Outcome:
    """Result of extending a copy: entries added, the code of the first refused entry, and an alert code
    (`log_fork`) when the refused entry continues the verified head with another history."""

    added: int
    error: str | None = None
    alert: str | None = None


def _body(entry: object) -> Mapping[str, object]:
    body = entry.get("body") if isinstance(entry, dict) else None
    return cast(Mapping[str, object], body) if isinstance(body, dict) else {}


def _founder(entry: object) -> str:
    """The genesis signing key named by the first entry ("" when it is not a genesis; L3 then refuses it)."""
    pub = _body(entry).get("pub")
    sig = pub.get("sig") if isinstance(pub, dict) else None
    return sig if isinstance(sig, str) else ""


def _forks(entry: object, head: Head) -> bool:
    """The refused entry is the successor of `head` (same `seq`) but names another predecessor: a second history."""
    body = _body(entry)
    seq = body.get("seq")
    return isinstance(seq, int) and not isinstance(seq, bool) and seq == head.seq + 1 and body.get("prev") != head.hash


def _text(entry: Mapping[str, object]) -> str:
    return canonical(dict(entry)).decode("ascii")


def stop_key(org: str) -> str:
    """`sync_state` key of a stopped org."""
    return STOP_PREFIX + uuid.UUID(org).hex


class OrgBook:
    """The verified logs of this device (see the module docstring)."""

    def __init__(self, *, device_id: str, kem_public: str, sig_public: str, root: str, kind: str = "platform") -> None:
        self.device_id = device_id
        self.kind = kind
        self.kem_public = kem_public
        self.sig_public = sig_public
        self.root = root
        self.platform: VerifiedPlatformLog | None = None
        self.logs: dict[str, VerifiedLog] = {}
        self.stopped: dict[str, str] = {}
        self.platform_stopped: str | None = None
        self.loaded = False
        self.refused: set[tuple[object, ...]] = set()
        """Keys of refused inbound items already audited (a hub repeating them is audited once per daemon)."""
        self.lock = asyncio.Lock()
        """Held by whoever refreshes a copy (reconcile step, inbox L10 refresh), so two never interleave."""

    # --- loading -----------------------------------------------------------------------------------------------

    async def ensure_loaded(self, conn: AsyncConnection[Any]) -> None:
        """Load and verify the stored copies once per daemon."""
        if self.loaded:
            return
        # everything is verified into locals first: a copy that fails leaves the book unloaded, never half filled
        cur = await conn.execute("SELECT entry_text FROM runner.platform_log_entry ORDER BY seq")
        platform_entries = [parse_json(str(row[0]).encode("ascii")) for row in await cur.fetchall()]
        verified_platform = verify_platform_log(platform_entries, self.root) if platform_entries else None
        platform = verified_platform.state if verified_platform is not None else None
        cur = await conn.execute("SELECT org_id::text, entry_text FROM runner.log_entry ORDER BY org_id, seq")
        grouped: dict[str, list[object]] = {}
        for org, text in await cur.fetchall():
            grouped.setdefault(str(org), []).append(parse_json(str(text).encode("ascii")))
        logs = {org: verify_log(entries, _founder(entries[0]), platform=platform) for org, entries in grouped.items()}
        cur = await conn.execute("SELECT key, value FROM runner.sync_state WHERE key LIKE 'org\\_stopped\\_%'")
        stopped = {str(uuid.UUID(str(key)[len(STOP_PREFIX) :])): str(value) for key, value in await cur.fetchall()}
        self.platform_stopped = await outbox.get_state(conn, PLATFORM_STOP)
        self.platform, self.logs, self.stopped = verified_platform, logs, stopped
        self.loaded = True

    # --- certification -----------------------------------------------------------------------------------------

    @property
    def platform_state(self) -> PlatformState | None:
        return None if self.platform is None else self.platform.state

    def certification(self) -> Certification | None:
        """The latest certification of this device ID in the verified platform log, if it certifies these keys."""
        state = self.platform_state
        seq = None if state is None else state.devices.get(self.device_id)
        if state is None or seq is None:
            return None
        found = state.certifications[seq]
        return found if (found.kem, found.sig) == (self.kem_public, self.sig_public) else None

    @property
    def certified(self) -> bool:
        """The verified platform log certifies this device now and the platform copy is not stopped."""
        found = self.certification()
        return found is not None and found.decertified_seq is None and self.platform_stopped is None

    @property
    def decertified(self) -> bool:
        found = self.certification()
        return found is not None and found.decertified_seq is not None

    @property
    def org_device(self) -> bool:
        return self.kind == "org"

    def status(self) -> str:
        """`certified`, `decertified`, `stopped`, `uncertified` (not, or not with these keys, in the log); org devices:
        `stopped` or `enrolled`."""
        if self.platform_stopped is not None:
            return "stopped"
        if self.org_device:
            return "enrolled"
        if self.decertified:
            return "decertified"
        return "certified" if self.certified else "uncertified"

    # --- orgs and rights ---------------------------------------------------------------------------------------

    def head(self, org: str) -> Head | None:
        log = self.logs.get(org)
        return None if log is None else log.head

    @property
    def platform_head(self) -> Head | None:
        return None if self.platform is None else self.platform.state.head_value()

    def member(self, org: str) -> Principal | None:
        """This device as an active member of an acting org (None otherwise)."""
        log = self.logs.get(org)
        if self.org_device:
            if log is None or org in self.stopped or self.platform_stopped is not None:
                return None
            principal = log.state.member(self.device_id)
            if principal is None or principal.platform_seq is not None:
                return None
            return principal if (principal.kem, principal.sig) == (self.kem_public, self.sig_public) else None
        found = self.certification()
        if log is None or found is None or not self.certified or org in self.stopped:
            return None
        principal = log.state.member(self.device_id)
        return principal if principal is not None and principal.platform_seq == found.seq else None

    def acting(self, org: str) -> bool:
        return self.member(org) is not None

    def acting_orgs(self) -> list[str]:
        return sorted(org for org in self.logs if self.acting(org))

    def log(self, org: str) -> VerifiedLog | None:
        """The verified copy of an acting org."""
        return self.logs[org] if self.acting(org) else None

    def excluded(self, org: str) -> frozenset[str]:
        """Platform devices of the org whose certification has ended (no authors, readers or recipients)."""
        state = self.platform_state
        log = self.logs.get(org)
        if state is None or log is None:
            return frozenset()
        return state.excluded(log.state)

    def support_authors(self) -> Roster:
        """The support persons of the verified platform log, also former ones (ADR-096: authors of support events)."""
        state = self.platform_state
        return {} if state is None else state.support_authors()

    def support_roster(self) -> Roster:
        """The current support persons (ADR-096: recipients of the released workgroups' epochs in catch-up)."""
        state = self.platform_state
        return {} if state is None else state.support_roster()

    def author_sig(self, org: str, lh: int, author: str) -> str:
        """The signing key of an event author at `lh`: a principal of the org log, else a support person."""
        found = self.logs[org].states[lh].principals.get(author)
        return found.sig if found is not None else self.support_authors()[author].sig

    def assignments(self) -> list[tuple[str, str]]:
        """`(org, wg)` of every workgroup this device is assigned to in an acting org, sorted."""
        return [(org, wg) for org in self.acting_orgs() for wg in self.logs[org].state.device_wgs(self.device_id)]

    def org_of_wg(self, wg: str) -> str | None:
        """The acting org that has the workgroup `wg`."""
        for org in self.acting_orgs():
            if wg in self.logs[org].state.workgroups:
                return org
        return None

    def pull_scopes(self) -> dict[str, tuple[str, str]]:
        """`scope_id -> (org, scope)` of the scopes this device pulls: its workgroups and their org scopes."""
        scopes: dict[str, tuple[str, str]] = {}
        for org, wg in self.assignments():
            scopes[wg] = (org, WG)
            scopes[org] = (org, ORG)
        return scopes

    def state_at(self, org: str, lh: int) -> OrgState | None:
        """The rights state of an acting org after entry `lh` (None beyond the verified head)."""
        log = self.log(org)
        if log is None or lh > log.head.seq:
            return None
        return log.states[lh]

    def author_may_write(self, org: str, wg: str, author: str, lh: int) -> bool:
        """`wg:write` of a user author at the log head `lh` its event names (E5, prompts and settings)."""
        state = self.state_at(org, lh)
        return state is not None and author not in self.excluded(org) and state.can_write_wg(author, wg)

    def assigned_at(self, org: str, wg: str, device: str, lh: int) -> bool:
        """A device assigned to the workgroup at `lh` (device-authored kinds)."""
        state = self.state_at(org, lh)
        return state is not None and device not in self.excluded(org) and state.assigned(device, wg)

    # --- extending the copies ----------------------------------------------------------------------------------

    async def extend_platform(self, conn: AsyncConnection[Any], entries: Sequence[object]) -> Outcome:
        """Verify `entries` as the continuation of the platform copy and store the verified prefix."""
        current = self.platform
        state = None if current is None else current.state
        verified: list[dict[str, object]] = []
        states: list[PlatformState] = []
        outcome = Outcome(0)
        for entry in entries:
            try:
                state = apply_platform(state, entry, self.root)
            except CryptoError as refused:
                head = None if current is None or verified else current.state.head_value()
                outcome = Outcome(0, refused.code, "log_fork" if head is not None and _forks(entry, head) else None)
                break
            verified.append(cast(dict[str, object], entry))
            states.append(state)
        if verified:
            async with conn.transaction():
                for added, after in zip(verified, states, strict=True):
                    await conn.execute(
                        "INSERT INTO runner.platform_log_entry (seq, entry_text, entry_hash) VALUES (%s, %s, %s)",
                        (after.seq, _text(added), after.head),
                    )
            self.platform = VerifiedPlatformLog(
                (*(() if current is None else current.entries), *verified),
                (*(() if current is None else current.states), *states),
            )
        return Outcome(len(verified), outcome.error, outcome.alert)

    def _relevant(self, state: OrgState) -> bool:
        """First use of a platform copy (ADR-040): the org's log made this device a member through its own
        certification, and the verified platform log gives the org platform access (ADR-026)."""
        found = self.certification()
        platform = self.platform_state
        principal = state.principals.get(self.device_id)
        return (
            found is not None
            and platform is not None
            and principal is not None
            and principal.platform_seq == found.seq
            and platform.platform_access(state.org, state.private)
        )

    def _clashes(self, org: str, state: OrgState) -> bool:
        """IDs are chosen by clients, so a second (possibly invented) org must never claim a workgroup, the org ID or
        a principal ID with other keys of another verified copy (ADR-040)."""
        for other_org, other in self.logs.items():
            if other_org == org:
                continue
            theirs = other.state
            ids = {other_org, *theirs.workgroups}
            if org in theirs.workgroups or ids & set(state.workgroups):
                return True
            for principal in state.principals.values():
                known = theirs.principals.get(principal.id)
                if known is not None and (known.kem, known.sig, known.kind) != (
                    principal.kem,
                    principal.sig,
                    principal.kind,
                ):
                    return True
        return False

    def remember_refusal(self, key: tuple[object, ...]) -> bool:
        """True when `key` is new (audit it), False when it was audited already; the memory is bounded."""
        if key in self.refused:
            return False
        if len(self.refused) >= MAX_REFUSED:
            self.refused.clear()
        self.refused.add(key)
        return True

    async def extend_org(self, conn: AsyncConnection[Any], org: str, entries: Sequence[object]) -> Outcome:
        """Verify `entries` as the continuation of the org's copy (from `seq` 0 for a new org) and store the verified
        prefix. A new org is stored only when its verified entries assign this device (`device_not_assigned`)."""
        current = self.logs.get(org)
        founder = current.state.founder if current is not None else _founder(entries[0] if entries else None)
        platform = self.platform_state
        state = None if current is None else current.state
        verified: list[dict[str, object]] = []
        states: list[OrgState] = []
        outcome = Outcome(0)
        for entry in entries:
            try:
                state = apply(state, entry, founder=founder, platform=platform)
                if state.org != org or self._clashes(org, state):
                    outcome = Outcome(0, "log_chain" if state.org != org else "log_rights")
                    break
            except CryptoError as refused:
                head = None if current is None or verified else current.head
                outcome = Outcome(0, refused.code, "log_fork" if head is not None and _forks(entry, head) else None)
                break
            verified.append(cast(dict[str, object], entry))
            states.append(state)
        if current is None and (self.org_device or not states or not self._relevant(states[-1])):
            return Outcome(0, outcome.error or NOT_ASSIGNED)
        if verified:
            async with conn.transaction():
                for added, after in zip(verified, states, strict=True):
                    await conn.execute(
                        "INSERT INTO runner.log_entry (org_id, seq, entry_text, entry_hash) VALUES (%s, %s, %s, %s)",
                        (org, after.seq, _text(added), after.head),
                    )
                last = states[-1]
                await conn.execute(
                    "INSERT INTO runner.log_head (org_id, seq, hash) VALUES (%s, %s, %s) "
                    "ON CONFLICT (org_id) DO UPDATE SET seq = EXCLUDED.seq, hash = EXCLUDED.hash",
                    (org, last.seq, last.head),
                )
            self.logs[org] = VerifiedLog(
                (*(() if current is None else current.entries), *verified),
                (*(() if current is None else current.states), *states),
            )
        return Outcome(len(verified), outcome.error, outcome.alert)

    async def stop(self, conn: AsyncConnection[Any], org: str | None, code: str) -> None:
        """Stop acting for one org (`org`) or for every org (`None`: the platform copy), in the caller's transaction."""
        if org is None:
            await outbox.set_state(conn, PLATFORM_STOP, code)
            self.platform_stopped = code
        else:
            await outbox.set_state(conn, stop_key(org), code)
            self.stopped[org] = code


def book_for(context: Any) -> OrgBook:
    """The book of this daemon (built on first use from the identity and the configured root key)."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, OrgBook):
        return found
    identity = context.identity
    book = OrgBook(
        device_id=identity.device.device_id,
        kem_public=identity.keys.kem_public,
        sig_public=identity.keys.sig_public,
        root=context.config.platform_root_key,
        kind=identity.device.kind,
    )
    setattr(context, ATTRIBUTE, book)
    return book
