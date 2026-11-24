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
  its epoch is the newest own `CK`) and that no verified log entitles, lost now. An org whose log does not verify
  entitles nobody. The hub can cause a rotation this way, never prevent one the logs owe.

It is owed for a loss not clearly older than the start of the current epoch (the hub's `created_at` of the user's own
self-wrap, `SKEW` margin; unknown or future: owed). The browser remembers handled losses per sign-in; the tool runs one
command at a time, so a check shortly (within `SKEW`) after a rotation for the same loss rotates once more, which only
costs a re-encryption. `--force` rotates without a loss.

The rotation: a fresh `CK_{e+1}`, wrapped for the user and every device entitled now; every record re-encrypted by the
user under it (same ID and kind, `version + 1`), all in one `POST /v1/me/credentials/rotate`. A record written by a
device that lost eligibility is deleted first (re-signing it would launder what that device wrote); any other record
that cannot be opened (signature, epoch, body as `contracts/credentials/<kind>.json` allows) blocks the rotation and
nothing is deleted. A `409 version_conflict` is retried once from the start.
"""

from __future__ import annotations

import argparse
import re
import secrets
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from agent_platform.account import Account, unverified
from agent_platform.batch import make_wrap
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_runner.crypto.credential import KINDS, new_header, open_credential, seal_credential, verify_credential
from agent_runner.crypto.encoding import b64u_decode, body_json, format_time, parse_json, parse_time
from agent_runner.crypto.errors import CryptoError, fail
from agent_runner.crypto.log.platform import VerifiedPlatformLog
from agent_runner.crypto.log.state import DEVICE, OrgState
from agent_runner.crypto.log.verify import VerifiedLog
from agent_runner.crypto.log.wraps import CRED, WrapSlot, cred_readers

SKEW = timedelta(seconds=300)
KIND_ORDER = ("github", "anthropic_api_key", "claude_subscription", "mailbox")
_PRINTABLE = re.compile(r"[!-~]{1,4096}")
_HOST = re.compile(r"[A-Za-z0-9.-]{1,253}")
_IMAP_USER = re.compile(r"[ -~]{1,256}")
_EMAIL = re.compile(r"[!-?A-~]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){0,8}")
_LOGIN = re.compile(r"[A-Za-z0-9-]{1,39}")
_SCOPE = re.compile(r"[a-z0-9:_-]{1,64}")


# --- bodies (contracts/credentials/*.json, as `credentials/records.ts` checks them) ---


def _text(value: object, pattern: re.Pattern[str]) -> bool:
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _email(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 254 and _EMAIL.fullmatch(value) is not None


def _time(value: object) -> bool:
    try:
        parse_time(value)
    except CryptoError:
        return False
    return True


def _count(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _github(body: dict[str, Any]) -> bool:
    scopes = body.get("scopes")
    return (
        set(body) == {"token", "login", "user_id", "scopes", "created_at"}
        and _text(body["token"], _PRINTABLE)
        and _text(body["login"], _LOGIN)
        and _count(body["user_id"], 1, 2**53 - 1)
        and isinstance(scopes, list)
        and len(scopes) <= 64
        and all(_text(scope, _SCOPE) for scope in scopes)
        and _time(body["created_at"])
    )


def _mailbox(body: dict[str, Any]) -> bool:
    password = body.get("imap_password")
    return (
        set(body) == {"imap_host", "imap_port", "imap_user", "imap_password", "email"}
        and _text(body["imap_host"], _HOST)
        and _count(body["imap_port"], 1, 65535)
        and _text(body["imap_user"], _IMAP_USER)
        and isinstance(password, str)
        and 1 <= len(password) <= 1024
        and _email(body["email"])
    )


def _subscription(body: dict[str, Any]) -> bool:
    keys = {"token", "email", "kind", *(("expires_at",) if "expires_at" in body else ())}
    return (
        set(body) == keys
        and ("expires_at" not in body or _time(body["expires_at"]))
        and _text(body["token"], _PRINTABLE)
        and (body["email"] is None or _email(body["email"]))
        and body["kind"] in ("setup_token", "login_token")
    )


def valid_body(kind: str, body: object) -> bool:
    """Whether `body` is exactly what `contracts/credentials/<kind>.json` allows."""
    if not isinstance(body, dict):
        return False
    if kind == "anthropic_api_key":
        return set(body) == {"key"} and _text(body["key"], _PRINTABLE)
    if kind == "github":
        return _github(body)
    if kind == "mailbox":
        return _mailbox(body)
    return _subscription(body)


class BodyInvalid(Exception):
    """A record body that `contracts/credentials/<kind>.json` does not allow (the web's `params_invalid`)."""


# --- losses ---


@dataclass(frozen=True)
class OwnKey:
    epoch: int
    key: bytes
    created_at: datetime | None


def _excluded(platform: VerifiedPlatformLog | None, states: Iterable[OrgState]) -> list[str]:
    return [] if platform is None else [device for state in states for device in platform.state.excluded(state)]


def credential_losses(
    logs: Sequence[VerifiedLog], platform: VerifiedPlatformLog | None, owner: str
) -> dict[str, datetime]:
    """Devices that lost eligibility in the verified logs and are not entitled now, with the time of the loss."""
    states = [log.state for log in logs]
    now = cred_readers(states, owner, _excluded(platform, states))
    losses: dict[str, datetime] = {}

    def note(device: str, at: datetime) -> None:
        if device not in now and (device not in losses or at > losses[device]):
            losses[device] = at

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


def removal_losses(logs: Sequence[VerifiedLog], member: set[str], owner: str, now: datetime) -> dict[str, datetime]:
    """Devices entitled only through logs of orgs the user no longer belongs to (lost now)."""
    remaining = cred_readers([log.state for log in logs if log.state.org in member], owner)
    return {
        device: now
        for log in logs
        if log.state.org not in member
        for device in cred_readers([log.state], owner)
        if device != owner and device not in remaining
    }


def holder_losses(
    states: Sequence[OrgState], platform: VerifiedPlatformLog | None, holders: Sequence[str], owner: str, now: datetime
) -> dict[str, datetime]:
    """Holders of the current `CK` that no verified log of an org the user belongs to entitles (lost now)."""
    readers = cred_readers(states, owner, _excluded(platform, states))
    return {device: now for device in holders if device != owner and device not in readers}


def rotation_owed(losses: Mapping[str, datetime], start: datetime | None, now: datetime) -> bool:
    """A loss not clearly older than the start of the current epoch; an unknown or future start fails closed."""
    if not losses:
        return False
    if start is None or start > now + SKEW:
        return True
    return any(at >= start - SKEW for at in losses.values())


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
    return sorted({str(item["kind"]) for item in items}, key=KIND_ORDER.index)


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
    """One snapshot: the verified logs of the user's orgs, the former ones held by the run and the platform log."""

    member: list[VerifiedLog]
    former: list[VerifiedLog]
    platform: VerifiedPlatformLog | None
    holders: tuple[int, list[str]] | None

    @property
    def states(self) -> list[OrgState]:
        return [log.state for log in self.member]

    @property
    def all_states(self) -> list[OrgState]:
        return [log.state for log in (*self.member, *self.former)]


def _view(account: Account, former: Sequence[VerifiedLog]) -> _View:
    me = account.me()
    member: list[VerifiedLog] = []
    ids: set[str] = set()
    for item in me.get("orgs", []):
        org = str(item.get("org_id")) if isinstance(item, dict) else ""
        ids.add(org)
        try:
            member.append(account.org_log(org))
        except PlatformError as error:
            if error.code != "hub_refused":
                raise
    platform = account.platform_log() if account.platform_root else None
    kept = [log for log in former if log.state.org not in ids]
    return _View(member, kept, platform, _holders(me))


def _kem(view: _View, account: Account, recipient: str) -> bytes:
    if recipient == account.user_id:
        return account.identity.kem_public()
    found = next(state.principals[recipient] for state in view.all_states if recipient in state.principals)
    return b64u_decode(found.kem, 32)


def _attempt(account: Account, force: bool, former: Sequence[VerifiedLog]) -> dict[str, Any]:
    account.refresh_wraps()
    view = _view(account, former)
    now = datetime.now(UTC)
    user = account.user_id
    logs = [*view.member, *view.former]
    from_logs = {
        **credential_losses(logs, view.platform, user),
        **removal_losses(logs, {log.state.org for log in view.member}, user, now),
    }
    holders = [] if view.holders is None else view.holders[1]
    from_holders = holder_losses(view.states, view.platform, holders, user, now)
    keys = own_keys(account)
    if not keys:
        if force:
            raise unverified("cred_key_missing")
        return {"status": "not_owed", "lost_devices": []}
    latest = keys[0]
    lost_holders = from_holders if view.holders is not None and view.holders[0] == latest.epoch else {}
    losses = {**from_logs, **lost_holders}
    if not force and not rotation_owed(losses, latest.created_at, now):
        return {"status": "not_owed", "lost_devices": sorted(losses)}
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
    return {
        "status": "rotated",
        "epoch": epoch,
        "records": len(records),
        "dropped": dropped,
        "recipients": len(recipients),
        "lost_devices": sorted(losses),
    }


def _author_key(account: Account, view: _View, author: object, lost_holders: Mapping[str, datetime]) -> bytes | None:
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
    sub = tree.command(group, "rotate", _rotate, "rotate the credential key when a device lost access (or --force)")
    sub.add_argument("--force", action="store_true", help="rotate even when no loss is known")
