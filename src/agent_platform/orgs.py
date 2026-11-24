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

from agent_platform.account import Account, check_package_head, needs_platform, unverified
from agent_platform.batch import Built, build_batch, make_wrap
from agent_platform.errors import PlatformError
from agent_platform.hub import anon_request, segment
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto import identity as identity_format
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.encoding import b64u_decode, b64u_encode, body_json, format_time, parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import new_header, seal_event
from agent_runner.crypto.ids import is_uuid7, uuid7
from agent_runner.crypto.log.platform import PlatformState
from agent_runner.crypto.log.state import ADMIN, MEMBER, ORG, READ, WG, WRITE, OrgState
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


def seal_epoch(state: OrgState, scope: str, scope_id: str) -> int:
    """The epoch a new event of the scope is sealed under: the current one, unless the scope's rotation is owed
    (`pending`: a member that left by itself could still read it): `rotation_pending` until an admin or writer has
    rotated (`upkeep.prepare` does it before every writing command when the user may)."""
    if (scope, scope_id) in state.pending:
        raise PlatformError("rotation_pending", scope=scope, scope_id=scope_id)
    return state.epoch(scope, scope_id)


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
            # read only because the user is an admin of the org (ADR-058), not by an own grant
            "my_access_inherited": state.inherited(account.user_id, wg.id),
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
        **({"invites": invite_states(account, org, state)} if me is not None and me.role == "admin" else {}),
    }


def invite_states(account: Account, org: str, state: OrgState) -> list[dict[str, Any]]:
    """The user invitations with their state for the inviter (KOR-36): `open` (not opened yet as far as the hub
    knows), `opened` (someone looked the link up), `registered` (a new account was created with it, the join is still
    missing) or `accepted`. Listed are only invitations the verified log knows, not revoked and not expired (accepted
    ones until their expiry, so that "did they accept?" is answered); an open one must also be open in the hub's list,
    which is otherwise a hint for `opened` and `registered` only (03-api.md section 4.2; `opened_at` is missing on
    older hubs)."""
    answer = account.call("GET", f"/v1/orgs/{segment(org)}/invites")
    rows = {str(row.get("invite_id")): row for row in answer.get("invites", []) if isinstance(row, dict)}
    now = datetime.now(UTC)
    joined = {p.joined_seq: p.id for p in state.principals.values()}
    found: list[dict[str, Any]] = []
    for inv in sorted(state.invites.values(), key=lambda item: -item.seq):
        if inv.kind != "user" or inv.revoked_seq is not None or parse_time(inv.expires) <= now:
            continue
        row = rows.get(inv.invite_id, {})
        # an open invitation the hub does not list as open (used, revoked, unknown) is not shown (as in the web client)
        if inv.accepted_seq is None and row.get("state") != "open":
            continue
        opened = row.get("opened_at")
        if inv.accepted_seq is not None:
            status = "accepted"
        elif row.get("registered") is True:
            status = "registered"
        else:
            status = "opened" if opened is not None else "open"
        found.append(
            {
                "invite_id": inv.invite_id,
                "role": inv.role,
                "expires": inv.expires,
                "status": status,
                "opened_at": opened,
                "accepted_by": None if inv.accepted_seq is None else joined.get(inv.accepted_seq),
            }
        )
    return found


# --- org create ---


def org_create(account: Account, name: str, given_display_name: str | None) -> dict[str, Any]:
    return genesis(account, display_name(name), own_display_name(account, given_display_name))


def genesis(account: Account, org_name: str, member_name: str, platform_invite: str | None = None) -> dict[str, Any]:
    """A new organization with this user as its founder; with `platform_invite` the private organization of a
    registration through that platform invitation (04-crypto.md section 5.3)."""
    names = PRIVATE_NAMES[locale(account)]
    org, wg = uuid7(), uuid7()
    first = {"type": "genesis", "subject": account.user_id, "pub": account.identity.public(), "default_wg": wg}
    built = build_batch(
        None,
        org=org,
        author=account.user_id,
        identity=account.identity,
        fields=[{**first, "private": platform_invite is not None}],
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
    if platform_invite is not None:
        body["platform_invite_id"] = platform_invite
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


def org_invite(
    account: Account, org: str, role: str, wgs: dict[str, str], hours: int, *, message: str | None = None
) -> dict[str, Any]:
    if not 1 <= hours <= EXPIRY_HOURS_MAX:
        raise PlatformError("invalid_argument")
    try:
        if message is not None:
            agent_crypto.check_message(message)
    except CryptoError:
        raise PlatformError("invalid_argument") from None
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
        if message is not None:
            package["message"] = message
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
            platform = account.platform() if needs_platform(entries) else None
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
        result = {"org_id": org, "role": found.role, "org_name": package.get("org_name") or None}
        if "message" in package:
            result["message"] = package["message"]
        try:
            answer = account.call("POST", f"/v1/orgs/{segment(org)}/log", built.body(events))
            require_head(answer, built.log)
        except PlatformError:
            # an `accept` the hub stored although its answer got lost (also of an earlier attempt) or that won a
            # race: the membership says so and the verified log must hold this invitation accepted by this user
            if not _member(account, org):
                raise
            joined_log(account, org, secrets.invite_id, cast(dict[str, Any], package["head"]))
            return result
        account.remember(org, built.log)
        return result

    return with_retry(account, "", attempt)


def _member(account: Account, org: str) -> bool:
    try:
        return any(isinstance(item, dict) and item.get("org_id") == org for item in account.me().get("orgs", []))
    except PlatformError:
        return False


def joined_log(account: Account, org: str, invite: str, package_head: dict[str, Any]) -> VerifiedLog:
    """The org's log read as a member, extending the package head, with `invite` accepted by this user (`log_accept`
    otherwise), as the web's `joinedLog`."""
    account.forget(org)
    log = account.org_log(org)
    try:
        check_package_head(package_head, log.entries)
    except CryptoError as error:
        raise crypto_refusal(error) from None
    accepted = log.state.invites[invite].accepted_seq if invite in log.state.invites else None
    entry = None if accepted is None else cast(dict[str, Any], log.entries[accepted]["body"])
    if entry is None or entry.get("subject") != account.user_id or log.state.member(account.user_id) is None:
        raise PlatformError("hub_refused", reason="log_accept")
    return log


# --- direct admission (ADR-060) ---


def _proven(platform: PlatformState, answer: Mapping[str, Any], user: str | None, code: str | None) -> dict[str, Any]:
    """The identity answer of the hub, verified against the platform log (never trusted as such): the anchor at
    `platform_seq` proves `(user_id, pub)`; a typed account code must be the code of exactly these keys."""
    try:
        found = {
            "user_id": str(answer["user_id"]),
            "pub": {"kem": str(answer["pub"]["kem"]), "sig": str(answer["pub"]["sig"])},
            "platform_seq": answer["platform_seq"],
            "id_sig": str(answer["id_sig"]),
        }
        seq = found["platform_seq"]
        anchored = (
            isinstance(seq, int)
            and not isinstance(seq, bool)
            and (user is None or found["user_id"] == user)
            and platform.identity_anchored(seq, found["user_id"], found["pub"], found["id_sig"], current=True)
            and (code is None or identity_format.account_code(found["user_id"], found["pub"]) == code)
        )
    except (KeyError, TypeError, CryptoError):
        anchored = False
    if not anchored:
        raise unverified("identity_unproven")
    return found


def _identity_of(account: Account, platform: PlatformState, who: str) -> dict[str, Any]:
    """The proven identity of a user ID or of an account code."""
    if is_uuid7(who):
        return _proven(platform, account.call("GET", f"/v1/users/{segment(who)}/identity"), who, None)
    try:
        code = identity_format.parse_account_code(who)
    except CryptoError:
        raise PlatformError("invalid_argument") from None
    return _proven(platform, account.call("GET", f"/v1/users/lookup?code={code}"), None, code)


def org_admit(account: Account, org: str, who: str, role: str, wgs: dict[str, str]) -> dict[str, Any]:
    """Admit an existing account (user ID or account code) as member or admin with workgroup access, without any step
    of the admitted person (04-crypto.md section 5.13): the identities of both sides are verified against the platform
    log, then `admit` is appended with the wraps of the current org key, the default workgroup key and every listed
    workgroup key the user holds (the rest arrives by catch-up)."""
    if role not in (ADMIN, MEMBER):
        raise PlatformError("invalid_argument")
    platform = account.platform()
    subject = _identity_of(account, platform, who)
    own = _proven(
        platform, account.call("GET", f"/v1/users/{segment(account.user_id)}/identity"), account.user_id, None
    )
    if own["pub"] != account.identity.public():
        raise unverified("identity_mismatch")

    def attempt() -> Built:
        log = account.org_log(org)
        state = log.state
        if not state.is_admin(account.user_id):
            raise PlatformError("not_admin")
        if state.member(subject["user_id"]) is not None:
            raise PlatformError("invalid_argument", reason="already_member")
        granted = {wg: access for wg, access in wgs.items() if wg != state.default_wg}
        if any(not state.writable(wg) for wg in granted):
            raise PlatformError("invalid_argument")
        fields = {
            "type": "admit",
            "subject": subject["user_id"],
            "pub": subject["pub"],
            "platform_seq": subject["platform_seq"],
            "id_sig": subject["id_sig"],
            "by_platform_seq": own["platform_seq"],
            "by_id_sig": own["id_sig"],
            "role": role,
            "wgs": granted,
        }
        try:
            built = build_batch(
                log,
                org=org,
                author=account.user_id,
                identity=account.identity,
                fields=[fields],
                ts=now_text(),
                held=account.keys(org),
                platform=platform,
                by_catch_up=frozenset(granted),
            )
        except CryptoError as error:
            raise crypto_refusal(error) from None
        answer = account.call("POST", f"/v1/orgs/{segment(org)}/log", built.body([]))
        require_head(answer, built.log)
        account.remember(org, built.log)
        return built

    built = with_retry(account, org, attempt)
    code = identity_format.account_code(subject["user_id"], subject["pub"])
    return {
        "org_id": org,
        "user_id": subject["user_id"],
        "account_code": identity_format.format_account_code(code),
        "role": role,
        "wgs": dict(sorted(cast(dict[str, str], cast(dict[str, Any], built.entries[0]["body"])["wgs"]).items())),
        "seq": built.log.head.seq,
    }


def org_invitees(account: Account, org: str | None) -> dict[str, Any]:
    """The accounts registered through the user's own platform invitations (`GET /v1/me/invitees`, `loadInvitees`),
    newest first, each identity verified against the platform log like `org admit` (`admissible` false and no account
    code when it is missing or does not verify). With `--org` only the accounts that can be admitted there: proven and
    not a member yet (the list of the web's "Add an existing account")."""
    if org is not None and not is_uuid7(org):
        raise PlatformError("invalid_argument")
    state = None if org is None else account.org_log(org).state
    platform = account.platform()
    items = account.call("GET", "/v1/me/invitees").get("invitees")
    rows = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not isinstance(item.get("user_id"), str):
            continue
        if not isinstance(item.get("registered_at"), str):
            continue
        user = item["user_id"]
        try:
            proven: dict[str, Any] | None = _proven(platform, item["identity"], user, None)
        except (PlatformError, KeyError, TypeError):
            proven = None
        if state is not None and (proven is None or state.member(user) is not None):
            continue
        code = None if proven is None else identity_format.account_code(user, proven["pub"])
        rows.append(
            {
                "user_id": user,
                "registered_at": item["registered_at"],
                "admissible": proven is not None,
                "account_code": None if code is None else identity_format.format_account_code(code),
            }
        )
    return {"org_id": org, "invitees": rows}


# --- workgroups ---


def wg_list(account: Account, org: str) -> dict[str, Any]:
    return {"org_id": org, "workgroups": org_show(account, org)["workgroups"]}


def _append(
    account: Account,
    org: str,
    fields: list[dict[str, object]],
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
        epoch = seal_epoch(state, ORG, org)
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
