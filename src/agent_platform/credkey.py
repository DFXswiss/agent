"""Rotation of the user's credential key `CK` (11-agent-access.md section 8.4; 04-crypto.md sections 4.4, 5.10, 6.3;
03-api.md section 4.8; twin of the web client's job 5, `jobs/credential-rewrap.ts`, and `credentials/records.ts`).

A device is entitled to `CK` while it is an active device assigned to a workgroup in which the user has access
(`cred_readers` over the verified logs of the user's orgs, decertified platform devices excluded). A rotation is owed
when a device lost that eligibility:

- `credential_losses`: replaying the verified logs, a device that read `CK` before an entry and not after it, and that
  no log entitles now, lost it at the entry's signed `ts`; a decertified platform device at its newest
  `device_decertify`;
- `removal_losses`: devices entitled only through a log the run holds for an org that `GET /v1/me` no longer lists
  (the org the user just left), lost now;
- `holder_losses`: devices that `GET /v1/me` names as holders of the current `CK` (`cred_holders`, counted only when
  its epoch is the newest own `CK`) and that no verified log entitles, lost now.

An org whose log the hub served but that does not verify (`LogUnverifiable`) entitles nobody, like the web's blocked
org. An org whose log the hub refuses (any other `hub_refused`, e.g. a `403` or `500`) has no verdict yet, like an org
the web is still verifying: the check waits (`not_owed` with `reason: org_log_unavailable` and the `orgs`; `--force`
fails with the refusal), so a flaky or malicious hub cannot make the tool rotate `CK` away from entitled devices or
delete their records.

It is owed for a loss not clearly older than the start of the current epoch (`SKEW` margin; unknown or future:
owed). The start is the hub's `created_at` of the user's own self-wrap, which nothing signs: like the web client
(`spec-deviations.md`, 06 §11 job 5), a hub that dates the self-wrap after a loss makes the loss look handled and so
withholds a rotation the logs owe, as it can by refusing the rotation request; it cannot make the new key reach a
device the verified logs do not entitle. A loss is handled once: like the browser per sign-in, the tool keeps the keys
of the losses a rotation handled with its pairing (`handled_losses` in `state.json`, the newest `HANDLED_MAX`), so the
checks around every writing command (`upkeep`) do not rotate again for the same loss. `--force` rotates without a
loss.

The rotation: a fresh `CK_{e+1}`, wrapped for the user and every device entitled now; every record re-encrypted by the
user under it (same ID and kind, `version + 1`), all in one `POST /v1/me/credentials/rotate`. A record written by a
device that lost eligibility is deleted first (re-signing it would launder what that device wrote); any other record
that cannot be opened (signature, epoch, body as `contracts/credentials/<kind>.json` allows) blocks the rotation and
nothing is deleted. A `409 version_conflict` is retried once from the start.
"""

from __future__ import annotations

import argparse
import secrets
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from agent_platform.account import Account, LogUnverifiable, unverified
from agent_platform.batch import make_wrap
from agent_platform.commands import Context, Tree
from agent_platform.credentials import KINDS, valid_body
from agent_platform.errors import PlatformError
from agent_runner.crypto.credential import new_header, open_credential, seal_credential, verify_credential
from agent_runner.crypto.encoding import b64u_decode, body_json, format_time, parse_json, parse_time
from agent_runner.crypto.errors import CryptoError, fail
from agent_runner.crypto.log.platform import VerifiedPlatformLog
from agent_runner.crypto.log.state import DEVICE, OrgState
from agent_runner.crypto.log.verify import VerifiedLog
from agent_runner.crypto.log.wraps import CRED, WrapSlot, cred_readers

SKEW = timedelta(seconds=300)
HANDLED_MAX = 256


class BodyInvalid(Exception):
    """A record body that `contracts/credentials/<kind>.json` does not allow (the web's `params_invalid`)."""


# --- losses ---


@dataclass(frozen=True)
class Loss:
    """A device's loss of eligibility: when, and the key that marks it handled (the web's `Loss`)."""

    at: datetime
    key: str


Losses = dict[str, Loss]


@dataclass(frozen=True)
class OwnKey:
    epoch: int
    key: bytes
    created_at: datetime | None


def _excluded(platform: VerifiedPlatformLog | None, states: Iterable[OrgState]) -> list[str]:
    return [] if platform is None else [device for state in states for device in platform.state.excluded(state)]


def credential_losses(logs: Sequence[VerifiedLog], platform: VerifiedPlatformLog | None, owner: str) -> Losses:
    """Devices that lost eligibility in the verified logs and are not entitled now, with the time of the loss."""
    states = [log.state for log in logs]
    now = cred_readers(states, owner, _excluded(platform, states))
    losses: Losses = {}

    def note(device: str, at: datetime) -> None:
        if device not in now and (device not in losses or at > losses[device].at):
            losses[device] = Loss(at, f"{device} {format_time(at)}")

    for log in logs:
        for seq in range(1, len(log.states)):
            after = cred_readers([log.states[seq]], owner)
            for device in cred_readers([log.states[seq - 1]], owner) - after:
                note(device, parse_time(cast(dict[str, object], log.entries[seq]["body"])["ts"]))
    bodies = [] if platform is None else [cast(dict[str, object], entry["body"]) for entry in platform.entries]
    # an excluded platform device: its certification ended with a `device_decertify`, the newest one counts
    for device in cred_readers(states, owner) - now:
        ends = [body for body in bodies if body["type"] == "device_decertify" and body["device"] == device]
        note(device, parse_time(ends[-1]["ts"]))
    return losses


def removal_losses(logs: Sequence[VerifiedLog], member: set[str], owner: str, now: datetime) -> Losses:
    """Devices entitled only through logs of orgs the user no longer belongs to (lost now)."""
    remaining = cred_readers([log.state for log in logs if log.state.org in member], owner)
    return {
        device: Loss(now, f"{device} left {log.state.org}")
        for log in logs
        if log.state.org not in member
        for device in cred_readers([log.state], owner)
        if device != owner and device not in remaining
    }


def holder_losses(
    states: Sequence[OrgState], platform: VerifiedPlatformLog | None, holders: Sequence[str], owner: str, now: datetime
) -> Losses:
    """Holders of the current `CK` that no verified log of an org the user belongs to entitles (lost now)."""
    readers = cred_readers(states, owner, _excluded(platform, states))
    return {device: Loss(now, f"{device} holds") for device in holders if device != owner and device not in readers}


def rotation_owed(losses: Iterable[Loss], start: datetime | None, now: datetime, handled: Collection[str] = ()) -> bool:
    """A loss not handled yet and not clearly older than the start of the current epoch; an unknown or future start
    fails closed."""
    pending = [loss for loss in losses if loss.key not in handled]
    if not pending:
        return False
    if start is None or start > now + SKEW:
        return True
    return any(loss.at >= start - SKEW for loss in pending)


def handled_losses(account: Account) -> list[str]:
    """The keys of the losses a rotation of this pairing handled (`state.json`, `handled_losses`)."""
    found = account.state.get("handled_losses")
    return [key for key in found if isinstance(key, str)] if isinstance(found, list) else []


def remember_handled(account: Account, losses: Iterable[Loss]) -> None:
    """Keep the handled losses (the newest `HANDLED_MAX`) so that later checks do not rotate again for them."""
    known = handled_losses(account)
    keys = [*known, *(loss.key for loss in losses if loss.key not in known)][-HANDLED_MAX:]
    account.state["handled_losses"] = keys
    account.home.save(account.state)


def own_keys(account: Account) -> list[OwnKey]:
    """The user's verified `CK` epochs, newest first, with the hub's `created_at` of the self-wrap."""
    keys = account.cred_keys()
    created: dict[int, datetime | None] = {}
    for item in account.wraps():
        epoch = item.get("epoch")
        own = (item.get("scope"), item.get("scope_id"), item.get("recipient_id"), item.get("wrapped_by"))
        if own != (CRED, account.user_id, account.user_id, account.user_id) or epoch not in keys or epoch in created:
            continue
        try:
            created[cast(int, epoch)] = parse_time(item.get("created_at"))
        except CryptoError:
            created[cast(int, epoch)] = None
    return [OwnKey(epoch, keys[epoch], created.get(epoch)) for epoch in sorted(keys, reverse=True)]


def _holders(me: Mapping[str, Any]) -> tuple[int, list[str]] | None:
    found = me.get("cred_holders")
    if not isinstance(found, dict):
        return None
    epoch, devices = found.get("epoch"), found.get("devices")
    if not isinstance(epoch, int) or not isinstance(devices, list) or not all(isinstance(d, str) for d in devices):
        return None
    return epoch, devices


# --- records ---


def _listed(account: Account) -> list[str]:
    answer = account.call("GET", "/v1/me/credentials")
    items = answer.get("credentials")
    if not isinstance(items, list) or not all(isinstance(i, dict) and i.get("kind") in KINDS for i in items):
        raise unverified("response_invalid")
    return sorted({str(item["kind"]) for item in items}, key=KINDS.index)


def _fetch(account: Account, kind: str) -> dict[str, Any] | None:
    try:
        answer = account.call("GET", f"/v1/me/credentials/{kind}")
    except PlatformError as error:
        if error.fields.get("hub_error") == "not_found":
            return None
        raise
    record = answer.get("record")
    return record if isinstance(record, dict) else None


def device_key(
    account: Account, states: Sequence[OrgState], platform: VerifiedPlatformLog | None, author: str
) -> bytes:
    """The signing key of a device the verified logs entitle to the user's `CK` now (`requests/client.ts`
    `deviceKeys`); `author_not_writer` otherwise."""
    for state in states:
        found = state.member(author)
        if getattr(found, "kind", None) == DEVICE and author in cred_readers(
            [state], account.user_id, _excluded(platform, [state])
        ):
            return b64u_decode(cast(Any, found).sig, 32)
    fail("author_not_writer")


def open_record(
    account: Account, kind: str, record: dict[str, Any], keys: Sequence[OwnKey], sig_key: bytes
) -> tuple[dict[str, Any], object]:
    """The checked header and body of a record of `kind` (`openRecord`)."""
    header = record.get("header")
    if not isinstance(header, dict) or header.get("owner") != account.user_id or header.get("kind") != kind:
        fail("header_invalid")
    checked = verify_credential(record, sig_key)
    key = next((item.key for item in keys if item.epoch == checked["epoch"]), None)
    if key is None:
        fail("epoch_stale")
    body = parse_json(open_credential(key, record, sig_key))
    if not valid_body(kind, body):
        raise BodyInvalid(kind)
    return checked, body


# --- the rotation ---


@dataclass
class _View:
    """One snapshot: the verified logs of the user's orgs, the former ones held by the run, the platform log, the
    holders and the orgs whose log the hub refused (no verdict yet)."""

    member: list[VerifiedLog]
    former: list[VerifiedLog]
    platform: VerifiedPlatformLog | None
    holders: tuple[int, list[str]] | None
    unavailable: list[tuple[str, PlatformError]]

    @property
    def states(self) -> list[OrgState]:
        return [log.state for log in self.member]

    @property
    def all_states(self) -> list[OrgState]:
        return [log.state for log in (*self.member, *self.former)]


def _view(account: Account, former: Sequence[VerifiedLog]) -> _View:
    me = account.me()
    member: list[VerifiedLog] = []
    unavailable: list[tuple[str, PlatformError]] = []
    ids: set[str] = set()
    for item in me.get("orgs", []):
        org = str(item.get("org_id")) if isinstance(item, dict) else ""
        ids.add(org)
        try:
            member.append(account.org_log(org))
        except LogUnverifiable:
            continue  # served but does not verify: entitles nobody
        except PlatformError as error:
            if error.code != "hub_refused":
                raise
            unavailable.append((org, error))
    platform = account.platform_log() if account.platform_root else None
    kept = [log for log in former if log.state.org not in ids]
    return _View(member, kept, platform, _holders(me), unavailable)


def _kem(view: _View, account: Account, recipient: str) -> bytes:
    if recipient == account.user_id:
        return account.identity.kem_public()
    found = next(state.principals[recipient] for state in view.all_states if recipient in state.principals)
    return b64u_decode(found.kem, 32)


def _attempt(account: Account, force: bool, former: Sequence[VerifiedLog]) -> dict[str, Any]:
    account.refresh_wraps()
    view = _view(account, former)
    if view.unavailable:
        if force:
            raise view.unavailable[0][1]
        orgs = sorted(org for org, _ in view.unavailable)
        return {"status": "not_owed", "reason": "org_log_unavailable", "orgs": orgs, "lost_devices": []}
    now = datetime.now(UTC)
    user = account.user_id
    logs = [*view.member, *view.former]
    from_logs = [
        credential_losses(logs, view.platform, user),
        removal_losses(logs, {log.state.org for log in view.member}, user, now),
    ]
    holders = [] if view.holders is None else view.holders[1]
    from_holders = holder_losses(view.states, view.platform, holders, user, now)
    keys = own_keys(account)
    if not keys:
        if force:
            raise unverified("cred_key_missing")
        return {"status": "not_owed", "lost_devices": []}
    latest = keys[0]
    lost_holders = from_holders if view.holders is not None and view.holders[0] == latest.epoch else {}
    # a device may have lost eligibility in several ways (a log entry, leaving, holding the key): each loss counts
    groups = [*from_logs, lost_holders]
    if not force and not rotation_owed(_every(groups), latest.created_at, now, handled_losses(account)):
        return {"status": "not_owed", "lost_devices": _devices(groups)}
    opened: list[tuple[dict[str, Any], object]] = []
    dropped: list[str] = []
    blocked: list[str] = []
    for kind in _listed(account):
        record = _fetch(account, kind)
        if record is None:
            continue
        header = record.get("header")
        author = header.get("author") if isinstance(header, dict) else None
        try:
            sig_key = (
                account.identity.sig_public() if author == user else _author_key(account, view, author, lost_holders)
            )
            if sig_key is None:
                dropped.append(kind)
                continue
            opened.append(open_record(account, kind, record, keys, sig_key))
        except (CryptoError, BodyInvalid):
            blocked.append(kind)
    if blocked:
        return {"status": "blocked", "kinds": blocked}
    for kind in dropped:
        _delete(account, kind)
    epoch = latest.epoch + 1
    key = secrets.token_bytes(32)
    excluded = _excluded(view.platform, view.states)
    recipients = sorted(cred_readers(view.states, user, excluded))
    wraps = [
        make_wrap(account.identity, WrapSlot(CRED, user, epoch, r), _kem(view, account, r), key) for r in recipients
    ]
    ts = format_time(now)
    records = []
    for header, body in opened:
        fields = new_header(
            id=str(header["id"]),
            owner=user,
            kind=str(header["kind"]),
            version=cast(int, header["version"]) + 1,
            epoch=epoch,
            author=user,
            ts=ts,
        )
        records.append(seal_credential(key, fields, body_json(body), account.identity.sig))
    account.call("POST", "/v1/me/credentials/rotate", {"epoch": epoch, "wraps": wraps, "records": records})
    account.refresh_wraps()
    remember_handled(account, _every(groups))
    return {
        "status": "rotated",
        "epoch": epoch,
        "records": len(records),
        "dropped": dropped,
        "recipients": len(recipients),
        "lost_devices": _devices(groups),
    }


def _every(groups: Sequence[Losses]) -> list[Loss]:
    return [loss for found in groups for loss in found.values()]


def _devices(groups: Sequence[Losses]) -> list[str]:
    return sorted({device for found in groups for device in found})


def _author_key(account: Account, view: _View, author: object, lost_holders: Mapping[str, Loss]) -> bytes | None:
    """The key of a device author entitled now; None for a device that lost eligibility (its record is dropped);
    `author_not_writer` for any other author (the record blocks the rotation)."""
    try:
        return device_key(account, view.states, view.platform, str(author))
    except CryptoError:
        known = any(getattr(state.principals.get(str(author)), "kind", None) == DEVICE for state in view.all_states)
        if known or author in lost_holders:
            return None
        raise


def _delete(account: Account, kind: str) -> None:
    try:
        account.call("DELETE", f"/v1/me/credentials/{kind}")
    except PlatformError as error:
        if error.fields.get("hub_error") != "not_found":
            raise


def rotate_credentials(account: Account, *, force: bool = False, former: Sequence[VerifiedLog] = ()) -> dict[str, Any]:
    """The check and, when owed (or forced), the rotation; one retry after `version_conflict`."""
    try:
        return _attempt(account, force, former)
    except PlatformError as error:
        if error.fields.get("hub_error") != "version_conflict":
            raise
    return _attempt(account, force, former)


def rotate_if_owed(account: Account, former: Sequence[VerifiedLog] = ()) -> dict[str, Any]:
    """The check the browser's re-wrap job runs after a membership change (module docstring)."""
    return rotate_credentials(account, former=former)


def _rotate(account: Account, args: argparse.Namespace, context: Context) -> dict[str, Any]:
    result = rotate_credentials(account, force=args.force)
    if result["status"] == "blocked":
        raise PlatformError("credentials_blocked", kinds=result["kinds"])
    return result


def register(tree: Tree) -> None:
    group = tree.group("credentials", "credential records")
    sub = tree.command(
        group,
        "rotate",
        _rotate,
        "rotate the credential key when a device lost access (or --force)",
        example="agent-platform credentials rotate",
        words="zugangsschlüssel schlüssel erneuern verloren gerät key rotation lost renew",
    )
    sub.add_argument("--force", action="store_true", help="rotate even when no loss is known")
