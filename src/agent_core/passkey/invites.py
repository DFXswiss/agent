"""The fields of a new invite (PASSKEY-PROTOCOL.md section 5.1, I1), one invite of the list
(section 5.2) and the texts for the keys-file pull request (section 5.6).

Used by `POST /api/key/invites`, `GET /api/key/invites` and the operator commands
`invite-bootstrap` and `invite-list` (section 5.3), so that the website and `invite-list` show the
same text.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import yaml

from ..keys import valid_handle
from ..signing import b64url_encode, format_time
from ..teams import Roster
from .encoding import (
    INVITE_MAX_SECONDS,
    INVITE_MIN_SECONDS,
    JsonInvalid,
    fingerprint,
    json_integer,
    parse_json,
    sha256,
)
from .storage import Invite, stored_binary

INVITE_FIELDS = frozenset({"handle", "team", "lifetime_seconds"})
TEAM_SCOPES = ("record:read", "record:write")
ADMINISTRATOR_SCOPES = ("record:read", "record:write", "keys:invite")
OPERATOR = "operator"
INVITE_PAGE = "/invite"


@dataclass(frozen=True)
class InviteFields:
    """The three fields of an invite that passed I1."""

    handle: str
    team: str
    lifetime_seconds: int


def invite_fields(handle: object, team: object, lifetime_seconds: object, roster: Roster) -> InviteFields | None:
    """I1 on the three values: the handle rule and not a login in the `agents` map, a team of
    `teams.yaml`, and a JSON integer from `INVITE_MIN_SECONDS` to `INVITE_MAX_SECONDS`."""
    if (
        type(handle) is not str
        or not valid_handle(handle)
        or roster.is_agent(handle)
        or type(team) is not str
        or team not in roster.teams
        or not json_integer(lifetime_seconds)
        or not INVITE_MIN_SECONDS <= lifetime_seconds <= INVITE_MAX_SECONDS  # type: ignore[operator]
    ):
        return None
    return InviteFields(handle=handle, team=team, lifetime_seconds=lifetime_seconds)  # type: ignore[arg-type]


def parse_invite_body(body: bytes, roster: Roster) -> InviteFields | None:
    """I1: one JSON object (section 2) with exactly the three fields, each valid."""
    try:
        data = parse_json(body)
    except JsonInvalid:
        return None
    if type(data) is not dict or set(data) != INVITE_FIELDS:
        return None
    return invite_fields(data["handle"], data["team"], data["lifetime_seconds"], roster)


def invite_link(origin: str, token: str) -> str:
    """The invite link: the token stands only in the fragment, which a browser never sends to a
    server (section 5.1)."""
    return f"{origin}{INVITE_PAGE}#{token}"


def token_hash(token: bytes) -> str:
    """What the hub stores of an invite token: the base64url of its SHA-256 (section 5.1)."""
    return b64url_encode(sha256(token))


def _yaml_text(value: str) -> str:
    """A value of the keys entry written plain, as section 5.6 shows it, when YAML reads it back as
    exactly that text; otherwise in double quotes (KEY-PROTOCOL.md section 3: a value that YAML
    would read as something other than text is written in quotes)."""
    try:
        plain = yaml.safe_load(value) == value
    except yaml.YAMLError:
        plain = False
    return value if plain else json.dumps(value)


def keys_entry(invite: Invite) -> str:
    """The proposed keys-file entry of a received invite, indented by two spaces (section 5.6)."""
    scopes = ADMINISTRATOR_SCOPES if invite.bootstrap else TEAM_SCOPES
    lines = (
        f"  - id: {invite.proposed_id}",
        "    kind: webauthn",
        "    principal: staff",
        f"    handle: {_yaml_text(invite.handle)}",
        f"    team: {_yaml_text(invite.team)}",
        f'    credential_id: "{invite.credential_id}"',
        f'    public_key: "{invite.public_key}"',
        f"    scopes: [{', '.join(scopes)}]",
    )
    return "\n".join(lines)


def pull_request_title(invite: Invite) -> str:
    if invite.bootstrap:
        return f"Add administrator passkey {invite.proposed_id} for {invite.handle}"
    return f"Add passkey {invite.proposed_id} for {invite.handle}"


def pull_request_body(invite: Invite, fingerprint_text: str, received: str) -> str:
    """The body of section 5.6 for a normal or a bootstrap invite, lines joined by `\\n`."""
    handle, team = invite.handle, invite.team
    membership = (
        "While GitHub sign-in exists, every member of a team in teams.yaml is a GitHub login that "
        "reads the whole team's data. Only after the confirmation above, and only if "
        f"{handle} is not yet a member of {team} in teams.yaml, this pull request adds the membership."
    )
    operator_check = (
        "An operator compares credential_id and public_key in this pull request with the output of "
        "invite-list on the hub host (PASSKEY-PROTOCOL.md section 5.6)."
    )
    if invite.bootstrap:
        summary = (
            f"Adds the administrator passkey received through bootstrap invite {invite.id} to "
            f"keys.yaml, with keys:invite. Before merging, {handle} compares the fingerprint below "
            f"with the one shown when the passkey was created. {operator_check} Before merging, the "
            f"reviewers confirm that the GitHub account with exactly the login {handle} exists and "
            "belongs to the person who created this passkey (PASSKEY-PROTOCOL.md section 5.6); "
            "without that confirmation this pull request is not merged."
        )
        created_by = "operator (bootstrap invite, PASSKEY-PROTOCOL.md section 5.3)"
        closing: tuple[str, ...] = (
            "The review of this pull request is the control over who becomes administrator.",
            "",
            membership,
        )
    else:
        summary = (
            f"Adds the passkey received through invite {invite.id} to keys.yaml. Before merging, "
            f"the administrator compares the fingerprint below with {handle} over a channel other "
            f"than the invite link (PASSKEY-PROTOCOL.md section 5.5). {operator_check} Before "
            "merging, the administrator confirms that the GitHub account with exactly the login "
            f"{handle} exists and belongs to the person who created this passkey "
            "(PASSKEY-PROTOCOL.md section 5.6); without that confirmation this pull request is not "
            "merged."
        )
        created_by = invite.created_by
        closing = (membership,)
    lines = (
        summary,
        "",
        f"- Handle: {handle}",
        f"- Team: {team}",
        f"- Created by: {created_by}",
        f"- Fingerprint: {fingerprint_text}",
        f"- Received: {received}",
        "",
        *closing,
    )
    return "\n".join(lines)


def invite_entry(invite: Invite, now: int) -> dict[str, Any]:
    """One invite of the list of section 5.2, its fields in the order shown there. The received
    key and the keys-file text are present only for status `received`; no token and no token hash
    is ever part of it."""
    entry: dict[str, Any] = {
        "invite": invite.id,
        "handle": invite.handle,
        "team": invite.team,
        "created": format_time(invite.created),
        "expires": format_time(invite.expires),
        "status": invite.status(now),
        "created_by": invite.created_by,
    }
    # Status `received` is exactly a set `received` time (section 5.2).
    if invite.received is not None:
        fingerprint_text = fingerprint(stored_binary(invite.public_key))
        received = format_time(invite.received)
        entry.update(
            {
                "received": received,
                "credential_id": invite.credential_id,
                "public_key": invite.public_key,
                "fingerprint": fingerprint_text,
                "keys_entry": keys_entry(invite),
                "pull_request_title": pull_request_title(invite),
                "pull_request_body": pull_request_body(invite, fingerprint_text, received),
            }
        )
    return entry
