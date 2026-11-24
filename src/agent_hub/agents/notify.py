"""Notification of the user for account-changing actions of an agent (11-agent-access.md section 8, ADR-056 §5).

Under the owner's decision an agent may do everything its user may; a stolen agent key would be a full account
access. So that such a key cannot change the account unnoticed, every account-changing action taken by an agent
writes an in-app notification of the kind `agent.account_change` for the user (`org_id` NULL: it belongs to the
account). `ref` = `<action>:<acting agent ID>:<target>` (`ACTIONS`; the target names what changed: the paired or
revoked agent, the removed passkey, the organization, the credential kind, the bundle slot's passkey, the device of a
request; empty where there is none). Nothing is written for the actions of a browser session, except
`recovery_finished` (`recovery_notice`): an anonymous recovery that added a passkey (target), with an empty agent
field - a stolen recovery code is as dangerous as a stolen agent key - and `agent_paired_remote` (`remote_notice`):
the browser session confirmed a pairing from another computer (target: the new agent; ADR-067 section 4.4).

The rows are published after the commit by `agent_hub.notifications.rules.publish_rows` as the stream frame
`notification`; only streams opened with `account=1` receive them, and `GET /v1/notifications` lists them only with
`account=1`, so a web client of before KOR-24 never sees them (decisions KOR-24).
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg

from agent_hub.audit.chain import ACTING_AGENT
from agent_hub.notifications.rules import COLUMNS, Notification, new_id, row

KIND = "agent.account_change"
ACTIONS = frozenset(
    {
        "passkey_removed",
        "recovery_replaced",
        "recovery_finished",
        "agent_revoked",
        "role_changed",
        "credential_changed",
        "bundle_replaced",
        "invite_created",
        "device_request",
        "agent_paired_remote",
    }
)
# The target part of `ref` (`contracts/api/objects.json` `account_ref`): UUIDs, credential kinds, b64u IDs.
_TARGET_RE = re.compile(r"[A-Za-z0-9_:-]{0,128}")


def _insert(
    connection: psycopg.Connection[Any], user: UUID, action: str, now: datetime, *, agent: UUID | None, target: str
) -> list[Notification]:
    if action not in ACTIONS or _TARGET_RE.fullmatch(target) is None:
        raise ValueError("unknown account change")
    found = connection.execute(
        "INSERT INTO hub.notification (id, recipient_id, kind, org_id, wg_id, task_id, ref, created_at)"
        f" VALUES (%s, %s, %s, NULL, NULL, NULL, %s, %s) RETURNING {COLUMNS}",
        (new_id(now), user, KIND, f"{action}:{'' if agent is None else agent}:{target}", now),
    ).fetchall()
    return [row(item) for item in found]


def account_change(
    connection: psycopg.Connection[Any],
    user: UUID,
    action: str,
    now: datetime,
    *,
    target: object = "",
    agent_id: UUID | None = None,
) -> list[Notification]:
    """The notification row of `action` on `target` when an agent (`agent_id`, else the agent of the current
    request) takes it; nothing for a browser session."""
    agent = agent_id or ACTING_AGENT.get()
    if agent is None:
        return []
    return _insert(connection, user, action, now, agent=agent, target=str(target))


def recovery_notice(connection: psycopg.Connection[Any], user: UUID, now: datetime, passkey: str) -> list[Notification]:
    """`recovery_finished` for the user: an anonymous recovery added the passkey `passkey`, or - for an account
    without a passkey (KOR-62) - made the agent `passkey` (its ID) the main orchestrator (no acting agent)."""
    return _insert(connection, user, "recovery_finished", now, agent=None, target=passkey)


def remote_notice(connection: psycopg.Connection[Any], user: UUID, now: datetime, agent_id: UUID) -> list[Notification]:
    """`agent_paired_remote` for the user: a browser session confirmed the remote pairing `agent_id` (no agent)."""
    return _insert(connection, user, "agent_paired_remote", now, agent=None, target=str(agent_id))
