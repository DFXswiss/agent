"""Read scope of a session at call time (ADR-089, 03-api.md section 8.1 "Read scope", 04-crypto.md section 4.3).

A session reads what both its **task owner** and the **instructing human** of the read's turn may read, evaluated
against the verified membership log at the moment of the call (`OrgBook`, never a cached answer of the hub). The
instructing human is captured when the read arrives (`credentials.acting.stamp` in the `platform.query` journal
transaction, `QueryCall.actor_id`) and never re-read while the read is answered; None when the turn has instructions
of several people or none.

- organization level (workgroup names, members of the org, org whiteboard): the owner is an active user of the org;
- the session's own workgroup (tasks, chat messages, workgroup and task whiteboards, operations, workgroup members):
  additionally the owner has explicit `read` or `write` access to it (`OrgState.explicit_access`; a session is never
  started for an owner who cannot write there, and every instructing human has `write` there by the prompt check);
- another workgroup W (`decide`): the instructing human is known (else `forbidden` for every other W alike, so nothing
  is revealed), W exists in the log (archived allowed) and the owner and the instructing human both have effective
  access to it (`OrgState.wg_access`: explicit access or the inherited read of an admin, ADR-058) - else `not_found`,
  the same answer whether W does not exist or the people may not read it; and this device holds W's key because it is
  assigned to W, directly or through the organization (else `unavailable` naming W and the admin's remedy, E-05).
  Such a workgroup is read with `read` access.

An owner who was removed from the org is `forbidden` for every read; one who lost the own workgroup access keeps the
organization level and is `forbidden` on the workgroup level. An instructing human who is no longer an active member
with access to the session's workgroup makes every read `forbidden`. A device that cannot act for the org right now
(copy not verified, org stopped, decertified) or is no longer assigned to the session's workgroup answers
`unavailable`. Credentials are no workgroup and stay out of reach (ADR-058 section 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from agent_runner.crypto.log.state import READ, USER, OrgState
from agent_runner.orgs.book import book_for
from agent_runner.sessions.records import LocalSession


class ReadRefused(Exception):
    """A read that is not answered with content: tool error code, English sentence and the workgroup it concerned
    (`wg_id`, audited as `read_wg_id`; None on the organization level or before a workgroup was chosen)."""

    def __init__(self, error: str, message: str, *, wg_id: str | None = None) -> None:
        super().__init__(error)
        self.error = error
        self.message = message
        self.wg_id = wg_id

    def at(self, wg_id: str) -> ReadRefused:
        """The same refusal for the workgroup `wg_id`."""
        return ReadRefused(self.error, self.message, wg_id=wg_id)


class NotServed(ReadRefused):
    """Readable by the people but not by this device: it is not assigned to the workgroup (ADR-089 section 2). The
    sentence names the workgroup by `name`, by its ID while the name is not known."""

    def __init__(self, wg_id: str, name: str | None = None) -> None:
        super().__init__(
            "unavailable",
            f"This execution device does not serve workgroup {name or wg_id}; an admin of the organization can assign"
            " the device to that workgroup or to the whole organization.",
            wg_id=wg_id,
        )


NOT_MEMBER = ReadRefused("forbidden", "The task owner is no longer a member of this organization.")
NO_ACCESS = ReadRefused("forbidden", "The task owner has no access to this workgroup anymore.")
NOT_ACTING = ReadRefused("unavailable", "This device cannot read this organization right now. Try again later.")
HUMAN_GONE = ReadRefused(
    "forbidden", "The person whose instruction this session works on is no longer a member of this workgroup."
)
UNKNOWN_HUMAN = ReadRefused(
    "forbidden",
    "This turn has instructions of several people (or none), so only this workgroup can be read. Ask one person to"
    " give the instruction.",
)
NO_WORKGROUP = ReadRefused("not_found", "There is no such workgroup you can read.")
REACH_SENTENCE = (
    "This session's rights profile reads only its own {reach}; ask the session that started you or a person."
)


def decide(state: OrgState, *, device_id: str, own_wg: str, owner_id: str, actor_id: str | None, wg_id: str) -> str:
    """The session's access (`read`/`write`) to workgroup `wg_id`, else `ReadRefused` (module docstring). The caller
    checked that the owner is an active user and the instructing human still a member (`read_scope`)."""
    if wg_id == own_wg:
        access = state.explicit_access(owner_id, own_wg)
        if access is None:
            raise NO_ACCESS.at(wg_id)
        return access
    if actor_id is None:
        raise UNKNOWN_HUMAN.at(wg_id)
    if state.wg_access(owner_id, wg_id) is None or state.wg_access(actor_id, wg_id) is None:
        raise NO_WORKGROUP.at(wg_id)
    if not state.assigned(device_id, wg_id):
        raise NotServed(wg_id)
    return READ


@dataclass(frozen=True)
class ReadScope:
    """What the session may read now: the verified org state, the owner, the instructing human of the read and the
    owner's access to the session's workgroup."""

    org_id: str
    wg_id: str
    owner_id: str
    state: OrgState
    excluded: frozenset[str]
    access: str | None
    """Explicit `read`/`write` of the owner on the session's workgroup (`OrgState.explicit_access`), None without."""
    device_id: str
    actor_id: str | None
    """The instructing human of the read's turn (None: unknown)."""
    task_id: str = ""
    reach: str = "all"
    """The reach of the session's rights profile (ADR-094 section 3): `workgroup` and `task` read no other workgroup,
    `task` no other task (`outside_reach`)."""

    def outside_reach(self, wg_id: str, task_id: str | None = None) -> None:
        """`ReadRefused` forbidden for a workgroup or task beyond the profile's reach."""
        if self.reach != "all" and wg_id != self.wg_id:
            raise ReadRefused("forbidden", REACH_SENTENCE.format(reach="workgroup")).at(wg_id)
        if self.reach == "task" and task_id is not None and task_id != self.task_id:
            raise ReadRefused("forbidden", REACH_SENTENCE.format(reach="task")).at(wg_id)

    def require_workgroup(self) -> str:
        """The owner's access to the session's workgroup (`ReadRefused` forbidden without)."""
        return self.readable(self.wg_id)

    def readable(self, wg_id: str) -> str:
        """The session's access to workgroup `wg_id` (`decide`), within the profile's reach."""
        self.outside_reach(wg_id)
        return decide(
            self.state,
            device_id=self.device_id,
            own_wg=self.wg_id,
            owner_id=self.owner_id,
            actor_id=self.actor_id,
            wg_id=wg_id,
        )


def read_scope(context: Any, session: LocalSession, actor_id: str | None, reach: str = "all") -> ReadScope:
    """The read scope of `session` now for a read instructed by `actor_id` (module docstring); `ReadRefused` when the
    session may read nothing."""
    book = book_for(context)
    log = book.log(session.org_id)
    if log is None or not log.state.assigned(book.device_id, session.wg_id):
        raise NOT_ACTING
    state = log.state
    owner = None if session.owner_id is None else state.member(session.owner_id)
    if owner is None or owner.kind != USER:
        raise NOT_MEMBER
    # the instructing human still reads the session's workgroup: as a member, or as a support person of the verified
    # platform log while the workgroup is released to support (ADR-096; it never reads further: `decide`)
    reads = actor_id is None or state.wg_access(actor_id, session.wg_id) is not None
    if not reads and not state.support_reads(book.support_roster(), cast(str, actor_id), session.wg_id):
        raise HUMAN_GONE
    return ReadScope(
        org_id=session.org_id,
        wg_id=session.wg_id,
        owner_id=owner.id,
        state=state,
        excluded=book.excluded(session.org_id),
        access=state.explicit_access(owner.id, session.wg_id),
        device_id=book.device_id,
        actor_id=actor_id,
        task_id=session.task_id,
        reach=reach,
    )
