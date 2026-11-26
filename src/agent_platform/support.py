"""Release of a workgroup to DFX support (ADR-096 sections 2, 3, 4 and 6; owner rule E-04: the releasing side of the
web client's release dialog in the workgroup settings).

- `wg support grant --wg --access read|write`: one `POST /v1/orgs/{org}/log` with the entry `support_grant {wg,
  access}`, the wraps of every epoch of the workgroup the user holds to every person of the support roster
  (`PlatformState.support_roster` of the platform log the tool verifies itself: the keys of each person's root
  certification, never keys from the hub; a person that is a reader of the workgroup already holds them) and one event
  `support.info` (scope `wg`, header `wg` = the workgroup, `task`/`session`/`ref` empty) sealed under the workgroup's
  current key: `{org_name, wg_name, members}` with the names of the users that read the workgroup (users without a
  verified name are left out; support then shows their mark). The right: an admin of the organization or a user with
  effective write on the workgroup (`OrgState.can_release`; an agent acts with its user's rights) - `no_write_access`
  otherwise; an archived workgroup is `invalid_argument` with `reason: forbidden`; the same access again is refused
  locally (`invalid_argument`, `reason: already_released`), another access changes the release.
- `wg support revoke --wg`: `support_revoke {wg}` plus the `rotate` of the workgroup and the wraps of the new epoch for
  every remaining reader of the org log (`membership.submit`, the path of `wg ungrant`; the hub refuses the entry
  without its rotation, `rotation_required`), then the pending-rotation follow-up; nothing is sent when the workgroup
  has no active release (`changed: false`).
- `wg support list [--wg]`: the active releases of the verified logs (every org of the user, or the org of `--wg`)
  with their access, the log position and the time of the release (`since`, from `GET /v1/orgs/{org}/support` when
  the hub's row names the same position, else null), and the current support persons of the verified platform log
  with the display names the hub keeps for them (`name`, null when it has none) and their mark (`#XXXX`, the ID mark
  of the web client, which a name the hub changes cannot hide).
"""

from __future__ import annotations

import argparse
from typing import Any, cast

from agent_platform.account import Account, Names
from agent_platform.batch import Built
from agent_platform.commands import Context, Tree
from agent_platform.community import mark
from agent_platform.errors import PlatformError
from agent_platform.eventio import rows_of
from agent_platform.hub import segment
from agent_platform.membership import EventsOf, Fields, Planner, changed, outcome, require_id, submit
from agent_platform.orgs import find_org, now_text, seal_epoch
from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.event import new_header, seal_event
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.state import READ, USER, WG, WRITE, OrgState
from agent_runner.crypto.log.verify import VerifiedLog

KIND = "support.info"
NAME_MAX = 200
MEMBER_NAME_MAX = 100
MAX_MEMBERS = 1000


def grant_plan(wg: str, access: str) -> Planner:
    def plan(log: VerifiedLog, me: str) -> Fields | None:
        state = log.state
        if not state.can_release(me, wg):
            raise PlatformError("no_write_access")
        if not state.writable(wg):
            raise PlatformError("invalid_argument", reason="forbidden")
        if state.support_release(wg) == access:
            raise PlatformError("invalid_argument", reason="already_released", access=access)
        return [{"type": "support_grant", "wg": wg, "access": access}]

    return plan


def revoke_plan(wg: str) -> Planner:
    def plan(log: VerifiedLog, me: str) -> Fields | None:
        state = log.state
        if not state.can_release(me, wg):
            raise PlatformError("no_write_access")
        if state.support_release(wg) is None:
            return None
        return [{"type": "support_revoke", "wg": wg}]

    return plan


def info_body(state: OrgState, names: Names, org: str, wg: str) -> dict[str, object]:
    """`{org_name, wg_name, members}`: the names support needs; members are the users that read the workgroup."""
    users = [p for p in state.readers(WG, wg) if state.principals[p].kind == USER]
    members = {user: names.members[user][:MEMBER_NAME_MAX] for user in users if names.members.get(user)}
    return {
        "org_name": (names.org or org)[:NAME_MAX],
        "wg_name": (names.workgroups.get(wg) or wg)[:NAME_MAX],
        "members": dict(sorted(members.items())[:MAX_MEMBERS]),
    }


def info_events(account: Account, org: str, wg: str, names: Names) -> EventsOf:
    """The `support.info` event of the batch, sealed under the workgroup's current key after it."""

    def events(built: Built) -> list[dict[str, object]]:
        state = built.log.state
        epoch = seal_epoch(state, WG, wg)
        key = account.keys(org).get((WG, wg, epoch))
        if key is None:
            raise PlatformError("hub_refused", reason="epoch_stale")
        header = new_header(
            id=uuid7(),
            org=org,
            scope=WG,
            scope_id=wg,
            epoch=epoch,
            author=account.actor,
            kind=KIND,
            ts=now_text(),
            lh=built.log.head.seq,
            wg=wg,
        )
        body = info_body(state, names, org, wg)
        return [seal_event(key, header, body_json(body), account.signer.sig)]

    return events


def support_grant(account: Account, wg: str, access: str) -> dict[str, Any]:
    """Release the workgroup to support with `access` (`read`/`write`, the parser's choices)."""
    org = find_org(account, require_id(wg))
    roster = account.platform().support_roster()
    names = account.names(org)
    built = submit(account, org, grant_plan(wg, access), info_events(account, org, wg, names), support=roster)
    done = cast(Built, built)  # the plan refuses or returns the entry
    people = sorted({str(w["recipient_id"]) for w in done.wraps if w["recipient_id"] in roster})
    return {**outcome(org, done), "wg_id": wg, "access": access, "people": people}


def support_revoke(account: Account, wg: str) -> dict[str, Any]:
    """End the workgroup's release; its key changes in the same batch."""
    org = find_org(account, require_id(wg))
    built = submit(account, org, revoke_plan(wg))
    return {**changed(account, org, built, credentials=False), "wg_id": wg}


def _org_ids(account: Account) -> list[str]:
    return [str(item["org_id"]) for item in account.me().get("orgs", [])]


def _person(user: str, name: object) -> dict[str, Any]:
    """A support person: the name the hub keeps (null when it has none) and the ID mark."""
    return {"user_id": user, "name": name if isinstance(name, str) else None, "mark": mark(user)}


def support_list(account: Account, wg: str | None) -> dict[str, Any]:
    """The active releases of the verified logs and the current support persons (module docstring)."""
    orgs = _org_ids(account) if wg is None else [find_org(account, require_id(wg))]
    releases: list[dict[str, Any]] = []
    for org in orgs:
        try:
            state = account.org_log(org).state
        except PlatformError:
            continue
        released = sorted((key, value) for key, value in state.support.items() if wg in (None, key))
        if not released:
            continue
        roster = account.platform().support_roster()
        answer = account.call("GET", f"/v1/orgs/{segment(org)}/support")
        rows = {row.get("wg_id"): row for row in rows_of(answer, "releases")}
        given = {row.get("user_id"): row.get("name") for row in rows_of(answer, "people")}
        people = [_person(user, given.get(user)) for user in sorted(roster)]
        names = account.names(org)
        for released_wg, (access, seq) in released:
            row = rows.get(released_wg, {})
            since = row.get("granted_at") if row.get("granted_seq") == seq else None
            releases.append(
                {
                    "org_id": org,
                    "wg_id": released_wg,
                    "wg_name": names.workgroups.get(released_wg),
                    "access": access,
                    "seq": seq,
                    "since": since if isinstance(since, str) else None,
                    "people": people,
                }
            )
    return {"releases": releases}


# --- command line ---


def _grant(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return support_grant(account, args.wg, args.access)


def _revoke(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return support_revoke(account, args.wg)


def _list(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return support_list(account, args.wg)


def register(tree: Tree) -> None:
    group = tree.group("support", "release a workgroup to DFX support", parent=tree.group("wg", "workgroups"))
    sub = tree.command(
        group,
        "grant",
        _grant,
        "release a workgroup to DFX support (read or write)",
        writes=True,
        example="agent-platform wg support grant --wg <wg_id> --access read",
        words="support freigeben freigabe hilfe dfx-support helfen lesen schreiben release help access",
        then="agent-platform wg support list --wg <wg_id>",
    )
    sub.add_argument("--wg", required=True)
    sub.add_argument("--access", required=True, choices=[READ, WRITE])
    sub = tree.command(
        group,
        "revoke",
        _revoke,
        "end a workgroup's release to DFX support (changes the workgroup's key)",
        writes=True,
        example="agent-platform wg support revoke --wg <wg_id>",
        words="support widerrufen freigabe beenden entziehen dfx-support revoke release end",
    )
    sub.add_argument("--wg", required=True)
    sub = tree.command(
        group,
        "list",
        _list,
        "workgroups released to DFX support and the current support persons",
        example="agent-platform wg support list [--wg <wg_id>]",
        words="support freigaben freigegeben wer dfx-support personen releases released people who",
        trim="first",
    )
    sub.add_argument("--wg")
