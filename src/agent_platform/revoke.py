"""Real revocation of an agent (ADR-082 sections 4 and 9; twin of the web client's revocation and its background
clean-up).

- `agents revoke --id <agent>` of another agent of the user (an old orchestrator): the hub refuses its key at once
  (`DELETE /v1/me/agents/{id}`), then the agent is removed from every log of the user in which it is still active
  (`remove{subject: agent}` with the rotation of its R and the wraps of the new epochs for every remaining reader,
  `membership.submit`), then the user's `CK` is rotated and the user's records are re-encrypted
  (`credkey.rotate_if_owed`: the agent was a credential reader and lost it). Nothing new is wrapped to its key.
- `agents revoke --id <this agent>`: an agent with its own identity first leaves every log in which it is active
  (`remove{subject: self, by: self}`: its R becomes `pending` and is rotated by its user's other clients or the
  admins, as for a member that leaves), then the hub revokes it and the tool deletes its state.
- `agents revoke --all`: the other agents as above, then this agent as above, then `DELETE /v1/me/agents`.
- `cleanup`, an upkeep step before every writing command (`upkeep.prepare`): every agent of the user other than this
  one that is still active in a verified log but revoked - superseded or removed in any verified log
  (`revoked_agents`), or revoked at the hub (`GET /v1/me/agents`, read only when such an agent exists) - is removed
  from that log with its R, as the browser's background job does.

An account that holds the user's identity (mode "user") is no principal of the logs: revoking itself only revokes it
at the hub.
"""

from __future__ import annotations

from typing import Any

from agent_platform import credkey, membership
from agent_platform.account import OWN, Account
from agent_platform.errors import PlatformError
from agent_platform.eventio import rows_of, uuid_argument
from agent_platform.hub import segment
from agent_runner.crypto.log.state import AGENT
from agent_runner.crypto.log.verify import VerifiedLog
from agent_runner.crypto.log.wraps import revoked_agents

REVOKED = "revoked"


def _logs(account: Account) -> list[VerifiedLog]:
    """The verified logs of the user's orgs (none when the hub does not list them now: the next run cleans up)."""
    try:
        listed = account.me().get("orgs", [])
    except PlatformError:
        return []
    found = []
    for item in listed:
        try:
            found.append(account.org_log(str(item["org_id"])))
        except (PlatformError, KeyError, TypeError):
            continue
    return found


def _others(account: Account, logs: list[VerifiedLog]) -> dict[str, list[str]]:
    """Org -> the user's agents other than this one that are active in its verified log."""
    found: dict[str, list[str]] = {}
    for log in logs:
        for principal in log.state.principals.values():
            mine = principal.kind == AGENT and principal.parent == account.user_id and principal.active
            if mine and principal.id != account.agent_id:
                found.setdefault(log.state.org, []).append(principal.id)
    return found


def _hub_revoked(account: Account) -> set[str]:
    rows = rows_of(account.call("GET", "/v1/me/agents"), "agents")
    return {
        str(row["agent_id"]) for row in rows if row.get("state") == REVOKED and isinstance(row.get("agent_id"), str)
    }


def remove(account: Account, org: str, agent: str) -> dict[str, Any]:
    """`remove{subject: agent}` in `org` while it is active there (R rotated in the batch unless it leaves)."""

    def planner(log: VerifiedLog, me: str) -> list[dict[str, object]] | None:
        found = log.state.member(agent)
        return None if found is None else [{"type": "remove", "subject": agent}]

    return membership.outcome(org, membership.submit(account, org, planner))


def cleanup(account: Account, only: set[str] | None = None) -> list[dict[str, Any]]:
    """Remove revoked agents of the user from every log where they are still active (module docstring); `only`
    names agents revoked anyway (no question to the hub)."""
    logs = _logs(account)
    others = _others(account, logs)
    if not others:
        return []
    revoked = set(revoked_agents(log.state for log in logs)) | (only or set())
    if any(agent not in revoked for agents in others.values() for agent in agents):
        revoked |= _hub_revoked(account)
    return [
        {**remove(account, org, agent), "agent_id": agent}
        for org, agents in sorted(others.items())
        for agent in agents
        if agent in revoked
    ]


def _leave(account: Account) -> list[dict[str, Any]]:
    """This agent leaves every log in which it is active (its R becomes pending)."""
    if account.mode != OWN:
        return []
    return [
        remove(account, log.state.org, account.agent_id)
        for log in _logs(account)
        if log.state.member(account.agent_id) is not None
    ]


def agents_revoke(account: Account, agent: str | None, every: bool) -> dict[str, Any]:
    """`agents revoke --id|--all` (module docstring)."""
    if every == (agent is not None):
        raise PlatformError("invalid_argument")
    itself = every or agent == account.agent_id
    result: dict[str, Any] = {}
    if agent is not None and not itself:
        target = uuid_argument(agent)
        account.call("DELETE", f"/v1/me/agents/{segment(target)}")
        result = {"revoked": 1, "agent_id": target, "removed": cleanup(account, {target})}
    if every:
        result = {
            "removed": cleanup(account, {a for found in _others(account, _logs(account)).values() for a in found})
        }
    if result.get("removed"):
        result["credentials"] = membership.follow_up(lambda: credkey.rotate_if_owed(account))
    if not itself:
        return {**result, "this_agent_revoked": False}
    left = _leave(account)
    if every:
        answer = account.call("DELETE", "/v1/me/agents")
        count = answer.get("revoked")
        result["revoked"] = count if isinstance(count, int) else None
    else:
        account.call("DELETE", f"/v1/me/agents/{segment(account.agent_id)}")
        result.update(revoked=1, agent_id=account.agent_id)
    account.home.delete(account.agent_id, account.state.get("key_store"))
    return {**result, "left": left, "this_agent_revoked": True}
