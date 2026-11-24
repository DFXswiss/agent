"""Devices of an org (11-agent-access.md section 8.4; 06-web.md section 7.3; 03-api.md sections 4.2, 4.5, 4.6.2;
04-crypto.md sections 4.6, 5.7-5.10; twin of the web client's `org/devices.ts` and `org/invites.ts`).

- `device list`: every active device member of the verified log (newest first) with its kind, the workgroups it is
  assigned to and, for platform devices, whether the verified platform log still certifies it; labels from the newest
  verified `device.profile` of the org scope, `organization` when the verified log assigns it to the whole
  organization (`device_assign_org`, ADR-075 section 5); the hub's list (`GET /v1/orgs/{org}/devices`) only adds
  presence, version, capacity (with its counters and limits) and the device's VMs with workgroup and sessions, and a
  device the log does not know is not shown.
- `device unassign`: `device_unassign` plus the rotation of R (`membership.submit`), then the credential-key check;
  with `--org` instead of `--wg` the end of an assignment to the whole organization (`device_unassign_org`, ADR-075).
- `device rename`: a new `device.profile` event (org scope, `ref` = device) sealed with the current org key.
- `device invite`: what the device screen does to add an org device: an `invite` entry of kind `device` for the chosen
  workgroups with the sealed package (the org key and the keys of those workgroups), `POST /v1/orgs/{org}/invites`; the
  code is printed once with the enrollment command `agent-runner enroll --hub <origin> --code <code>`, which the
  device's owner runs on the device.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from agent_platform import credkey, membership
from agent_platform.account import Account, unverified
from agent_platform.batch import build_batch
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.eventio import post_event, seal
from agent_platform.hub import segment
from agent_platform.orgs import EXPIRY_HOURS_MAX, crypto_refusal, device_unassign_org, find_org, now_text, with_retry
from agent_platform.reader import VERIFIED, scope_events, settle
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.encoding import b64u_encode, format_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log.state import DEVICE, ORG, WG, OrgState
from agent_runner.crypto.log.verify import VerifiedLog

DEVICE_PROFILE = "device.profile"
LABEL_MAX = 100
DEVICE_EXPIRY_HOURS = 24
HUB_FIELDS = ("online", "last_seen_at", "agent_version", "capacity", "capacity_low", "state", "vms")


def device_label(text: str) -> str:
    label = text.strip()
    if not 1 <= len(label) <= LABEL_MAX or "\r" in label or "\n" in label:
        raise PlatformError("invalid_argument")
    return label


def labels(account: Account, org: str) -> dict[str, str]:
    """Device ID -> label from the verified `device.profile` events (the newest by position wins)."""
    found: dict[str, str] = {}
    ring = account.keys(org)
    settled = [settle(account, org, item, ring) for item in scope_events(account, org, {"kinds": DEVICE_PROFILE})]
    for event in sorted(settled, key=lambda item: item.seq):
        body = event.body
        if event.status == VERIFIED and isinstance(body, dict) and isinstance(body.get("label"), str):
            found[str(event.header["ref"])] = body["label"]
    return found


def _hub_rows(account: Account, org: str) -> dict[str, dict[str, Any]]:
    answer = account.call("GET", f"/v1/orgs/{segment(org)}/devices")
    rows = answer.get("devices")
    if not isinstance(rows, list) or not all(isinstance(row, dict) and "device_id" in row for row in rows):
        raise unverified("response_invalid")
    return {str(row["device_id"]): {key: row.get(key) for key in HUB_FIELDS} for row in rows}


def _order(state: OrgState) -> list[str]:
    return sorted(state.workgroups, key=lambda wg: (wg != state.default_wg, wg))


def device_rows(account: Account, org: str) -> list[dict[str, Any]]:
    state = account.org_log(org).state
    platform = account.platform() if account.platform_root else None
    excluded = frozenset() if platform is None else platform.excluded(state)
    hub = _hub_rows(account, org)
    names = labels(account, org)
    devices = sorted(
        (p for p in state.principals.values() if p.kind == DEVICE and p.active),
        key=lambda p: (-p.joined_seq, p.id),
    )
    return [
        {
            "device_id": p.id,
            "label": names.get(p.id),
            "kind": "org" if p.platform_seq is None else "platform",
            "platform_seq": p.platform_seq,
            "decertified": p.id in excluded,
            "workgroups": [wg for wg in _order(state) if state.assigned(p.id, wg)],
            "organization": state.org_assigned(p.id),
            "hub": hub.get(p.id),
        }
        for p in devices
    ]


def device_list(account: Account, org: str | None) -> dict[str, Any]:
    orgs = [org] if org is not None else [str(item["org_id"]) for item in account.me().get("orgs", [])]
    return {"orgs": [{"org_id": found, "devices": device_rows(account, found)} for found in orgs]}


# --- unassign ---


def unassign_plan(device: str, wg: str) -> membership.Planner:
    def plan(log: VerifiedLog, me: str) -> membership.Fields | None:
        state = log.state
        membership.require_admin(state, me)
        if not state.assigned(device, wg):
            return None
        return [{"type": "device_unassign", "device": device, "wg": wg}]

    return plan


def device_unassign(account: Account, wg: str, device: str) -> dict[str, Any]:
    org = find_org(account, membership.require_id(wg))
    built = membership.submit(account, org, unassign_plan(membership.require_id(device), wg))
    return {**membership.changed(account, org, built, credentials=True), "wg_id": wg, "device_id": device}


def unassign_org(account: Account, org: str, device: str) -> dict[str, Any]:
    """`device_unassign_org` with the rotations it owes (one batch), then the follow-ups of a removal: the pending
    rotation and the credential-key check (the device held the credential keys of the members, ADR-075 section 5)."""
    result = device_unassign_org(account, org, device)
    result["pending_rotation"] = membership.follow_up(lambda: membership.rotate_pending(account, org))
    result["credentials"] = membership.follow_up(lambda: credkey.rotate_if_owed(account))
    return result


# --- rename ---


def _device_org(account: Account, device: str) -> str:
    found = [
        str(item["org_id"])
        for item in account.me().get("orgs", [])
        if getattr(account.org_log(str(item["org_id"])).state.member(device), "kind", None) == DEVICE
    ]
    if len(found) != 1:
        raise PlatformError("not_found") if not found else PlatformError("invalid_argument", reason="org_ambiguous")
    return found[0]


def device_rename(account: Account, device: str, name: str, org: str | None) -> dict[str, Any]:
    label = device_label(name)
    org = org if org is not None else _device_org(account, membership.require_id(device))
    log = account.org_log(org)
    state = log.state
    membership.require_admin(state, account.user_id)
    if getattr(state.member(device), "kind", None) != DEVICE:
        raise PlatformError("not_found")
    event = seal(account, org, (ORG, org), DEVICE_PROFILE, {"label": label}, ref=device)
    return {"org_id": org, "device_id": device, "label": label, "scope_seq": post_event(account, event)}


# --- invite ---


def device_invite(account: Account, org: str, wgs: list[str], hours: int) -> dict[str, Any]:
    """An org device invitation for `wgs` (the device screen's "add device"); the code is printed once."""
    chosen = sorted({membership.require_id(wg) for wg in wgs})
    if not chosen or not 1 <= hours <= EXPIRY_HOURS_MAX:
        raise PlatformError("invalid_argument")
    code = invite_format.generate_code()
    secrets = invite_format.invite_secrets(code)

    def attempt() -> dict[str, Any]:
        log = account.org_log(org)
        state = log.state
        membership.require_admin(state, account.user_id)
        if not all(state.writable(wg) for wg in chosen):
            raise PlatformError("invalid_argument")
        ring = account.keys(org)
        keys = []
        for scope, scope_id in [(ORG, org), *((WG, wg) for wg in chosen)]:
            epoch = state.epoch(scope, scope_id)
            key = ring.get((scope, scope_id, epoch))
            if key is None:
                raise PlatformError("hub_refused", reason="epoch_stale")
            keys.append({"scope": scope, "scope_id": scope_id, "epoch": epoch, "key": b64u_encode(key)})
        expires = format_time(datetime.now(UTC) + timedelta(hours=hours))
        fields: dict[str, object] = {
            "type": "invite",
            "invite_id": secrets.invite_id,
            "pk_inv": secrets.pk_inv,
            "kind": "device",
            "wgs": dict.fromkeys(chosen, "device"),
            "expires": expires,
        }
        try:
            built = build_batch(
                log, org=org, author=account.actor, identity=account.signer, fields=[fields], ts=now_text(), held={}
            )
        except CryptoError as error:
            raise crypto_refusal(error) from None
        names = account.names(org)
        package = {
            "org": org,
            "org_name": names.org or "",
            "wg_names": {wg: names.workgroups.get(wg, "") for wg in chosen},
            "founder": state.founder,
            "head": built.log.head.to_json(),
            "keys": keys,
            "hub_key": dict(cast(dict[str, Any], account.state["hub_key"])),
            "v": 1,
        }
        sealed = invite_format.seal_package(secrets, package)
        answer = account.call(
            "POST", f"/v1/orgs/{segment(org)}/invites", {"entry": built.entries[0], "package_ct": sealed}
        )
        if answer.get("invite_id") != secrets.invite_id or answer.get("seq") != built.log.head.seq:
            raise unverified("response_invalid")
        account.remember(org, built.log)
        return {"org_id": org, "invite_id": secrets.invite_id, "expires": expires, "seq": built.log.head.seq}

    result = with_retry(account, org, attempt)
    text = invite_format.format_code(code)
    return {
        **result,
        "wgs": chosen,
        "code": text,
        "enroll_command": f"agent-runner enroll --hub {account.origin} --code {text}",
        "secret_fields": ["code", "enroll_command"],
    }


# --- command line ---


def _list(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return device_list(account, args.org)


def _unassign(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    if args.org is not None:
        return unassign_org(account, args.org, args.device)
    return device_unassign(account, args.wg, args.device)


def _rename(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return device_rename(account, args.device, args.name, args.org)


def _invite(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    return device_invite(account, args.org, args.wg, args.expires_hours)


def register(tree: Tree) -> None:
    group = tree.group("device", "execution devices")
    sub = tree.command(
        group,
        "list",
        _list,
        "devices of the organizations (verified log, labels, presence)",
        example="agent-platform device list [--org <org_id>]",
        words="geräte rechner computer online anwesenheit devices machines presence",
        trim="first",
    )
    sub.add_argument("--org")
    sub = tree.command(
        group,
        "unassign",
        _unassign,
        "take a device off a workgroup or the organization (rotates the keys)",
        writes=True,
        example="agent-platform device unassign --wg <wg_id> --device <device_id>",
        words="abziehen gerät entziehen trennen arbeitsgruppe organisation detach remove machine organization",
    )
    target = sub.add_mutually_exclusive_group(required=True)
    target.add_argument("--wg", help="end the assignment to this workgroup")
    target.add_argument("--org", help="end the assignment to the whole organization")
    sub.add_argument("--device", required=True)
    sub = tree.command(
        group,
        "rename",
        _rename,
        "set a device's label (device.profile)",
        writes=True,
        example='agent-platform device rename --device <device_id> --name "Mac mini"',
        words="beschriften umbenennen gerät name bezeichnung label",
    )
    sub.add_argument("--device", required=True)
    sub.add_argument("--name", required=True)
    sub.add_argument("--org", help="the organization (default: the only one the device belongs to)")
    sub = tree.command(
        group,
        "invite",
        _invite,
        "invite an org device (prints the enrollment code once)",
        writes=True,
        example="agent-platform device invite --org <org_id> --wg <wg_id>",
        words="einbinden eigener rechner gerät einrichten hinzufügen enroll own computer add machine code",
    )
    sub.add_argument("--org", required=True)
    sub.add_argument("--wg", action="append", default=[], help="workgroup to assign, repeatable")
    sub.add_argument("--expires-hours", type=int, default=DEVICE_EXPIRY_HOURS)
