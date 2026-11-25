"""Claude subscriptions logged in centrally (KOR-69, ADR-090; 03-api.md section 4.8, 11-agent-access.md section 8.4;
rules E-30, E-31, E-04).

Every subscription is a `claude_subscription` record of the user. The primary record serves the user's own sessions
and keeps the form the web client knows (it is written by `claude login` and `claude connect`); further
subscriptions are created by a login on a platform device and serve nothing until their use is switched on (default:
measure only). The token is created on the device and never printed here.

- `subscriptions list`: `GET /v1/me/subscriptions` (metadata and the newest reading), then every record (`GET
  /v1/subscriptions/{id}`), checked like every own record (owner, ID and kind of the header, author the user, the
  user's agent or a device the verified logs entitle to the user's `CK`, signature, the user's `CK` of the record's
  epoch, a body of `contracts/credentials/claude_subscription.json`). Per subscription: `name` (the primary one is
  `own sessions`), `note`, the `email` it signed in with, `status` (`logged_in`, `not_logged_in`: no token;
  `not_readable`: the usage endpoint refused the token, with `since`; `unverifiable` with its code), `use`, the last
  reading (`week_percent`, `week_resets_at`, `window_percent`, `window_resets_at`, `measured_at`, `device_id`) and,
  for a subscription that is not logged in, `next` with the command that logs it in.
- `subscriptions add --name [--note] [--automatic [--email]]`: a new record ID and `claude.login` with
  `subscription_id`, `name`, `note` (and `email`) to a device that announced the feature `subscriptions` (online
  first; `no_device` with `reason: device_outdated` when only older devices are there); the answer is the
  authorization link to open in the user's browser; the result names it as `next` with the command that sends the
  code (`claude login --request <id> --code`). With `--automatic` the device signs in through the user's mailbox
  credential and needs no browser.
- `subscriptions login --id`: the same for an existing subscription (log in again; name, note and use stay).
- `subscriptions rename --id --name`, `note --id (--note | --clear)`, `logout --id`: the record is opened in memory,
  changed (`logout` drops the token) and sealed again under the user's newest `CK` (version + 1, author the actor),
  `PUT /v1/subscriptions/{id}`; one retry after `version_conflict`/`epoch_stale`. The primary record is not changed
  here (`invalid_argument`, `reason: primary`). The provider offers no revocation of a setup token: `logout` answers
  `revoke_at` (where the user revokes it).
- `subscriptions use --id (--measure | [--pool] [--org <org_id>]...)`: which sessions besides the user's own may use
  the subscription (ADR-090 section 3): the sealed `use` of the record and, in the same `PUT`, its plaintext mirror
  for the hub (which tells the devices of those organizations); `--measure` switches every use off. `--pool` counts
  only for a user with the platform role `operator` (the organizations of the root-signed platform log). Every write
  of `rename`, `note`, `logout` and `use` sends the mirror of the use it sealed.
- `subscriptions remove --id`: `DELETE /v1/subscriptions/{id}` (`404` counts as done).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree
from agent_platform.devicereq import (
    WAIT_MAX_S,
    Keys,
    device_keys,
    finish,
    show_in_browser,
    submit,
    wait,
)
from agent_platform.errors import PlatformError
from agent_platform.eventio import rows_of, uuid_argument
from agent_platform.hub import segment
from agent_runner.crypto.credential import new_header, open_credential, seal_credential, verify_credential
from agent_runner.crypto.encoding import body_json, format_time, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7

KIND = "claude_subscription"
FEATURE = "subscriptions"
CONCURRENT = frozenset({"version_conflict", "epoch_stale"})
REVOKE_AT = "https://claude.ai/settings"
PRIMARY_NAME = "own sessions"
DEFAULT_WAIT_S = 30
TOKEN_RE = re.compile(r"^[!-~]{1,4096}$")
EMAIL_RE = re.compile(r"^[!-?A-~]{1,64}@[A-Za-z0-9-]{1,63}(\.[A-Za-z0-9-]{1,63}){0,8}$")
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LIMITS = {"name": 64, "note": 500}
BODY_KEYS = frozenset({"token", "email", "kind", "expires_at", "name", "note", "use"})
KINDS = ("setup_token", "login_token")
MAX_ORGS = 50


class Unverifiable(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def label(value: object, most: int) -> bool:
    """A name or note: 1..most characters, no control character."""
    return isinstance(value, str) and 0 < len(value) <= most and all(ord(char) >= 0x20 for char in value)


def valid_use(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != {"pool", "orgs"} or not isinstance(value["pool"], bool):
        return False
    orgs = value["orgs"]
    return (
        isinstance(orgs, list)
        and len(orgs) <= MAX_ORGS
        and all(isinstance(org, str) and UUID_RE.fullmatch(org) for org in orgs)
        and len(set(orgs)) == len(orgs)
    )


def valid_body(body: object) -> bool:
    """A body of `contracts/credentials/claude_subscription.json`."""
    if not isinstance(body, dict) or not set(body) <= BODY_KEYS or not {"email", "kind"} <= set(body):
        return False
    email = body["email"]
    return (
        body["kind"] in KINDS
        and (email is None or (isinstance(email, str) and len(email) <= 254 and bool(EMAIL_RE.fullmatch(email))))
        and ("token" not in body or (isinstance(body["token"], str) and bool(TOKEN_RE.fullmatch(body["token"]))))
        and ("expires_at" not in body or isinstance(body["expires_at"], str))
        and all(name not in body or label(body[name], most) for name, most in LIMITS.items())
        and ("use" not in body or valid_use(body["use"]))
    )


# --- the hub's list and records ---


def _meta(item: object) -> dict[str, Any]:
    texts = ("id", "owner_id", "created_at", "updated_at")
    if (
        not isinstance(item, dict)
        or not all(isinstance(item.get(name), str) for name in texts)
        or not isinstance(item.get("primary"), bool)
        or not isinstance(item.get("version"), int)
        or not isinstance(item.get("epoch"), int)
        or not (item.get("reading") is None or isinstance(item.get("reading"), dict))
    ):
        raise unverified("response_invalid")
    return item


def listed(account: Account) -> list[dict[str, Any]]:
    """`GET /v1/me/subscriptions` (`response_invalid` when malformed)."""
    return [_meta(item) for item in rows_of(account.call("GET", "/v1/me/subscriptions"), "subscriptions")]


def fetch(account: Account, record_id: str) -> dict[str, Any] | None:
    """`GET /v1/subscriptions/{id}`; None when the record is gone."""
    try:
        return account.call("GET", f"/v1/subscriptions/{segment(record_id)}")
    except PlatformError as error:
        if error.fields.get("hub_error") == "not_found":
            return None
        raise


def _author_key(account: Account, author: object) -> bytes:
    if isinstance(author, str) and author in (account.user_id, account.actor):
        return account.key_of(author)
    found = device_keys(account, author) if isinstance(author, str) else None
    if found is None:
        raise Unverifiable("author_not_writer")
    return found[1]


def open_own(
    account: Account, record_id: str, answer: dict[str, Any], keys: dict[int, bytes]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The checked header and body of an own subscription record (module docstring); `Unverifiable` otherwise."""
    record = answer.get("record")
    header = record.get("header") if isinstance(record, dict) else None
    if (
        answer.get("owner_id") != account.user_id
        or not isinstance(header, dict)
        or (header.get("id"), header.get("owner"), header.get("kind")) != (record_id, account.user_id, KIND)
    ):
        raise Unverifiable("header_invalid")
    key = _author_key(account, header.get("author"))
    try:
        checked = verify_credential(record, key)
        ck = keys.get(int(str(checked["epoch"])))
        if ck is None:
            raise Unverifiable("epoch_stale")
        body = parse_json(open_credential(ck, record, key))
    except CryptoError as error:
        raise Unverifiable(error.code) from None
    if not isinstance(body, dict) or not valid_body(body):
        raise Unverifiable("params_invalid")
    return checked, body


def _reading(meta: dict[str, Any]) -> dict[str, Any]:
    """The last reading as the overview of KOR-68 shows it: the weekly and the five-hour window."""
    reading = meta.get("reading")
    if not isinstance(reading, dict):
        return {"measured_at": None}
    windows = {
        item.get("window"): item
        for item in reading.get("windows") or []
        if isinstance(item, dict) and isinstance(item.get("window"), str)
    }
    week, window = windows.get("seven_day", {}), windows.get("five_hour", {})
    return {
        "week_percent": week.get("used_percent"),
        "week_resets_at": week.get("resets_at"),
        "window_percent": window.get("used_percent"),
        "window_resets_at": window.get("resets_at"),
        "measured_at": reading.get("fetched_at"),
        "device_id": reading.get("device_id"),
    }


def _entry(account: Account, meta: dict[str, Any], keys: dict[int, bytes]) -> dict[str, Any] | None:
    answer = fetch(account, meta["id"])
    if answer is None:
        return None
    entry: dict[str, Any] = {"id": meta["id"], "primary": meta["primary"], **_reading(meta)}
    try:
        _header, body = open_own(account, meta["id"], answer, keys)
    except Unverifiable as error:
        return {**entry, "status": "unverifiable", "code": error.code}
    reading = meta.get("reading")
    if "token" not in body:
        status = "not_logged_in"
    elif isinstance(reading, dict) and reading.get("unreadable") is True:
        status = "not_readable"
    else:
        status = "logged_in"
    entry.update(
        name=PRIMARY_NAME if meta["primary"] else body.get("name"),
        note=body.get("note"),
        email=body["email"],
        status=status,
        use="own_sessions" if meta["primary"] else body.get("use", {"pool": False, "orgs": []}),
    )
    if status == "not_logged_in":
        entry["next"] = f"agent-platform subscriptions login --id {meta['id']}"
    elif status == "not_readable":
        entry["since"] = reading["fetched_at"] if isinstance(reading, dict) else None
    return entry


def overview(account: Account) -> dict[str, Any]:
    """`subscriptions list` (module docstring), most room first (unknown last)."""
    found = listed(account)
    keys = account.cred_keys() if found else {}
    entries = [entry for meta in found if (entry := _entry(account, meta, keys)) is not None]
    entries.sort(key=lambda item: (item.get("week_percent") is None, item.get("week_percent") or 0.0, item["id"]))
    return {"subscriptions": entries}


# --- login on a device ---


def _device(account: Account, device: str | None) -> tuple[str, Keys]:
    """The named device, else an online one that announced `subscriptions` (module docstring)."""
    rows = rows_of(account.call("GET", "/v1/me/devices"), "devices")
    able: dict[str, bool] = {}
    for row in rows:
        features = row.get("features")
        if isinstance(row.get("device_id"), str) and isinstance(features, list) and FEATURE in features:
            able[row["device_id"]] = able.get(row["device_id"], False) or row.get("online") is True
    if device is not None:
        wanted = uuid_argument(device)
        if wanted not in able:
            raise PlatformError("no_device", reason="device_outdated")
        candidates = [wanted]
    else:
        candidates = sorted(able, key=lambda item: (not able[item], item))
    for candidate in candidates:
        keys = device_keys(account, candidate)
        if keys is not None:
            return candidate, keys
    raise PlatformError("no_device", reason="device_outdated" if rows else "device_not_assigned")


def _login(account: Account, args: Any, context: Context, body: dict[str, Any]) -> dict[str, Any]:
    if not 0 <= args.wait <= WAIT_MAX_S:
        raise PlatformError("invalid_argument")
    if args.email is not None:
        if not args.automatic or not EMAIL_RE.fullmatch(args.email) or len(args.email) > 254:
            raise PlatformError("invalid_argument", reason="email")
        body["email"] = args.email
    body["mode"] = "automatic" if args.automatic else "manual"
    prepared = submit(account, _device(account, args.device), "claude.login", body)
    found = wait(account, prepared, args.wait, frozenset({"authorize", "done", "failed"}))
    result = {"subscription_id": body["subscription_id"], **finish(prepared, found)}
    if found["step"] == "authorize":
        show_in_browser(context.environ, found["url"], "authorize the Claude login at")
        result["next"] = (
            f"the user opens {found['url']} and approves; then "
            f"`agent-platform claude login --request {prepared['id']} --code` with the shown code on stdin"
        )
    return result


def add(account: Account, args: Any, context: Context) -> dict[str, Any]:
    if not label(args.name, LIMITS["name"]) or (args.note is not None and not label(args.note, LIMITS["note"])):
        raise PlatformError("invalid_argument", reason="label")
    body: dict[str, Any] = {"subscription_id": uuid7(), "name": args.name}
    if args.note is not None:
        body["note"] = args.note
    return _login(account, args, context, body)


def login(account: Account, args: Any, context: Context) -> dict[str, Any]:
    record_id = uuid_argument(args.id)
    for meta in listed(account):
        if meta["id"] == record_id:
            if meta["primary"]:
                raise PlatformError("invalid_argument", reason="primary")
            return _login(account, args, context, {"subscription_id": record_id})
    raise PlatformError("not_found")


# --- changes by the owner ---


def change(account: Account, record_id: str, edit: Any) -> dict[str, Any]:
    """Open the own further subscription, apply `edit(body)` and seal it again (module docstring); its version."""
    for _attempt in range(2):
        answer = fetch(account, record_id)
        if answer is None:
            raise PlatformError("not_found")
        if answer.get("primary") is not False:
            raise PlatformError("invalid_argument", reason="primary")
        keys = account.cred_keys()
        try:
            header, body = open_own(account, record_id, answer, keys)
        except Unverifiable as error:
            raise unverified(error.code) from None
        edit(body)
        version = int(str(header["version"]))
        latest = max(keys)
        fields = new_header(
            id=record_id,
            owner=account.user_id,
            kind=KIND,
            version=version + 1,
            epoch=latest,
            author=account.actor,
            ts=format_time(datetime.now(UTC)),
        )
        record = seal_credential(keys[latest], fields, body_json(body), account.signer.sig)
        mirror = body.get("use", {"pool": False, "orgs": []})
        body.clear()
        put = {"record": record, "expected_version": version, "use": mirror}
        try:
            stored = account.call("PUT", f"/v1/subscriptions/{segment(record_id)}", put)
        except PlatformError as error:
            if error.fields.get("hub_error") in CONCURRENT:
                continue
            raise
        return {"subscription_id": record_id, "version": stored.get("version")}
    raise PlatformError("hub_refused", hub_error="version_conflict")


def rename(account: Account, args: Any, _context: Context) -> dict[str, Any]:
    if not label(args.name, LIMITS["name"]):
        raise PlatformError("invalid_argument", reason="label")
    return change(account, uuid_argument(args.id), lambda body: body.update(name=args.name))


def note(account: Account, args: Any, _context: Context) -> dict[str, Any]:
    if args.clear == (args.note is not None) or (args.note is not None and not label(args.note, LIMITS["note"])):
        raise PlatformError("invalid_argument", reason="label")

    def edit(body: dict[str, Any]) -> None:
        body.pop("note", None)
        if args.note is not None:
            body["note"] = args.note

    return change(account, uuid_argument(args.id), edit)


def logout(account: Account, args: Any, _context: Context) -> dict[str, Any]:
    result = change(account, uuid_argument(args.id), lambda body: body.pop("token", None))
    return {**result, "status": "not_logged_in", "revoke_at": REVOKE_AT}


def use(account: Account, args: Any, _context: Context) -> dict[str, Any]:
    orgs = sorted({uuid_argument(org) for org in args.org or []})
    if args.measure == bool(args.pool or orgs) or len(orgs) > MAX_ORGS:
        raise PlatformError("invalid_argument", reason="use")
    wanted = {"pool": bool(args.pool), "orgs": orgs}

    def edit(body: dict[str, Any]) -> None:
        body.pop("use", None)
        if not args.measure:
            body["use"] = dict(wanted)

    result = change(account, uuid_argument(args.id), edit)
    return {**result, "use": wanted}


def remove(account: Account, args: Any, _context: Context) -> dict[str, Any]:
    record_id = uuid_argument(args.id)
    try:
        account.call("DELETE", f"/v1/subscriptions/{segment(record_id)}")
    except PlatformError as error:
        if error.fields.get("hub_error") != "not_found":
            raise
    return {"subscription_id": record_id, "status": "removed", "revoke_at": REVOKE_AT}


def _list(account: Account, _args: Any, _context: Context) -> dict[str, Any]:
    return overview(account)


def _login_options(sub: Any) -> None:
    sub.add_argument("--automatic", action="store_true", help="sign in through the user's mailbox (no browser)")
    sub.add_argument("--email", help="with --automatic: the address to sign in with (default: the mailbox's)")
    sub.add_argument("--device", help="the platform device (default: an online one that can do it)")
    sub.add_argument("--wait", type=int, default=DEFAULT_WAIT_S, help="seconds to wait for the device (0-600)")


def register(tree: Tree) -> None:
    group = tree.group("subscriptions", "Claude subscriptions logged in on a platform device")
    tree.command(
        group,
        "list",
        _list,
        "every Claude subscription with status, use and the remaining quota (never a token)",
        example="agent-platform subscriptions list",
        words="abos abonnements kontingent übersicht rest woche messen subscriptions quota overview remaining",
    )
    sub = tree.command(
        group,
        "add",
        add,
        "add a subscription: log it in on a platform device (link for the user's browser, or --automatic)",
        writes=True,
        example='agent-platform subscriptions add --name "Team"',
        words="abo hinzufügen anmelden zentral einloggen add subscription login central",
        then="agent-platform claude login --request <request_id> --code",
    )
    sub.add_argument("--name", required=True, help="the label (up to 64 characters)")
    sub.add_argument("--note", help="what it is for (up to 500 characters)")
    _login_options(sub)
    sub = tree.command(
        group,
        "login",
        login,
        "log a subscription in again on a platform device (name, note and use stay)",
        writes=True,
        example="agent-platform subscriptions login --id <subscription_id>",
        words="erneut anmelden jetzt anmelden nicht angemeldet relogin log in again",
        then="agent-platform claude login --request <request_id> --code",
    )
    sub.add_argument("--id", required=True)
    _login_options(sub)
    sub = tree.command(
        group,
        "rename",
        rename,
        "change the label of a subscription",
        writes=True,
        example='agent-platform subscriptions rename --id <subscription_id> --name "Review"',
        words="umbenennen bezeichnung name label rename",
    )
    sub.add_argument("--id", required=True)
    sub.add_argument("--name", required=True)
    sub = tree.command(
        group,
        "note",
        note,
        "set or clear the note of a subscription",
        writes=True,
        example='agent-platform subscriptions note --id <subscription_id> --note "..."',
        words="notiz bemerkung wofür note remark",
    )
    sub.add_argument("--id", required=True)
    sub.add_argument("--note")
    sub.add_argument("--clear", action="store_true")
    sub = tree.command(
        group,
        "logout",
        logout,
        "log a subscription out (the token is removed; name, note and use stay)",
        writes=True,
        example="agent-platform subscriptions logout --id <subscription_id>",
        words="abmelden ausloggen widerrufen logout sign out revoke",
    )
    sub.add_argument("--id", required=True)
    sub = tree.command(
        group,
        "use",
        use,
        "which sessions may use a subscription: --measure (only measured), the operator pool or organizations",
        writes=True,
        example="agent-platform subscriptions use --id <subscription_id> --org <org_id>",
        words="verwendung freigeben nutzen messen organisation pool use share measure organization",
    )
    sub.add_argument("--id", required=True)
    sub.add_argument("--measure", action="store_true", help="only measure it (the default of a new subscription)")
    sub.add_argument("--pool", action="store_true", help="the operator's pool (platform role operator only)")
    sub.add_argument("--org", action="append", help="an organization whose sessions may use it (repeatable)")
    sub = tree.command(
        group,
        "remove",
        remove,
        "remove a subscription and its readings",
        writes=True,
        example="agent-platform subscriptions remove --id <subscription_id>",
        words="entfernen löschen remove delete",
    )
    sub.add_argument("--id", required=True)
