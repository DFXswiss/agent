"""Request amender of the profiles area (`sessions.extensions`, `<area>/session_request.py:AMENDERS`; ADR-093
section 3): every session start carries the session's effective rights profile.

- `profile` of `POST /v1/sessions` (guest 0.4.0 and later, `PROFILE_GUEST`): the guest does not offer the tools the
  profile excludes and records an operation of another type as locally rejected. An older guest gets no field (its
  contract is closed); the runner and the hub still check every request.
- one line of the harness context for every guest: the profile, its types and reach, and the rule to ask instead of
  looking for another way. Without a profile nothing is added.
"""

from __future__ import annotations

import re
from typing import Any

from agent_runner.orgs.db import session as store_session
from agent_runner.profiles import state
from agent_runner.profiles.model import Profile
from agent_runner.sessions.extensions import RequestAmender
from agent_runner.sessions.records import LocalSession

PROFILE_GUEST = (0, 4, 0)
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)")
_QUERY = "SELECT g.guest_version FROM runner.vm v JOIN runner.golden g ON g.version = v.golden_version WHERE v.id = %s"


def knows_profiles(guest_version: str | None) -> bool:
    match = None if guest_version is None else _VERSION_RE.match(guest_version)
    if match is None:
        return False
    return (int(match.group(1)), int(match.group(2)), int(match.group(3))) >= PROFILE_GUEST


def harness_line(profile: Profile, depth: int) -> str:
    who = "the session that started you" if depth > 0 else "the person who gave the instruction"
    types = ", ".join(sorted(profile.operations)) or "none"
    return (
        f"Rights profile: {profile.name}. Operation types this session may request: {types}. Reads: {profile.reads}."
        f" Anything else is refused by the platform; ask {who} instead of looking for another way."
    )


async def amend(context: Any, session: LocalSession, request: dict[str, Any]) -> None:
    async with store_session(context.pool) as conn:
        profile = await state.effective(conn, session.id, session.parent_id)
        if profile is None:
            return
        version = None
        if session.vm_id is not None:
            cursor = await conn.execute(_QUERY, (session.vm_id,))
            row = await cursor.fetchone()
            version = None if row is None else str(row[0])
    if session.vm_id is None or knows_profiles(version):
        request["profile"] = profile.as_json()
    harness = request["harness"]
    harness["context_markdown"] = (
        str(harness["context_markdown"]).rstrip("\n") + "\n" + harness_line(profile, session.depth)
    )


AMENDERS = [RequestAmender("profiles.profile", amend)]
