"""Organizations, invitations, workgroups and the platform device (11-agent-access.md section 8.4; 04-crypto.md
sections 4.6, 4.7, 5.2a, 5.3, 5.7, 5.9, 5.10; the web client's `account/genesis.ts`, `account/join.ts`,
`org/invites.ts`, `org/workgroups.ts`, `org/devices.ts`).

Every command reads the verified log of the org (`Account.org_log`) and the keys from verified wraps, builds its
entries with `batch.build_batch` (which runs the hub's checks on the result) and sends one request; the answer must
name the head the batch produces. A `409 seq_conflict` (another client appended first) re-reads the log and builds
again, at most `ATTEMPTS` times.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from agent_platform.account import Account, check_package_head, unverified
from agent_platform.batch import Built, build_batch, make_wrap
from agent_platform.errors import PlatformError
from agent_platform.hub import anon_request, segment
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.encoding import b64u_decode, b64u_encode, body_json, format_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import new_header, seal_event
from agent_runner.crypto.ids import is_uuid7, uuid7
from agent_runner.crypto.log.state import ORG, READ, WG, WRITE
from agent_runner.crypto.log.verify import VerifiedLog, verify_log
from agent_runner.crypto.log.wraps import CRED, WrapSlot

ATTEMPTS = 3
DISPLAY_NAME_MAX = 80
PRIVATE_NAMES = {"en": {"org": "Private", "wg": "Default"}, "de": {"org": "Privat", "wg": "Standard"}}
EXPIRY_HOURS_MAX = 720


def now_text() -> str:
    return format_time(datetime.now(UTC))


def display_name(text: str) -> str:
    name = text.strip()
    if not 1 <= len(name) <= DISPLAY_NAME_MAX:
        raise PlatformError("invalid_argument")
    return name


def with_retry[T](account: Account, org: str, attempt: Callable[[], T]) -> T:
    """`attempt` again after `seq_conflict` on a re-read log."""
    number = 1
    while True:
        try:
            return attempt()
        except PlatformError as error:
            if error.fields.get("hub_error") != "seq_conflict" or number == ATTEMPTS:
                raise
        account.forget(org)
        number += 1


def require_head(answer: Mapping[str, Any], log: VerifiedLog) -> None:
    if answer.get("head") != log.head.to_json():
        raise unverified("response_invalid")


def crypto_refusal(error: CryptoError) -> PlatformError:
    if error.code in ("log_rights", "forbidden"):
        return PlatformError("not_admin", reason=error.code)
    return PlatformError("hub_refused", reason=error.code)


def locale(account: Account) -> str:
    settings = account.me().get("settings")
    found = settings.get("locale") if isinstance(settings, dict) else None
    return found if found in PRIVATE_NAMES else "en"


def profile_event(
    account: Account, org: str, key: bytes, kind: str, body: Mapping[str, object], *, epoch: int, lh: int, **extra: str
) -> dict[str, object]:
    header = new_header(
        id=uuid7(),
        org=org,
        scope=ORG,
        scope_id=org,
        epoch=epoch,
        author=account.user_id,
        kind=kind,
        ts=now_text(),
        lh=lh,
        **extra,
    )
    return seal_event(key, header, body_json(dict(body)), account.identity.sig)


def own_display_name(account: Account, given: str | None) -> str:
    if given is not None:
        return display_name(given)
    found = account.display_name()
    if found is None:
        raise PlatformError("invalid_argument", reason="display_name_missing")
    return found


# --- org list / show ---


def org_list(account: Account) -> dict[str, Any]:
    orgs = []
    for item in account.me().get("orgs", []):
        org = str(item["org_id"])
        try:
            name = account.names(org).org
        except PlatformError:
            name = None
        orgs.append(
            {
                "org_id": org,
                "role": item.get("role"),
                "private": item.get("private"),
                "default_wg": item.get("default_wg"),
                "name": name,
            }
        )
    return {"orgs": orgs}


def org_show(account: Account, org: str) -> dict[str, Any]:
    log = account.org_log(org)
    state = log.state
    names = account.names(org)
    me = state.member(account.user_id)
    members = [
        {"principal_id": p.id, "kind": p.kind, "role": p.role, "name": names.members.get(p.id)}
        for p in sorted(state.principals.values(), key=lambda p: p.joined_seq)
        if p.active
    ]
    workgroups = [
        {
            "wg_id": wg.id,
            "name": names.workgroups.get(wg.id),
            "default": wg.default,
            "archived": wg.archived_seq is not None,
            "epoch": state.epoch(WG, wg.id),
            "my_access": state.wg_access(account.user_id, wg.id),
            "devices": sorted(device for device, assigned in state.assignments if assigned == wg.id),
        }
        for wg in sorted(state.workgroups.values(), key=lambda wg: wg.created_seq)
    ]
    invites = [
        {"invite_id": inv.invite_id, "kind": inv.kind, "role": inv.role, "expires": inv.expires, "seq": inv.seq}
        for inv in state.invites.values()
        if inv.revoked_seq is None and inv.accepted_seq is None
    ]
    return {
        "org_id": org,
        "name": names.org,
        "private": state.private,
        "default_wg": state.default_wg,
        "head": log.head.to_json(),
        "role": None if me is None else me.role,
        "members": members,
        "workgroups": workgroups,
        "open_invites": invites,
    }


# --- org create ---


def org_create(account: Account, name: str, given_display_name: str | None) -> dict[str, Any]:
    org_name = display_name(name)
    member_name = own_display_name(account, given_display_name)
    names = PRIVATE_NAMES[locale(account)]
    org, wg = uuid7(), uuid7()
    genesis = {"type": "genesis", "subject": account.user_id, "pub": account.identity.public(), "default_wg": wg}
    built = build_batch(
        None,
        org=org,
        author=account.user_id,
        identity=account.identity,
        fields=[{**genesis, "private": False}],
        ts=now_text(),
        held={},
    )
    key = built.new_keys[(ORG, org, 1)]
    events = [
        profile_event(account, org, key, "org.profile", {"name": org_name}, epoch=1, lh=0),
        profile_event(account, org, key, "wg.profile", {"name": names["wg"]}, epoch=1, lh=0, wg=wg),
        profile_event(
            account, org, key, "member.profile", {"display_name": member_name}, epoch=1, lh=0, ref=account.user_id
        ),
    ]
    body = {"genesis": built.entries[0], "wraps": built.wraps, "events": events}
    answer = account.call("POST", "/v1/orgs", body)
    if answer.get("org_id") != org or answer.get("default_wg") != wg:
        raise unverified("response_invalid")
    account.remember(org, built.log)
    return {"org_id": org, "default_wg": wg, "name": org_name}


# --- invitations ---


def parse_wgs(values: list[str]) -> dict[str, str]:
    """`--wg <id>[:read|write]` (default write)."""
    found: dict[str, str] = {}
    for value in values:
        wg, _, access = value.partition(":")
        access = access or WRITE
        if not is_uuid7(wg) or access not in (READ, WRITE):
            raise PlatformError("invalid_argument")
        found[wg] = access
    return found


def org_invite(account: Account, org: str, role: str, wgs: dict[str, str], hours: int) -> dict[str, Any]:
    if not 1 <= hours <= EXPIRY_HOURS_MAX:
        raise PlatformError("invalid_argument")
    code = invite_format.generate_code()
    secrets = invite_format.invite_secrets(code)

    def attempt() -> dict[str, Any]:
        log = account.org_log(org)
        state = log.state
        if not state.is_admin(account.user_id):
            raise PlatformError("not_admin")
        invited = {wg: access for wg, access in wgs.items() if wg != state.default_wg}
        if any(wg not in state.workgroups or not state.writable(wg) for wg in invited):
            raise PlatformError("invalid_argument")
        expires = format_time(datetime.now(UTC) + timedelta(hours=hours))
        fields = {
            "type": "invite",
            "invite_id": secrets.invite_id,
            "pk_inv": secrets.pk_inv,
            "kind": "user",
            "role": role,
            "wgs": invited,
            "expires": expires,
        }
        ring = account.keys(org)
        package_wgs = sorted({state.default_wg, *invited})
        keys = []
        for scope, scope_id in [(ORG, org), *((WG, wg) for wg in package_wgs)]:
            epoch = state.epoch(scope, scope_id)
            key = ring.get((scope, scope_id, epoch))
            if key is None:
                raise PlatformError("hub_refused", reason="epoch_stale")
            keys.append({"scope": scope, "scope_id": scope_id, "epoch": epoch, "key": b64u_encode(key)})
        built = build_batch(
            log, org=org, author=account.user_id, identity=account.identity, fields=[fields], ts=now_text(), held={}
        )
        names = account.names(org)
        package = {
            "org": org,
            "org_name": names.org or "",
            "wg_names": {wg: names.workgroups.get(wg, "") for wg in package_wgs},
            "founder": state.founder,
            "head": built.log.head.to_json(),
            "keys": keys,
            "hub_key": dict(account.state["hub_key"]),
            "v": 1,
        }
        sealed = invite_format.seal_package(secrets, package)
        answer = account.call(
            "POST", f"/v1/orgs/{segment(org)}/invites", {"entry": built.entries[0], "package_ct": sealed}
        )
        if answer.get("invite_id") != secrets.invite_id or answer.get("seq") != built.log.head.seq:
            raise unverified("response_invalid")
        account.remember(org, built.log)
        return {"invite_id": secrets.invite_id, "expires": expires, "seq": built.log.head.seq}

    result = with_retry(account, org, attempt)
    link = f"{account.origin}/invite#{invite_format.format_code(code)}"
    return {**result, "role": role, "invite_link": link, "secret_fields": ["invite_link"]}


def parse_link(text: str) -> bytes:
    """The code of `<origin>/invite#<code>[&...]` (or the bare code)."""
    fragment = text.strip().split("#", 1)[-1]
    try:
        return invite_format.parse_code(fragment.split("&", 1)[0])
    except CryptoError:
        raise PlatformError("invalid_argument") from None


def org_join(account: Account, link: str, given_display_name: str | None) -> dict[str, Any]:
    """Accept an org invitation as the existing user (04-crypto.md section 5.2a)."""
    secrets = invite_format.invite_secrets(parse_link(link))
    member_name = own_display_name(account, given_display_name)

    def attempt() -> dict[str, Any]:
        lookup = anon_request(account.origin, "POST", "/v1/invites/lookup", {"invite_id": secrets.invite_id})
        try:
            package = invite_format.open_package(secrets, str(lookup.get("package_ct")))
            org = str(package["org"])
            entries = cast(list[dict[str, object]], lookup.get("log"))
            if not isinstance(entries, list):
                raise CryptoError("log_empty")
            check_package_head(cast(dict[str, Any], package["head"]), entries)
            platform = account.platform() if _platform_needed(entries) else None
            log = verify_log(entries, str(package["founder"]), platform=platform)
            if log.state.org != org or lookup.get("log_head") != log.head.to_json():
                raise CryptoError("log_chain")
            found = log.state.invites.get(secrets.invite_id)
            if found is None or found.pk_inv != secrets.pk_inv or found.kind != "user":
                raise CryptoError("invite_not_in_log")
            if found.revoked_seq is not None or found.accepted_seq is not None:
                raise CryptoError("invite_consumed")
            ring = {
                (str(k["scope"]), str(k["scope_id"]), int(k["epoch"])): b64u_decode(k["key"], 32)
                for k in cast(list[dict[str, Any]], package["keys"])
            }
            accept = {
                "type": "accept",
                "invite_id": secrets.invite_id,
                "subject": account.user_id,
                "kind": "user",
                "pub": account.identity.public(),
            }
            built = build_batch(
                log,
                org=org,
                author=account.user_id,
                identity=account.identity,
                fields=[accept],
                ts=now_text(),
                held=ring,
                platform=platform,
                invite=secrets.signing,
            )
        except CryptoError as error:
            raise crypto_refusal(error) from None
        epoch = built.log.state.epoch(ORG, org)
        key = ring.get((ORG, org, epoch))
        events = (
            []
            if key is None
            else [
                profile_event(
                    account,
                    org,
                    key,
                    "member.profile",
                    {"display_name": member_name},
                    epoch=epoch,
                    lh=built.log.head.seq,
                    ref=account.user_id,
                )
            ]
        )
        answer = account.call("POST", f"/v1/orgs/{segment(org)}/log", built.body(events))
        require_head(answer, built.log)
        account.remember(org, built.log)
        return {"org_id": org, "role": found.role, "org_name": package.get("org_name") or None}

    return with_retry(account, "", attempt)


def _platform_needed(entries: list[dict[str, object]]) -> bool:
    return any(
        isinstance(entry.get("body"), dict)
        and cast(dict[str, object], entry["body"]).get("type") == "device_assign"
        and "platform_seq" in cast(dict[str, object], entry["body"])
        for entry in entries
    )


# --- workgroups ---


def wg_list(account: Account, org: str) -> dict[str, Any]:
    return {"org_id": org, "workgroups": org_show(account, org)["workgroups"]}


def _append(
    account: Account,
    org: str,
    fields: list[dict[str, object]],
    history: bool = False,
    events_of: Callable[[Built], list[dict[str, object]]] | None = None,
) -> Built:
    def attempt() -> Built:
        log = account.org_log(org)
        if not log.state.is_admin(account.user_id):
            raise PlatformError("not_admin")
        platform = account.platform() if any("platform_seq" in item for item in fields) else None
        try:
            built = build_batch(
                log,
                org=org,
                author=account.user_id,
                identity=account.identity,
                fields=fields,
                ts=now_text(),
                held=account.keys(org),
                platform=platform,
                history=history,
            )
        except CryptoError as error:
            raise crypto_refusal(error) from None
        events = [] if events_of is None else events_of(built)
        answer = account.call("POST", f"/v1/orgs/{segment(org)}/log", built.body(events))
        require_head(answer, built.log)
        account.remember(org, built.log)
        return built

    return with_retry(account, org, attempt)


def wg_create(account: Account, org: str, name: str) -> dict[str, Any]:
    wg_name = display_name(name)
    wg = uuid7()

    def events(built: Built) -> list[dict[str, object]]:
        state = built.log.state
        epoch = state.epoch(ORG, org)
        key = {**account.keys(org), **built.new_keys}.get((ORG, org, epoch))
        if key is None:
            raise PlatformError("hub_refused", reason="epoch_stale")
        lh = built.log.head.seq
        return [profile_event(account, org, key, "wg.profile", {"name": wg_name}, epoch=epoch, lh=lh, wg=wg)]

    built = _append(account, org, [{"type": "wg_create", "wg": wg, "epoch": 1}], events_of=events)
    return {"org_id": org, "wg_id": wg, "name": wg_name, "seq": built.log.head.seq}


def find_org(account: Account, wg: str) -> str:
    """The org of a workgroup the user belongs to."""
    for item in account.me().get("orgs", []):
        org = str(item["org_id"])
        if wg in account.org_log(org).state.workgroups:
            return org
    raise PlatformError("no_org")


def wg_grant(account: Account, wg: str, user: str, access: str, history: bool) -> dict[str, Any]:
    if not is_uuid7(user) or access not in (READ, WRITE):
        raise PlatformError("invalid_argument")
    org = find_org(account, wg)
    entry: dict[str, object] = {"type": "grant", "subject": user, "wg": wg, "access": access}
    built = _append(account, org, [entry], history=history)
    return {"org_id": org, "wg_id": wg, "user_id": user, "access": access, "seq": built.log.head.seq}


# --- platform device ---


def device_assign(account: Account, wg: str) -> dict[str, Any]:
    org = find_org(account, wg)
    log = account.org_log(org)
    if not log.state.is_admin(account.user_id):
        raise PlatformError("not_admin")
    platform = account.platform()
    if not platform.platform_access(org, log.state.private):
        raise PlatformError("hub_refused", reason="platform_access_denied")
    answer = account.call("GET", f"/v1/workgroups/{segment(wg)}/device-candidates")
    candidates = answer.get("candidates")
    if not isinstance(candidates, list):
        raise unverified("response_invalid")
    chosen: tuple[str, int] | None = None
    for item in candidates:
        if not isinstance(item, dict):
            continue
        device, seq = item.get("device_id"), item.get("platform_seq")
        cert = platform.certification(seq) if isinstance(seq, int) and not isinstance(seq, bool) else None
        if cert is None or cert.device != device or not platform.valid(cert.seq):
            continue
        if (str(device), wg) in log.state.assignments:
            continue
        chosen = (str(device), cert.seq)
        break
    if chosen is None:
        raise PlatformError("no_device")
    device, seq = chosen
    built = _append(account, org, [{"type": "device_assign", "device": device, "wg": wg, "platform_seq": seq}])
    cred = _cred_wrap(account, device, built.log.state.principals[device].kem)
    return {"org_id": org, "wg_id": wg, "device_id": device, "seq": built.log.head.seq, "cred_wrap": cred}


def _cred_wrap(account: Account, device: str, kem: str) -> bool:
    """Wrap the user's current `CK` for the newly assigned device (W2), `POST /v1/wraps`."""
    try:
        epoch, key = account.cred_key()
    except PlatformError:
        print("agent-platform: no verified credential key to share with the device", file=sys.stderr)
        return False
    record = make_wrap(account.identity, WrapSlot(CRED, account.user_id, epoch, device), b64u_decode(kem, 32), key)
    account.call("POST", "/v1/wraps", {"wraps": [record]})
    return True
