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
- **Model not in the plan** (`refusal: "model_not_in_plan"` of the record, any status): the access does not cover the
  requested model. The refusal is remembered for the access key, the model and the identity of the refused content
  (`model.limits.Limits.refuse_model`, so the pool's choice leaves the access out for that model); a refusal of the
  session's own model marks the session with `Refusal.model` (`model.fallback` moves it to an access that serves the
  model or lets it wait on its model, told once: `announced`). Such a record is no `401` refusal and no limit.
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

from agent_runner.model import limits, pauses
from agent_runner.sessions.records import LocalSession

ATTRIBUTE = "model_revocations"
REFUSED = 401
MODEL_NOT_IN_PLAN = "model_not_in_plan"
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
    model: str | None = None
    """None: the token was refused (`401`); else the model the access does not cover (`model_not_in_plan`)."""


@dataclass
class Revocations:
    """Per daemon: the refused sessions, the time of the last provisioning of each session, the tries and the sessions
    whose wait for a subscription with their model was announced."""

    refused: dict[str, Refusal] = field(default_factory=dict)
    provisioned_at: dict[str, float] = field(default_factory=dict)
    tries: dict[str, int] = field(default_factory=dict)
    announced: set[str] = field(default_factory=set)


def revocations(context: Any) -> Revocations:
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, Revocations):
        return found
    created = Revocations()
    setattr(context, ATTRIBUTE, created)
    return created


@dataclass(frozen=True)
class Seen:
    """One `usage` record of a session on a subscription, as `observe` takes it."""

    status: object
    stamp: object
    """The session's current provisioning stamp."""
    label: str
    key: str
    """The access key (`model.limits`)."""
    identity: str | None = None
    """The identity text of an operator entry's content the session holds (None: the owner's own)."""
    refusal: object = None
    """`refusal` of the record (`MODEL_NOT_IN_PLAN`)."""
    model: object = None
    """`model` of the record."""


def observe(env: Any, session: LocalSession, seen: Seen) -> None:
    """One `usage` record of a session (module docstring)."""
    context, status = env.context, seen.status
    if context is None or session.model_access != "subscription" or session.depth != 0:
        return
    if isinstance(status, bool) or not isinstance(status, int):
        return
    found = revocations(context)
    if 200 <= status < 300:
        found.tries.pop(session.id, None)
        found.announced.discard(session.id)
        if found.refused.pop(session.id, None) is not None:
            pauses.state_of(context).refused.discard(session.id)
            env.log(f"runner.model access_answered session_id={session.id}")
        return
    if seen.refusal == MODEL_NOT_IN_PLAN and isinstance(seen.model, str):
        _model_refused(env, session, seen, seen.model)
        return
    if status != REFUSED or session.id in found.refused:
        return
    _mark(env, session, Refusal(env.clock(), seen.stamp, seen.label, datetime.fromtimestamp(env.wall(), UTC)))
    env.log(f"runner.model access_refused session_id={session.id} access={seen.label}")


def _mark(env: Any, session: LocalSession, refusal: Refusal) -> None:
    revocations(env.context).refused[session.id] = refusal
    pauses.state_of(env.context).refused.add(session.id)


def _model_refused(env: Any, session: LocalSession, seen: Seen, model: str) -> None:
    """The access does not cover `model`: remembered for the access and the model (`model.limits`); a refusal of the
    session's own model also marks the session (it moves to an access that serves the model, or waits)."""
    known = limits.limits_for(env.context)
    known.refuse_model(seen.key, model, seen.identity, known.wall())
    env.log(f"runner.model model_refused session_id={session.id} access={seen.label} model={model}")
    current = revocations(env.context).refused.get(session.id)
    if model != session.model_id or (current is not None and current.model == model):
        return
    paused_at = datetime.fromtimestamp(env.wall(), UTC)
    _mark(env, session, Refusal(env.clock(), seen.stamp, seen.label, paused_at, model))


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
    found.announced.discard(session_id)
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
    found.announced &= live
    pauses.state_of(context).refused &= live
