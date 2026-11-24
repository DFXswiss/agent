"""Copied from src/agent_hub/log/rotation.py.

The rotation obligation of 04-crypto.md section 4.7: the required rotate set R.

`R(before, after, subject)` = { (`wg`, w) | subject could read workgroup w before and cannot after } union
{ (`org`, org) | subject could read the org scope before and cannot after (a removed user; a device left without any
assigned workgroup) }. The `cred` scope is never in R (the owner's browser rotates `CK`, section 5.10). R depends
only on the two membership states; platform decertification is handled by the hub (`rotation_pending`), not here.

An entry can take access from more than its subject (ADR-082): the active agent of a user loses what its user loses,
and an `agent_add` of a higher generation removes the user's previous agent. `losers` names them, `lost` is the union
of their R, and `self_owed` says when that R is owed by the others (`OrgState.pending`) instead of the batch: a
principal that leaves (`remove` by the subject itself, or of a user by its own agent) or an admin that demotes itself
(`role` by the user or its agent).
"""

from __future__ import annotations

from collections.abc import Mapping

from agent_runner.crypto.log.state import ORG, WG, OrgState, Scope

SELF_OWED = frozenset({"remove", "role"})
"""Entry types whose R is owed by the others when the subject is the author itself (or the user of the author)."""


def required_set(before: OrgState, after: OrgState, subject: str) -> frozenset[Scope]:
    """R as a set of `(scope, scope_id)`."""
    lost: set[Scope] = {(WG, wg) for wg in before.workgroups if before.can_read(subject, WG, wg)}
    lost = {scope for scope in lost if not after.can_read(subject, *scope)}
    if before.can_read(subject, ORG, before.org) and not after.can_read(subject, ORG, after.org):
        lost.add((ORG, before.org))
    return frozenset(lost)


def entry_subject(body: Mapping[str, object]) -> str:
    """The principal an entry is about: `subject`, else `device`, else its author `by`."""
    return str(body.get("subject", body.get("device", body["by"])))


def losers(before: OrgState, body: Mapping[str, object]) -> frozenset[str]:
    """The principals that may lose access by the entry: its subject and the active agent (in `before`) of the subject,
    or for `agent_add` of the agent's user (that agent is replaced)."""
    subject = entry_subject(body)
    agent = before.agent_of(str(body.get("user", subject)))
    return frozenset({subject} if agent is None else {subject, agent.id})


def lost(before: OrgState, after: OrgState, body: Mapping[str, object]) -> frozenset[Scope]:
    """The union of R over `losers`."""
    return frozenset(scope for loser in losers(before, body) for scope in required_set(before, after, loser))


def self_owed(before: OrgState, body: Mapping[str, object]) -> bool:
    """R is owed by the others: `remove`/`role` whose subject is the author or the author's user (`acting`)."""
    by = str(body["by"])
    return body["type"] in SELF_OWED and body.get("subject") in (by, before.acting(by))


def as_json(scopes: frozenset[Scope]) -> list[dict[str, str]]:
    """R in the vector/API form `[{scope, scope_id}]`, sorted."""
    return [{"scope": scope, "scope_id": scope_id} for scope, scope_id in sorted(scopes)]
