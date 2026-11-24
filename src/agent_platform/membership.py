"""Membership changes that may take access away (11-agent-access.md section 8.4; 04-crypto.md sections 4.7, 5.10;
06-web.md sections 7.1, 7.2 and 11 job 4; twin of the web client's `org/batch.ts`, `org/members.ts`,
`org/workgroups.ts`, `org/invites.ts` and `jobs/rotation.ts`).

`submit(account, org, planner)` is the web's `submitBatch`: the planner looks at the verified log and returns the
requested entries (None: nothing left to do, e.g. another admin made the change meanwhile; a `PlatformError` when the
change is not allowed); `batch.build_batch` adds one `rotate` per element of R with fresh keys and the wraps for every
remaining reader and checks the result against the hub's rules; one `POST /v1/orgs/{org}/log`, whose answer must name
the head the batch produces; the new keys of scopes the user still reads are kept for the rest of the run. After
`seq_conflict` the log is re-read and the planner runs again (`orgs.with_retry`).

The pending-rotation job of the browser (`rotate_pending`, `org rotate`): an admin unassigns platform devices whose
certification ended (verified platform log) plus R; then one `rotate` for every scope of the log's `pending` (a member
left by itself) that the user may rotate (org: admin; workgroup: admin or writer). The browser runs it whenever a log
changes; the tool runs it before every writing command (`upkeep.prepare`), after each of its membership changes and
on `org rotate`. The credential-key check of the browser's re-wrap job (`credkey.rotate_if_owed`) runs before every
writing command as well and after every change that can take a device's eligibility away (`member remove`,
`wg ungrant`, `device unassign`, `org leave`). A failure of these follow-ups does not undo the change: it is reported
in the result (`error` and its fields). `wg grant` follows the web's `grantPlan`: not on the default or an archived
workgroup (`invalid_argument`, `reason: forbidden`), nothing to do when the member has exactly that access.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from typing import Any, cast

from agent_platform import credkey
from agent_platform.account import Account, unverified
from agent_platform.batch import Built, build_batch
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.hub import segment
from agent_platform.orgs import crypto_refusal, find_org, now_text, require_head, with_retry
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import is_uuid7
from agent_runner.crypto.log.platform import PlatformState
from agent_runner.crypto.log.state import ADMIN, MEMBER, ORG, USER, WRITE, OrgState
from agent_runner.crypto.log.verify import VerifiedLog

Fields = list[dict[str, object]]
Planner = Callable[[VerifiedLog, str], Fields | None]
EventsOf = Callable[[Built], list[dict[str, object]]]
MAX_PENDING_BATCHES = 2


def require_id(value: str) -> str:
    if not is_uuid7(value):
        raise PlatformError("invalid_argument")
    return value


def require_admin(state: OrgState, me: str) -> None:
    if not state.is_admin(me):
        raise PlatformError("not_admin")


def is_last_admin(state: OrgState, subject: str) -> bool:
    return state.is_admin(subject) and len(state.admins()) == 1


def platform_for(account: Account, state: OrgState, fields: Sequence[dict[str, object]]) -> PlatformState | None:
    """The verified platform state when the batch names a platform device or the org has one (exclusions)."""
    needed = any("platform_seq" in item for item in fields) or any(
        p.platform_seq is not None for p in state.principals.values()
    )
    return account.platform() if needed else None


def submit(
    account: Account, org: str, planner: Planner, events_of: EventsOf | None = None, *, history: bool = False
) -> Built | None:
    """The append flow of the module docstring; None when the planner finds nothing to do."""

    def attempt() -> Built | None:
        log = account.org_log(org)
        fields = planner(log, account.user_id)
        if fields is None:
            return None
        try:
            built = build_batch(
                log,
                org=org,
                author=account.user_id,
                identity=account.identity,
                fields=fields,
                ts=now_text(),
                held=account.keys(org),
                platform=platform_for(account, log.state, fields),
                history=history,
            )
        except CryptoError as error:
            raise crypto_refusal(error) from None
        events = [] if events_of is None else events_of(built)
        answer = account.call("POST", f"/v1/orgs/{segment(org)}/log", built.body(events))
        require_head(answer, built.log)
        account.remember(org, built.log)
        state = built.log.state
        account.learn({slot: key for slot, key in built.new_keys.items() if state.can_read(account.user_id, *slot[:2])})
        return built

    return with_retry(account, org, attempt)


def outcome(org: str, built: Built | None) -> dict[str, Any]:
    """What a change did: whether it appended, the head and the scopes it rotated (R and pending rotations)."""
    if built is None:
        return {"org_id": org, "changed": False}
    rotated = [
        {"scope": body["scope"], "scope_id": body["scope_id"], "epoch": body["epoch"]}
        for body in (cast(dict[str, object], entry["body"]) for entry in built.entries)
        if body["type"] == "rotate"
    ]
    return {"org_id": org, "changed": True, "seq": built.log.head.seq, "rotated": rotated}


def follow_up(work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """A follow-up of a change: its result, or the refusal as `{error, ...fields}` (the change itself stands)."""
    try:
        return work()
    except PlatformError as error:
        return {"error": error.code, **error.fields}


# --- the pending-rotation job ---


def owed_planner(platform: PlatformState | None) -> Planner:
    """The next batch the user owes (`jobs/rotation.ts` `owedPlanner`), or None."""

    def plan(log: VerifiedLog, me: str) -> Fields | None:
        state = log.state
        if platform is not None and state.is_admin(me):
            unassign: Fields = [
                {"type": "device_unassign", "device": device, "wg": wg}
                for device in sorted(platform.excluded(state))
                for wg in state.device_wgs(device)
            ]
            if unassign:
                return unassign
        rotate: Fields = [
            {"type": "rotate", "scope": scope, "scope_id": scope_id, "epoch": state.epoch(scope, scope_id) + 1}
            for scope, scope_id in sorted(state.pending)
            if (
                state.is_admin(me)
                if scope == ORG
                else scope_id in state.workgroups and (state.is_admin(me) or state.wg_access(me, scope_id) == WRITE)
            )
        ]
        return rotate or None

    return plan


def rotate_pending(account: Account, org: str) -> dict[str, Any]:
    """Up to `MAX_PENDING_BATCHES` owed batches of `org`: the scopes rotated and the devices unassigned."""
    platform = account.platform() if account.platform_root else None
    planner = owed_planner(platform)
    scopes: list[dict[str, object]] = []
    devices: set[str] = set()
    batches = 0
    while batches < MAX_PENDING_BATCHES:
        built = submit(account, org, planner)
        if built is None:
            break
        batches += 1
        bodies = [cast(dict[str, object], entry["body"]) for entry in built.entries]
        scopes += [
            {"scope": b["scope"], "scope_id": b["scope_id"], "epoch": b["epoch"]}
            for b in bodies
            if b["type"] == "rotate"
        ]
        devices |= {str(b["device"]) for b in bodies if b["type"] == "device_unassign"}
    return {"batches": batches, "rotated": scopes, "unassigned_devices": sorted(devices)}


def changed(account: Account, org: str, built: Built | None, *, credentials: bool) -> dict[str, Any]:
    """The outcome of a change with the follow-ups of the module docstring."""
    result = outcome(org, built)
    if built is None:
        return result
    result["pending_rotation"] = follow_up(lambda: rotate_pending(account, org))
    if credentials:
        result["credentials"] = follow_up(lambda: credkey.rotate_if_owed(account))
    return result


# --- members ---


def member_rows(state: OrgState, me: str, names: dict[str, str]) -> list[dict[str, Any]]:
    """The active users, admins first, then by name (unnamed last) and ID (`memberRows`)."""
    order = sorted(state.workgroups, key=lambda wg: (wg != state.default_wg, wg))
    rows = []
    for principal in state.principals.values():
        if principal.kind != USER or not principal.active:
            continue
        access = [
            {
                "wg_id": wg,
                "access": found,
                "default": wg == state.default_wg,
                "inherited": state.inherited(principal.id, wg),
            }
            for wg in order
            if (found := state.wg_access(principal.id, wg)) is not None
        ]
        rows.append(
            {
                "user_id": principal.id,
                "name": names.get(principal.id),
                "role": principal.role,
                "access": access,
                "is_me": principal.id == me,
                "last_admin": is_last_admin(state, principal.id),
                "joined_seq": principal.joined_seq,
            }
        )
    return sorted(
        rows,
        key=lambda row: (row["role"] != ADMIN, row["name"] is None, (row["name"] or "").casefold(), row["user_id"]),
    )


def member_list(account: Account, org: str) -> dict[str, Any]:
    state = account.org_log(org).state
    rows = member_rows(state, account.user_id, account.names(org).members)
    pending = [{"scope": scope, "scope_id": scope_id} for scope, scope_id in sorted(state.pending)]
    return {"org_id": org, "members": rows, "can_manage": state.is_admin(account.user_id), "rotation_pending": pending}


def _require_user(state: OrgState, subject: str) -> None:
    found = state.member(subject)
    if found is None or found.kind != USER:
        raise PlatformError("not_found")


def role_plan(subject: str, role: str) -> Planner:
    def plan(log: VerifiedLog, me: str) -> Fields | None:
        state = log.state
        require_admin(state, me)
        _require_user(state, subject)
        if cast(Any, state.member(subject)).role == role:
            return None
        if is_last_admin(state, subject):
            raise PlatformError("last_admin")
        return [{"type": "role", "subject": subject, "role": role}]

    return plan


def remove_plan(subject: str) -> Planner:
    def plan(log: VerifiedLog, me: str) -> Fields | None:
        state = log.state
        require_admin(state, me)
        if subject == me:
            raise PlatformError("invalid_argument", reason="use_org_leave")
        if state.member(subject) is None:
            return None
        return [{"type": "remove", "subject": subject}]

    return plan


def leave_plan(log: VerifiedLog, me: str) -> Fields | None:
    state = log.state
    if state.member(me) is None:
        return None
    if is_last_admin(state, me):
        raise PlatformError("last_admin")
    return [{"type": "remove", "subject": me}]


def member_role(account: Account, org: str, user: str, role: str) -> dict[str, Any]:
    built = submit(account, org, role_plan(require_id(user), role))
    return {**changed(account, org, built, credentials=False), "user_id": user, "role": role}


def member_remove(account: Account, org: str, user: str) -> dict[str, Any]:
    built = submit(account, org, remove_plan(require_id(user)))
    return {**changed(account, org, built, credentials=True), "user_id": user}


def org_leave(account: Account, org: str) -> dict[str, Any]:
    """Leave the org (a self `remove`): the rotation is owed by the remaining admins and writers (`pending`)."""
    built = submit(account, org, leave_plan)
    result = outcome(org, built)
    if built is not None:
        result["credentials"] = follow_up(lambda: credkey.rotate_if_owed(account, former=[built.log]))
    return result


def org_rotate(account: Account, org: str) -> dict[str, Any]:
    """The pending-rotation job now; after a sent batch the credential-key check (a device may have lost access)."""
    result = {"org_id": org, **rotate_pending(account, org)}
    if result["batches"]:
        result["credentials"] = follow_up(lambda: credkey.rotate_if_owed(account))
    return result


# --- invitations ---


def invite_revoke(account: Account, org: str, invite: str) -> dict[str, Any]:
    """Revoke an open invitation (`invite_revoke`); done when the re-read log no longer has it open."""

    def attempt() -> dict[str, Any]:
        log = account.org_log(org)
        state = log.state
        require_admin(state, account.user_id)
        known = state.invites.get(invite)
        if known is None:
            raise PlatformError("not_found")
        if known.revoked_seq is not None or known.accepted_seq is not None:
            return {"org_id": org, "invite_id": invite, "changed": False}
        fields: Fields = [{"type": "invite_revoke", "invite_id": invite}]
        built = build_batch(
            log, org=org, author=account.user_id, identity=account.identity, fields=fields, ts=now_text(), held={}
        )
        target = f"/v1/orgs/{segment(org)}/invites/{segment(invite)}/revoke"
        answer = account.call("POST", target, {"entry": built.entries[0]})
        if answer.get("seq") != built.log.head.seq:
            raise unverified("response_invalid")
        account.remember(org, built.log)
        return {"org_id": org, "invite_id": invite, "changed": True, "seq": built.log.head.seq}

    return with_retry(account, org, attempt)


# --- workgroups ---


def grant_plan(subject: str, wg: str, access: str) -> Planner:
    """`grant` of `access` (`grantPlan` of the web): not on the default workgroup nor an archived one (`forbidden`);
    None once the subject has exactly that explicit access."""

    def plan(log: VerifiedLog, me: str) -> Fields | None:
        state = log.state
        require_admin(state, me)
        _require_user(state, subject)
        if wg == state.default_wg or not state.writable(wg):
            raise PlatformError("invalid_argument", reason="forbidden")
        if state.explicit_access(subject, wg) == access:
            return None
        return [{"type": "grant", "subject": subject, "wg": wg, "access": access}]

    return plan


def ungrant_plan(subject: str, wg: str) -> Planner:
    def plan(log: VerifiedLog, me: str) -> Fields | None:
        state = log.state
        require_admin(state, me)
        if wg == state.default_wg:
            raise PlatformError("invalid_argument", reason="forbidden")
        # only the explicit access: an admin's inherited read stays (ADR-058), so there is nothing to ungrant
        if state.member(subject) is None or state.explicit_access(subject, wg) is None:
            return None
        return [{"type": "ungrant", "subject": subject, "wg": wg}]

    return plan


def archive_plan(wg: str) -> Planner:
    def plan(log: VerifiedLog, me: str) -> Fields | None:
        state = log.state
        require_admin(state, me)
        if wg == state.default_wg:
            raise PlatformError("invalid_argument", reason="forbidden")
        if not state.writable(wg):
            return None
        return [{"type": "wg_archive", "wg": wg}]

    return plan


def wg_grant(account: Account, wg: str, user: str, access: str, history: bool) -> dict[str, Any]:
    """Give a member `access` (`read`/`write`, the parser's choices) to a workgroup (with `history` also the older
    epochs)."""
    org = find_org(account, require_id(wg))
    built = submit(account, org, grant_plan(require_id(user), wg, access), history=history)
    return {**outcome(org, built), "wg_id": wg, "user_id": user, "access": access}


def wg_ungrant(account: Account, wg: str, user: str) -> dict[str, Any]:
    org = find_org(account, require_id(wg))
    built = submit(account, org, ungrant_plan(require_id(user), wg))
    return {**changed(account, org, built, credentials=True), "wg_id": wg, "user_id": user}


def wg_archive(account: Account, wg: str) -> dict[str, Any]:
    org = find_org(account, require_id(wg))
    built = submit(account, org, archive_plan(wg))
    return {**changed(account, org, built, credentials=False), "wg_id": wg}


# --- command line ---


def _member_list(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return member_list(account, args.org)


def _member_role(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return member_role(account, args.org, args.user, args.role)


def _member_remove(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return member_remove(account, args.org, args.user)


def _org_leave(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return org_leave(account, args.org)


def _org_rotate(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return org_rotate(account, args.org)


def _invite_revoke(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return invite_revoke(account, args.org, args.invite)


def _wg_ungrant(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return wg_ungrant(account, args.wg, args.user)


def _wg_archive(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return wg_archive(account, args.wg)


def register(tree: Tree) -> None:
    group = tree.group("member", "members of an organization")
    sub = tree.command(
        group,
        "list",
        _member_list,
        "members with role and workgroup access (verified log)",
        example="agent-platform member list --org <org_id>",
        words="mitglieder rolle zugriff personen leute members people access",
        trim="first",
    )
    sub.add_argument("--org", required=True)
    sub = tree.command(
        group,
        "role",
        _member_role,
        "make a member admin or member",
        writes=True,
        example="agent-platform member role --org <org_id> --user <user_id> --role admin",
        words="befördern rolle rechte administrator promote demote rights",
    )
    sub.add_argument("--org", required=True)
    sub.add_argument("--user", required=True)
    sub.add_argument("--role", required=True, choices=[ADMIN, MEMBER])
    sub = tree.command(
        group,
        "remove",
        _member_remove,
        "remove a member (rotates the keys it could read)",
        writes=True,
        example="agent-platform member remove --org <org_id> --user <user_id>",
        words="rauswerfen mitglied entfernen person kick person",
    )
    sub.add_argument("--org", required=True)
    sub.add_argument("--user", required=True)
    group = tree.group("org", "organizations")
    sub = tree.command(
        group,
        "leave",
        _org_leave,
        "leave an organization",
        writes=True,
        example="agent-platform org leave --org <org_id>",
        words="verlassen austreten organisation quit exit",
    )
    sub.add_argument("--org", required=True)
    sub = tree.command(
        group,
        "invite-revoke",
        _invite_revoke,
        "revoke an open invitation",
        writes=True,
        example="agent-platform org invite-revoke --org <org_id> --invite=<invite_id>",
        words="zurückziehen einladung widerrufen cancel invitation withdraw",
    )
    sub.add_argument("--org", required=True)
    sub.add_argument("--invite", required=True, help="invite_id (see `org show`)")
    sub = tree.command(
        group,
        "rotate",
        _org_rotate,
        "the owed key rotations of an organization",
        example="agent-platform org rotate --org <org_id>",
        words="schlüsselwechsel fällig offen schlüssel key rotation owed pending",
    )
    sub.add_argument("--org", required=True)
    group = tree.group("wg", "workgroups")
    sub = tree.command(
        group,
        "ungrant",
        _wg_ungrant,
        "take a member's workgroup access away (rotates the key)",
        writes=True,
        example="agent-platform wg ungrant --wg <wg_id> --user <user_id>",
        words="entziehen zugriff wegnehmen arbeitsgruppe mitglied revoke access take away",
    )
    sub.add_argument("--wg", required=True)
    sub.add_argument("--user", required=True)
    sub = tree.command(
        group,
        "archive",
        _wg_archive,
        "archive a workgroup (read-only)",
        writes=True,
        example="agent-platform wg archive --wg <wg_id>",
        words="archivieren stilllegen arbeitsgruppe schreibschutz readonly freeze close",
    )
    sub.add_argument("--wg", required=True)
