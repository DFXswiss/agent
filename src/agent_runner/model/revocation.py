"""A subscription access refused by its provider while a session uses it (HOT-26; ADR-092 section 5, ADR-081 section 4
point 7; 05-runner.md section 10.7).

A renewal of a Claude subscription (the measuring device of ADR-092, or a token file replaced from outside) revokes the
former access token at once. A request a session's gateway still sends with it is answered `401`
(`authentication_error`, "OAuth token revoked"); Claude Code ends the turn with that error and waits for a person.
This module keeps such a refusal; the reconcile step `model_fallback` (`model.fallback.renew`) carries it on like a
used-up access - one more cause of the same flow, not a second mechanism:

- `observe(env, session, status, stamp, label)`: the gateway's `usage` record of a request of a **main session** on a
  `subscription` access (the owner's own or the operator's pool). `401` marks the session refused (`Refusal`: the
  time, the provisioning stamp and the access label it had then) and holds its prompts at once (`pauses.held`), so a
  waiting prompt is not handed to a gateway whose token is dead. A successful answer (`2xx`) ends the mark and the
  count of tries: the agent went on by itself (Claude Code retried after the new token arrived).
- `provisioned(context, session_id, at)`: every provisioning of a running session's gateway (`model.gateways`: the
  renewal, the move to another operator file, the provisioning after a lost credential) is noted.
- `renewed(context, session_id, refused, stamp)`: a valid access is there - the stamp of the session's provisioning
  differs from the one it was refused with (a renewed record, a renewed or another file), or the gateway got a
  credential at most `FRESH_S` before the refusal (the request carried the former token just before the new one
  arrived).
- `tries`: continuations since the last successful answer; after `MAX_TRIES`, or when no valid access arrives within
  `WAIT_S`, the refusal is given up: the error stays visible as before and the next prompt of a person is delivered.

The marks live in the daemon's memory only (a restarted daemon leaves the session as it is, like before HOT-26).
Nothing of a request or a token is kept: a mark holds times, the stamp (never a secret) and the label. Subagent
sessions are not marked (their one turn ends with the error, like a used-up access ends them).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_runner.model import pauses
from agent_runner.sessions.records import LocalSession

ATTRIBUTE = "model_revocations"
REFUSED = 401
FRESH_S = 60.0
"""A provisioning at most this long before the refusal holds the new access (the request carried the former one)."""
WAIT_S = 300.0
"""A refused session waits this long for a valid access; then the error stays as it is."""
MAX_TRIES = 3
"""Continuations after refusals without a successful answer in between; then the error stays as it is."""


@dataclass(frozen=True)
class Refusal:
    at: float
    """`env.clock()` of the first refusal since the last successful answer."""
    stamp: object
    """The `model.access.Stamp` of the session's provisioning at that time (None when it had none)."""
    label: str
    """The access label then (`subscription` or `operator`, `model.access.Resolved.label`)."""
    paused_at: datetime
    """The wall time of the refusal: the stable prompt ID of its continuation (`model.fallback.prompt_id`)."""


@dataclass
class Revocations:
    """Per daemon: the refused sessions, the time of the last provisioning of each session and the tries."""

    refused: dict[str, Refusal] = field(default_factory=dict)
    provisioned_at: dict[str, float] = field(default_factory=dict)
    tries: dict[str, int] = field(default_factory=dict)


def revocations(context: Any) -> Revocations:
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Revocations):
        return found
    created = Revocations()
    setattr(context, ATTRIBUTE, created)
    return created


def observe(env: Any, session: LocalSession, status: object, stamp: object, label: str) -> None:
    """One `usage` record of a session (module docstring)."""
    context = env.context
    if context is None or session.model_access != "subscription" or session.depth != 0:
        return
    if isinstance(status, bool) or not isinstance(status, int):
        return
    found = revocations(context)
    if 200 <= status < 300:
        found.tries.pop(session.id, None)
        if found.refused.pop(session.id, None) is not None:
            pauses.state_of(context).refused.discard(session.id)
            env.log(f"runner.model access_answered session_id={session.id}")
        return
    if status != REFUSED or session.id in found.refused:
        return
    paused_at = datetime.fromtimestamp(env.wall(), UTC)
    found.refused[session.id] = Refusal(at=env.clock(), stamp=stamp, label=label, paused_at=paused_at)
    pauses.state_of(context).refused.add(session.id)
    env.log(f"runner.model access_refused session_id={session.id} access={label}")


def provisioned(context: Any, session_id: str, at: float) -> None:
    """The gateway of `session_id` got a credential at `at` (`env.clock()`)."""
    revocations(context).provisioned_at[session_id] = at


def renewed(context: Any, session_id: str, refused: Refusal, stamp: object) -> bool:
    """A valid access is there for the refused session (module docstring); `stamp` is its current provisioning."""
    if stamp is not None and stamp != refused.stamp:
        return True
    at = revocations(context).provisioned_at.get(session_id)
    return at is not None and at >= refused.at - FRESH_S


def settle(context: Any, session_id: str, *, continued: bool) -> None:
    """The refusal is carried on: continued (one more try) or given up (the tries start again). The mark goes and the
    session's prompts are no longer held for it."""
    found = revocations(context)
    del found.refused[session_id]
    if continued:
        found.tries[session_id] = found.tries.get(session_id, 0) + 1
    else:
        found.tries.pop(session_id, None)
    pauses.state_of(context).refused.discard(session_id)


def prune(context: Any, live: set[str]) -> None:
    """Forget the sessions that are no longer listed on this device."""
    found = revocations(context)
    for table in (found.refused, found.provisioned_at, found.tries):
        for session_id in [sid for sid in table if sid not in live]:
            del table[session_id]
    pauses.state_of(context).refused &= live
