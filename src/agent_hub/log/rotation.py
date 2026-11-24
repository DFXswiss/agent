"""The rotation obligation of 04-crypto.md section 4.7: the required rotate set R.

`R(before, after, subject)` = { (`wg`, w) | subject could read workgroup w before and cannot after } union
{ (`org`, org) | subject could read the org scope before and cannot after (a removed user; a device left without any
assigned workgroup) }. The `cred` scope is never in R (the owner's browser rotates `CK`, section 5.10). R depends
only on the two membership states; platform decertification is handled by the hub (`rotation_pending`), not here.
"""

from __future__ import annotations

from collections.abc import Mapping

from agent_hub.log.state import ORG, WG, OrgState, Scope


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


def as_json(scopes: frozenset[Scope]) -> list[dict[str, str]]:
    """R in the vector/API form `[{scope, scope_id}]`, sorted."""
    return [{"scope": scope, "scope_id": scope_id} for scope, scope_id in sorted(scopes)]
