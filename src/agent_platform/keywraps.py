"""Catch-up wrapping, the browser's job 3 (06-web.md section 11, 04-crypto.md section 5.11, 03-api.md section 4.3; twin
of `web/src/core/jobs/catch-up.ts` and `confirmedSlots` of `web/src/core/log/catchup.ts`).

`org catch-up --org`: `GET /v1/orgs/{org}/catch-up` lists the slots of the current epochs without a wrap that the
user may create. Only what the verified logs confirm is wrapped: `org`/`wg` slots of the current epoch whose
recipient is entitled now and that the user may wrap (W1/W2), with the scope key the user holds; the user's own `cred`
slots of the newest `CK` epoch for devices the verified logs entitle to it. Recipient keys come from the verified log,
never from the hub. The wraps go out in one `POST /v1/wraps`; nothing is sent when nothing is left.

Support (ADR-096): for an org with an active release, the persons of the support roster of the verified platform log
(`support_roster`) are recipients of every epoch of a released workgroup the user holds (W2 with the roster), sealed to
the keys of their root certification, so a person that joined support later gets the existing releases.
"""

from __future__ import annotations

from typing import Any

from agent_platform.account import Account
from agent_platform.batch import make_wrap
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.eventio import rows_of, uuid_argument
from agent_platform.hub import segment
from agent_platform.reader import excluded
from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.log.state import ORG, WG, OrgState, Roster
from agent_runner.crypto.log.verify import VerifiedLog
from agent_runner.crypto.log.wraps import CRED, WrapSlot, may_wrap, may_wrap_cred


def offered(answer: dict[str, Any]) -> list[WrapSlot]:
    slots = set()
    for item in rows_of(answer, "missing"):
        scope, scope_id, epoch, recipient = (item.get(k) for k in ("scope", "scope_id", "epoch", "recipient_id"))
        texts = isinstance(scope, str) and isinstance(scope_id, str) and isinstance(recipient, str)
        if texts and isinstance(epoch, int) and not isinstance(epoch, bool):
            slots.add(WrapSlot(str(scope), str(scope_id), epoch, str(recipient)))
    return sorted(slots)


def _logs(account: Account) -> list[VerifiedLog]:
    found = []
    for item in account.me().get("orgs", []):
        try:
            found.append(account.org_log(str(item["org_id"])))
        except (PlatformError, KeyError, TypeError):
            continue
    return found


def _kem(states: list[OrgState], principal: str) -> bytes:
    return next(b64u_decode(s.principals[principal].kem, 32) for s in states if principal in s.principals)


def _cred_wraps(account: Account, mine: list[WrapSlot]) -> list[dict[str, object]]:
    """The user's own `cred` slots of the newest verified `CK` epoch (none without one)."""
    try:
        epoch, key = account.cred_key()
    except PlatformError:
        return []
    logs = _logs(account)
    states = [found.state for found in logs]
    skip = [device for found in logs for device in excluded(account, found)]
    return [
        make_wrap(account.signer, slot, _kem(states, slot.recipient), key)
        for slot in mine
        if slot.epoch == epoch and may_wrap_cred(states, slot, account.actor, epoch, skip)
    ]


def support_roster(account: Account, state: OrgState) -> Roster:
    """The support roster of the verified platform log for an org with an active release (empty otherwise or without
    a platform root key)."""
    if not state.support or not account.platform_root:
        return {}
    return account.platform().support_roster()


def _recipient_kem(state: OrgState, roster: Roster, slot: WrapSlot) -> bytes:
    """A reader's key from the log; a support person's from its root certification."""
    if slot.recipient in state.readers(slot.scope, slot.scope_id):
        return _kem([state], slot.recipient)
    return b64u_decode(roster[slot.recipient].kem, 32)


def org_catch_up(account: Account, org: str) -> dict[str, Any]:
    log = account.org_log(uuid_argument(org))
    slots = offered(account.call("GET", f"/v1/orgs/{segment(org)}/catch-up"))
    skip = excluded(account, log)
    ring = account.keys(org)
    state = log.state
    roster = support_roster(account, state)
    wraps = []
    scoped = [
        s
        for s in slots
        if s.scope in (ORG, WG)
        and (s.epoch == state.epoch(s.scope, s.scope_id) or (s.scope_id in state.support and s.recipient in roster))
    ]
    for slot in scoped:
        key = ring.get((slot.scope, slot.scope_id, slot.epoch))
        if key is not None and may_wrap(log, slot, account.actor, skip, roster):
            wraps.append(make_wrap(account.signer, slot, _recipient_kem(state, roster, slot), key))
    mine = [slot for slot in slots if slot.scope == CRED and slot.scope_id == account.user_id]
    if mine:
        wraps += _cred_wraps(account, mine)
    if wraps:
        account.call("POST", "/v1/wraps", {"wraps": wraps})
    return {"org_id": org, "offered": len(slots), "wrapped": len(wraps)}


def _catch_up(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return org_catch_up(account, args.org)


def register(tree: Tree) -> None:
    group = tree.group("org", "organizations")
    sub = tree.command(
        group,
        "catch-up",
        _catch_up,
        "wrap the org's keys for members and devices still without them",
        example="agent-platform org catch-up --org <org_id>",
        words="nachholen schlüssel fehlende verteilen mitglieder missing keys distribute",
    )
    sub.add_argument("--org", required=True)
