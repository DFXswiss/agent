"""Names of people in an org for the agent (ADR-067; the web client's twin is `web/src/core/org/names.ts`).

- `give_name`: an admin gives a member without an own name - or the person of an invitation - a provisional name, a
  `member.name` event in the org scope (`ref` = the user ID or the `invite_id`) sealed with the current org key: every
  member and device of the org reads it, the hub does not. The member's own `member.profile` always ranks first.
- `ensure_own_name`: an org the user opens (`org show`) gets the user's own name if it has none there yet, taken from
  the own name in another org - no question, a display name is no access credential. Nothing is written when no
  other org has an own name or the current org key is not held.
- `close_settled_invites`: after a direct admission, the open user invitations the hub names the admitted account as
  registered with are revoked (the link is the hub's word; a lying hub can only make the admin revoke an open
  invitation, a denial it can cause anyway).
- `member_name`: the command `org member-name --org ORG --user (user ID | account code | invitation ID) --name TEXT`.
"""

from __future__ import annotations

import re
from typing import Any

from agent_platform.account import MEMBER_NAME, Account, unverified
from agent_platform.batch import build_batch
from agent_platform.errors import PlatformError
from agent_platform.hub import segment
from agent_platform.orgs import display_name, invite_states, now_text, org_admit, profile_event, with_retry
from agent_runner.crypto import identity as identity_format
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import is_uuid7
from agent_runner.crypto.log.state import ORG, USER, OrgState

INVITE_ID_RE = re.compile(r"[A-Za-z0-9_-]{21}[AQgw]")
"""An invitation ID (`common.json#/$defs/invite_id`)."""


def _post_event(account: Account, org: str, kind: str, body: dict[str, object], **extra: str) -> bool:
    """Seals one org-scope event with the current org key and sends it; False when that key is not held yet."""
    log = account.org_log(org)
    epoch = log.state.epoch(ORG, org)
    key = account.keys(org).get((ORG, org, epoch))
    if key is None:
        return False
    event = profile_event(account, org, key, kind, body, epoch=epoch, lh=log.head.seq, **extra)
    answer = account.call("POST", "/v1/events", {"events": [{"event": event}]})
    results = answer.get("results")
    if not isinstance(results, list) or len(results) != 1:
        raise unverified("response_invalid")
    return True


def _subject(state: OrgState, who: str) -> str:
    """A user of the org or a user invitation of its log, by user ID, account code or invitation ID."""
    if is_uuid7(who):
        found = state.principals.get(who)
        if found is not None and found.kind == USER:
            return who
        raise PlatformError("invalid_argument", reason="subject_unknown")
    invite = state.invites.get(who) if INVITE_ID_RE.fullmatch(who) else None
    if invite is not None and invite.kind == USER:
        return who
    try:
        code = identity_format.parse_account_code(who)
    except CryptoError:
        raise PlatformError("invalid_argument", reason="subject_unknown") from None
    for principal in state.principals.values():
        pub = {"kem": principal.kem, "sig": principal.sig}
        if principal.kind == USER and identity_format.account_code(principal.id, pub) == code:
            return principal.id
    raise PlatformError("invalid_argument", reason="subject_unknown")


def give_name(account: Account, org: str, who: str, name: str) -> dict[str, Any]:
    """Gives the subject `who` the provisional name `name` (admins only)."""
    text = display_name(name)
    state = account.org_log(org).state
    if not state.is_admin(account.user_id):
        raise PlatformError("not_admin")
    subject = _subject(state, who)
    if not _post_event(account, org, MEMBER_NAME, {"display_name": text}, ref=subject):
        raise unverified("key_missing")
    return {"org_id": org, "subject": subject, "name": text}


def ensure_own_name(account: Account, org: str) -> str:
    """Writes the user's own name into `org` if it has none there: `present`, `written`, `unknown` (no own name in
    another org), or `no_key` (the current org key is not held yet)."""
    if account.names(org).own.get(account.user_id) is not None:
        return "present"
    name = account.display_name(skip=org)
    if name is None:
        return "unknown"
    if not _post_event(account, org, "member.profile", {"display_name": name}, ref=account.user_id):
        return "no_key"
    return "written"


def _revoke(account: Account, org: str, invite: str) -> None:
    """`invite_revoke` of an open invitation (as the web's `revokeInvite`; KOR-42 adds the command `org
    invite-revoke`)."""

    def attempt() -> None:
        log = account.org_log(org)
        built = build_batch(
            log,
            org=org,
            author=account.user_id,
            identity=account.identity,
            fields=[{"type": "invite_revoke", "invite_id": invite}],
            ts=now_text(),
            held={},
        )
        answer = account.call(
            "POST", f"/v1/orgs/{segment(org)}/invites/{segment(invite)}/revoke", {"entry": built.entries[0]}
        )
        if answer.get("seq") != built.log.head.seq:
            raise unverified("response_invalid")
        account.remember(org, built.log)

    with_retry(account, org, attempt)


def close_settled_invites(account: Account, org: str, subject: str) -> list[str]:
    """Revokes the open user invitations the hub names `subject` as registered with, once it is a member."""
    log = account.org_log(org)
    settled = [
        str(row["invite_id"])
        for row in invite_states(account, org, log.state)
        if row["status"] == "settled" and row["registered_user"] == subject
    ]
    for invite in settled:
        _revoke(account, org, invite)
    return settled


def admit_named(account: Account, org: str, who: str, *, role: str, wgs: dict[str, str], name: str) -> dict[str, Any]:
    """`org admit --name`: the admission, then the name every member sees at once and the invitations it settles.
    The admission stands when the name fails (`name_saved` false with `name_error`; `org member-name` adds it)."""
    text = display_name(name)
    result = org_admit(account, org, who, role, wgs)
    try:
        give_name(account, org, result["user_id"], text)
        named: dict[str, Any] = {"name": text, "name_saved": True}
    except PlatformError as error:
        named = {"name": text, "name_saved": False, "name_error": error.code}
    try:
        closed = close_settled_invites(account, org, result["user_id"])
    except PlatformError:
        closed = []
    return {**result, **named, "closed_invites": closed}


def invite_named(account: Account, org: str, created: dict[str, Any], name: str | None) -> dict[str, Any]:
    """`org invite --name`: the invitation's name (`member.name` for the `invite_id`); the invitation stands when the
    name fails (`name_saved` false)."""
    if name is None:
        return created
    try:
        give_name(account, org, str(created["invite_id"]), name)
    except PlatformError as error:
        return {**created, "name": display_name(name), "name_saved": False, "name_error": error.code}
    return {**created, "name": display_name(name), "name_saved": True}


def member_name(account: Account, org: str, who: str, name: str) -> dict[str, Any]:
    """`org member-name`: give or change the provisional name of a member or of an invitation's person."""
    return give_name(account, org, who, name)
