"""Read scope of a session at call time (ADR-024, 03-api.md section 8.1 "Read scope", 04-crypto.md section 4.3).

A session reads with the rights of its **task owner**, evaluated against the verified membership log at the moment
of the call (`OrgBook`, never a cached answer of the hub), intersected with the session's workgroup:

- organization level (workgroup names, members of the org, org whiteboard): the owner is an active user of the org;
- workgroup level (tasks, chat messages, workgroup and task whiteboards, operations, workgroup members): additionally
  the owner has `read` or `write` access to the session's workgroup (default workgroup: `write`);
- other workgroups: names only (`list_workgroups`), never their content - the device holds no keys for them anyway.

An owner who was removed from the org is `forbidden` for every read; one who lost the workgroup access keeps the
organization level and is `forbidden` on the workgroup level. A device that cannot act for the org right now (copy
not verified, org stopped, decertified) or is no longer assigned to the session's workgroup answers `unavailable`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_runner.crypto.log.state import USER, OrgState
from agent_runner.orgs.book import book_for
from agent_runner.sessions.records import LocalSession


class ReadRefused(Exception):
    """A read that is not answered with content: tool error code and English sentence."""

    def __init__(self, error: str, message: str) -> None:
        super().__init__(error)
        self.error = error
        self.message = message


NOT_MEMBER = ReadRefused("forbidden", "The task owner is no longer a member of this organization.")
NO_ACCESS = ReadRefused("forbidden", "The task owner has no access to this workgroup anymore.")
NOT_ACTING = ReadRefused("unavailable", "This device cannot read this organization right now. Try again later.")


@dataclass(frozen=True)
class ReadScope:
    """What the session may read now: the verified org state, the owner and the owner's workgroup access."""

    org_id: str
    wg_id: str
    owner_id: str
    state: OrgState
    excluded: frozenset[str]
    access: str | None
    """`read`/`write` of the owner on the session's workgroup, None without access."""

    def require_workgroup(self) -> str:
        """The owner's access to the session's workgroup (`ReadRefused` forbidden without)."""
        if self.access is None:
            raise NO_ACCESS
        return self.access


def read_scope(context: Any, session: LocalSession) -> ReadScope:
    """The read scope of `session` now (module docstring); `ReadRefused` when the session may read nothing."""
    book = book_for(context)
    log = book.log(session.org_id)
    if log is None or not log.state.assigned(book.device_id, session.wg_id):
        raise NOT_ACTING
    state = log.state
    owner = None if session.owner_id is None else state.member(session.owner_id)
    if owner is None or owner.kind != USER:
        raise NOT_MEMBER
    return ReadScope(
        org_id=session.org_id,
        wg_id=session.wg_id,
        owner_id=owner.id,
        state=state,
        excluded=book.excluded(session.org_id),
        access=state.wg_access(owner.id, session.wg_id),
    )
