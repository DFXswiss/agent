"""Workgroup settings of the formal checks (03-api.md sections 9 and 10, `settings.workgroup`; 05-runner.md section 12
step 3).

The inbox hands every verified `settings.workgroup` event (E1-E5, author `wg:write` at `lh`, decrypted with a held
scope key) to `keep`: the body must be exactly `{repositories, protected_branches, pr_draft_default}` with the contract
shapes, else the event is refused (`settings_invalid`). The latest event per workgroup wins by the hub's scope order
(`scope_seq`); it is kept as received with the author's verified signing key (`runner.workgroup_settings`) and opened
again only into memory (`load`).

Without any verified event a workgroup has the defaults: no repository list, no protected branch (ADR-078 section 3,
A17: GitHub's own branch protection applies; a workgroup may set its own list), pull requests as drafts.

The repository list is an optional restriction (KOR-30, ADR-054): an empty list (no event, or an event with
`repositories: []`) allows every repository the acting user's GitHub connection reaches - GitHub itself is the boundary
(`repo_not_accessible` in the pre-checks); a non-empty list allows exactly its repositories. A stored event that no
longer opens or parses may have held a list, so it fails closed: `UNREADABLE` allows no repository until a new event
is saved.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from agent_runner.operations.rules import DEFAULT_PROTECTED
from agent_runner.operations.stored import open_stored

INVALID = "settings_invalid"
KEYS = frozenset({"repositories", "protected_branches", "pr_draft_default"})
MAX_REPOSITORIES = 500
MAX_PATTERNS = 100
_REPO_RE = re.compile(r"^[a-z0-9-]{1,39}/[a-z0-9._-]{1,100}$")
_PATTERN_RE = re.compile(r"^[A-Za-z0-9._/*+-]{1,200}$")


class SettingsInvalid(ValueError):
    """A `settings.workgroup` body that does not have the contract shape."""


@dataclass(frozen=True)
class WorkgroupSettings:
    repositories: frozenset[str]
    """Allowed repositories, lower case; empty means no restriction."""
    protected_branches: tuple[str, ...]
    pr_draft_default: bool
    unreadable: bool = False
    """The stored event no longer opens or parses: no repository is allowed (fail closed)."""

    @property
    def restricted(self) -> bool:
        """True when the workgroup limits its agents to the listed repositories (none when unreadable)."""
        return bool(self.repositories) or self.unreadable

    def allows(self, repo: str) -> bool:
        return not self.restricted or repo.lower() in self.repositories


DEFAULTS = WorkgroupSettings(repositories=frozenset(), protected_branches=DEFAULT_PROTECTED, pr_draft_default=True)
UNREADABLE = WorkgroupSettings(
    repositories=frozenset(), protected_branches=DEFAULT_PROTECTED, pr_draft_default=True, unreadable=True
)


def _strings(value: object, pattern: re.Pattern[str], limit: int) -> list[str]:
    if not isinstance(value, list) or len(value) > limit:
        raise SettingsInvalid("list")
    for item in value:
        if not isinstance(item, str) or pattern.fullmatch(item) is None:
            raise SettingsInvalid("item")
    return list(value)


def parse(body: Mapping[str, object]) -> WorkgroupSettings:
    """The settings of a `settings.workgroup` body (`SettingsInvalid` for any other shape)."""
    if set(body) != KEYS or not isinstance(body["pr_draft_default"], bool):
        raise SettingsInvalid("keys")
    return WorkgroupSettings(
        repositories=frozenset(_strings(body["repositories"], _REPO_RE, MAX_REPOSITORIES)),
        protected_branches=tuple(_strings(body["protected_branches"], _PATTERN_RE, MAX_PATTERNS)),
        pr_draft_default=body["pr_draft_default"],
    )


async def keep(
    conn: AsyncConnection[Any],
    *,
    org_id: str,
    wg_id: str,
    event_id: str,
    scope_seq: int,
    author_sig: str,
    sealed: Mapping[str, object],
) -> bool:
    """Store the event when it is later than the stored one of its workgroup; True when it was stored."""
    cur = await conn.execute(
        "INSERT INTO runner.workgroup_settings (wg_id, org_id, event_id, scope_seq, author_sig, sealed)"
        " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (wg_id) DO UPDATE SET org_id = EXCLUDED.org_id,"
        " event_id = EXCLUDED.event_id, scope_seq = EXCLUDED.scope_seq, author_sig = EXCLUDED.author_sig,"
        " sealed = EXCLUDED.sealed WHERE runner.workgroup_settings.scope_seq < EXCLUDED.scope_seq",
        (wg_id, org_id, event_id, scope_seq, author_sig, Jsonb(dict(sealed))),
    )
    return cur.rowcount == 1


async def load(conn: AsyncConnection[Any], storage_key: bytes, wg_id: str) -> WorkgroupSettings:
    """The settings of the latest verified event of the workgroup, else `DEFAULTS` (no list: every repository the
    owner's GitHub connection reaches, KOR-30).

    A stored event that no longer opens or parses (it was checked when it was stored) gives `UNREADABLE`, never a
    wider allow-list than the one it may have held."""
    cur = await conn.execute("SELECT author_sig, sealed FROM runner.workgroup_settings WHERE wg_id = %s", (wg_id,))
    row = await cur.fetchone()
    if row is None:
        return DEFAULTS
    body = await open_stored(conn, storage_key, wg_id, row[1], str(row[0]))
    try:
        return UNREADABLE if body is None else parse(body)
    except SettingsInvalid:
        return UNREADABLE
