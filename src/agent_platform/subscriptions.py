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
  `relogin`: the measuring device could not renew it, KOR-71; `not_readable`: the usage endpoint refused the token,
  with `since`; `unverifiable` with its code), `use`, `login` (the kind: `oauth_login`, `setup_token`, `login_token`),
  `expires_at` of the access token, `plan` (`type`, `tier`) when the login reported it, the last reading
  (`week_percent`, `week_resets_at`, `window_percent`, `window_resets_at`, `measured_at`, `device_id`) and, for a
  further subscription that is not logged in, must log in again or is not readable, `next` with the command that
  logs it in.
- `subscriptions add --name [--note] [--automatic [--email]]`: a new record ID and `claude.login` with
  `subscription_id`, `name`, `note` (and `email`) to a device that announced the feature `subscriptions` (online
  first; `no_device` with `reason: device_outdated` when only older devices are there); the answer is the
  authorization link to open in the user's browser; the result names it as `next` with the command that sends the
  code (`claude login --request <id> --code`). With `--automatic` the device signs in through the user's mailbox
  credential and needs no browser.
- `subscriptions login --id`: the same for an existing subscription (log in again; name, note and use stay).
- `subscriptions rename --id --name`, `note --id (--note | --clear)`, `logout --id`: the record is opened in memory,
  changed (`logout` drops the access and the refresh token, so the device renews nothing) and sealed again under
  the user's newest `CK` (version + 1, author the actor),
  `PUT /v1/subscriptions/{id}`; one retry after `version_conflict`/`epoch_stale`. The primary record is not changed
  here (`invalid_argument`, `reason: primary`). The provider offers no revocation of a setup token: `logout` answers
  `revoke_at` (where the user revokes it).
- `subscriptions use --id (--measure | [--pool] [--org <org_id>]...)`: which sessions besides the user's own may use
  the subscription (ADR-090 section 3): the sealed `use` of the record and, in the same `PUT`, its plaintext mirror
  for the hub (which tells the devices of those organizations); `--measure` switches every use off. `--pool` counts
  only for a user with the platform role `operator` (the organizations of the root-signed platform log). Every write
  of `rename`, `note`, `logout` and `use` sends the mirror of the use it sealed.
- `subscriptions remove --id`: `DELETE /v1/subscriptions/{id}` (`404` counts as done).
- `subscriptions usage [--period five_hour|week|range] [--since] [--until]` (ADR-102): `GET
  /v1/me/subscriptions/usage` - per subscription the span, the measured window, the total (own subscriptions only) and
  per person requests, tokens (input, output, cache read, cache write), sessions, `token_share` and
  `estimated_percent` (an estimate, `estimate` says why); `you` marks the caller. Own subscriptions carry `name` and
  `email` as `list` shows them; another owner's subscription is named by `owner_id` and lists only the caller's row.
  `dfx_access` names the command with the consumption per person through DFX access (`billing`, ADR-083).

Side by side, with an own mailbox and registration (KOR-73, ADR-100; rule E-33), only with a device that announced
`subscriptions.parallel` (else `no_device`, `reason: device_outdated`); every `claude.login` to such a device names its
subscription as the plaintext `slot`, so logins of different subscriptions do not replace each other:
- `subscriptions add` with `--mailbox-host`, `--mailbox-user`, `--mailbox-email` and the password on stdin
  (`--mailbox-password-stdin`, never on the command line), `--org` (use) or the registration (`--register-name`,
  `--birth-date`, `--consent terms|age|marketing`, only what the user gave) first creates the record without a token
  (`PUT /v1/subscriptions/{id}`, version 0: `email` the address to sign in with or null, `kind` `oauth_login`, name,
  note, `mailbox`, `use` and its mirror), then sends `claude.login {mode: "automatic", subscription_id, email?,
  account?}`; a mailbox or a registration means the automatic login.
- `subscriptions add-many --file F` (`-` = stdin): a JSON list of entries with the same fields (`name`, `note`,
  `email`, `automatic`, `mailbox`, `orgs`, `account`), all checked before anything is sent; then every login starts at
  once and the tool follows them up to `--wait` seconds: one result per subscription with its step and `next`.
- `subscriptions mailbox --id (... | --clear | --check)`: set or remove the own mailbox of a further subscription, or
  let a device check it (`mailbox.check {subscription_id}`); `list` shows its address (`mailbox`), never the
  password. The new login of a record with its own mailbox goes only to such a device as well.
- `subscriptions check --request [--out FILE] [--click X,Y | --relay]`: the state of one login; the newest picture of a
  check for humans (`verify`) is written to FILE as JPEG, `--click` sends the click of a **person** into that picture
  (fractions of its width and height; only when the tool signs as the user itself - an own-identity agent is refused
  with `human_only`, the device would ignore its click), `--relay` gives the automation up (the device answers the
  link). The check for humans is done by a person; the platform only carries the click.
- Every `claude.login` root to such a device says `extended: true`: the tool understands `verify`, `authorize.reason`
  and the new progress stages (`devicereq`).
"""

from __future__ import annotations

import base64
import json
import re
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from agent_platform import devicereq
from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree
from agent_platform.credentials import valid_mailbox
from agent_platform.devicereq import (
    WAIT_MAX_S,
    Keys,
    device_keys,
    finish,
    progress,
    read,
    resume,
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
KINDS = ("setup_token", "login_token", "oauth_login")
LABEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
SCOPE_RE = re.compile(r"^[a-z_:]{1,64}$")
RELOGIN = "renewal_refused"
MAX_ORGS = 50
USAGE_PERIODS = ("five_hour", "week", "range")
TIME_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$")
ESTIMATE_NOTE = (
    "estimated_percent is an estimate: the provider measures the quota per subscription only; each person's share of"
    " the tokens on the subscription in the window times the measured use of the window"
)
PARALLEL = "subscriptions.parallel"
"""The device keys logins by slot, reads the record's mailbox and knows `account`, `verify`, `click` and `relay`."""
CONSENTS = ("terms", "age", "marketing")
DATE_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])$")
ENTRY_FIELDS = frozenset({"name", "note", "email", "automatic", "mailbox", "orgs", "account"})
ACCOUNT_FIELDS = frozenset({"name", "birth_date", "consents"})
MAX_MANY = 40
"""At most this many entries of `add-many` (the subscriptions a user may hold)."""
MAX_FILE = 1024 * 1024
STEPS = frozenset({"authorize", "verify", "done", "failed"})
Target = tuple[str, Keys, bool]
"""A device for logins: its ID, its keys and whether it announced `subscriptions.parallel`."""


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


def _valid_login(body: dict[str, Any]) -> bool:
    """The fields of a full login (KOR-71, ADR-092), each optional."""
    scopes = body.get("scopes", [])
    return (
        all(
            name not in body or (isinstance(body[name], str) and bool(TOKEN_RE.fullmatch(body[name])))
            for name in ("token", "refresh_token")
        )
        and isinstance(scopes, list)
        and all(isinstance(item, str) and SCOPE_RE.fullmatch(item) for item in scopes)
        and all(
            name not in body or (isinstance(body[name], str) and bool(LABEL_RE.fullmatch(body[name])))
            for name in ("subscription_type", "rate_limit_tier")
        )
        and body.get("relogin", RELOGIN) == RELOGIN
    )


def valid_body(body: object) -> bool:
    """A body of `contracts/credentials/claude_subscription.json`. Fields this version does not know are kept as they
    are (a later version may add some; they are sealed again unchanged)."""
    if not isinstance(body, dict) or not {"email", "kind"} <= set(body):
        return False
    email = body["email"]
    return (
        body["kind"] in KINDS
        and (email is None or (isinstance(email, str) and len(email) <= 254 and bool(EMAIL_RE.fullmatch(email))))
        and _valid_login(body)
        and ("expires_at" not in body or isinstance(body["expires_at"], str))
        and all(name not in body or label(body[name], most) for name, most in LIMITS.items())
        and ("use" not in body or valid_use(body["use"]))
        and ("mailbox" not in body or (isinstance(body["mailbox"], dict) and valid_mailbox(body["mailbox"])))
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


def _plan(body: dict[str, Any]) -> dict[str, Any] | None:
    """The plan the full login reported (`subscription_type`, `rate_limit_tier`), None when unknown."""
    if "subscription_type" not in body and "rate_limit_tier" not in body:
        return None
    return {"type": body.get("subscription_type"), "tier": body.get("rate_limit_tier")}


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
        status = "relogin" if body.get("relogin") == RELOGIN else "not_logged_in"
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
        login=body["kind"],
        expires_at=body.get("expires_at"),
        plan=_plan(body),
        mailbox=body["mailbox"]["email"] if "mailbox" in body else None,
    )
    if status != "logged_in" and not meta["primary"]:
        entry["next"] = f"agent-platform subscriptions login --id {meta['id']}"
    if status == "not_readable":
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


def _device(account: Account, device: str | None, *, parallel: bool = False) -> Target:
    """The named device, else an online one that announced `subscriptions` (with `parallel`: also
    `subscriptions.parallel`), preferring one that runs logins side by side (module docstring)."""
    rows = rows_of(account.call("GET", "/v1/me/devices"), "devices")
    able: dict[str, tuple[bool, bool]] = {}
    for row in rows:
        features = row.get("features")
        if not isinstance(row.get("device_id"), str) or not isinstance(features, list) or FEATURE not in features:
            continue
        if parallel and PARALLEL not in features:
            continue
        online, side = able.get(row["device_id"], (False, False))
        able[row["device_id"]] = (online or row.get("online") is True, side or PARALLEL in features)
    if device is not None:
        wanted = uuid_argument(device)
        if wanted not in able:
            raise PlatformError("no_device", reason="device_outdated")
        candidates = [wanted]
    else:
        candidates = sorted(able, key=lambda item: (not able[item][0], not able[item][1], item))
    for candidate in candidates:
        keys = device_keys(account, candidate)
        if keys is not None:
            return candidate, keys, able[candidate][1]
    raise PlatformError("no_device", reason="device_outdated" if rows else "device_not_assigned")


def _shown(found: dict[str, Any]) -> dict[str, Any]:
    """A step as printed: the picture of a check for humans only by its number (`check --out` writes it)."""
    return {"step": "verify", "frame": found["frame"]} if found["step"] == "verify" else found


def _next(request_id: str, found: dict[str, Any]) -> str | None:
    if found["step"] == "authorize":
        return (
            f"the user opens {found['url']} and approves; then "
            f"`agent-platform claude login --request {request_id} --code` with the shown code on stdin"
        )
    if found["step"] == "verify":
        return (
            f"a check for humans: a person looks at the picture of `agent-platform subscriptions check --request "
            f"{request_id} --out frame.jpg` and clicks with `--click X,Y` (fractions of its width and height), or "
            "gives up with `--relay`; never let a model answer it"
        )
    if found["step"] in ("progress", "waiting"):
        return f"agent-platform subscriptions check --request {request_id}"
    return None


def _result(record_id: str, prepared: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    result = {"subscription_id": record_id, **finish(prepared, _shown(found))}
    step = _next(prepared["id"], found)
    if step is not None:
        result["next"] = step
    return result


def _optional_label(value: object, most: int) -> bool:
    return value is None or label(value, most)


def _account(value: object) -> dict[str, Any]:
    """The data of a new account (`account` of `claude.login`, ADR-100 section 5)."""
    if not isinstance(value, dict) or not set(value) <= ACCOUNT_FIELDS:
        raise PlatformError("invalid_argument", reason="account")
    found: dict[str, Any] = {"name": value.get("name")}
    if not label(found["name"], 100):
        raise PlatformError("invalid_argument", reason="account")
    born = value.get("birth_date")
    if born is not None:
        try:
            date.fromisoformat(born if isinstance(born, str) and DATE_RE.fullmatch(born) else "")
        except ValueError:
            raise PlatformError("invalid_argument", reason="account") from None
        found["birth_date"] = born
    consents = value.get("consents")
    if consents is not None:
        if (
            not isinstance(consents, list)
            or not all(item in CONSENTS for item in consents)
            or len(set(consents)) != len(consents)
        ):
            raise PlatformError("invalid_argument", reason="account")
        found["consents"] = list(consents)
    return found


def _mailbox_body(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PlatformError("invalid_argument", reason="mailbox")
    body = {"imap_port": 993, **value}
    if not valid_mailbox(body):
        raise PlatformError("invalid_argument", reason="mailbox")
    return body


def entry_of(raw: object) -> dict[str, Any]:
    """One subscription to add, checked (module docstring): `mode` and, when it needs the record written before the
    login, `extended`."""
    if not isinstance(raw, dict) or not set(raw) <= ENTRY_FIELDS:
        raise PlatformError("invalid_argument", reason="entry")
    if not label(raw.get("name"), LIMITS["name"]) or not _optional_label(raw.get("note"), LIMITS["note"]):
        raise PlatformError("invalid_argument", reason="label")
    entry: dict[str, Any] = {name: raw[name] for name in ("name", "note") if raw.get(name) is not None}
    if not isinstance(raw.get("automatic", False), bool):
        raise PlatformError("invalid_argument", reason="entry")
    if raw.get("mailbox") is not None:
        entry["mailbox"] = _mailbox_body(raw["mailbox"])
    if raw.get("account") is not None:
        entry["account"] = _account(raw["account"])
    orgs = raw.get("orgs") or []
    if not isinstance(orgs, list) or not all(isinstance(org, str) and UUID_RE.fullmatch(org) for org in orgs):
        raise PlatformError("invalid_argument", reason="use")
    if len(set(orgs)) > MAX_ORGS:
        raise PlatformError("invalid_argument", reason="use")
    if orgs:
        entry["orgs"] = sorted(set(orgs))
    automatic = raw.get("automatic") is True or "mailbox" in entry or "account" in entry
    email = raw.get("email")
    if email is not None:
        if not automatic or not isinstance(email, str) or len(email) > 254 or not EMAIL_RE.fullmatch(email):
            raise PlatformError("invalid_argument", reason="email")
        entry["email"] = email
    entry["mode"] = "automatic" if automatic else "manual"
    entry["extended"] = any(name in entry for name in ("mailbox", "orgs", "account"))
    return entry


def _seal_put(
    account: Account, record_id: str, version: int, body: dict[str, Any], keys: dict[int, bytes]
) -> dict[str, Any]:
    """Seal `body` as the next version of the own further subscription under the newest `CK` and `PUT` it with the
    mirror of its use."""
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
    put = {"record": record, "expected_version": version, "use": mirror}
    return account.call("PUT", f"/v1/subscriptions/{segment(record_id)}", put)


def _create(account: Account, record_id: str, entry: dict[str, Any]) -> None:
    """The new record without a token (ADR-100 section 1): it holds the mailbox and the use before the login."""
    body: dict[str, Any] = {"email": entry.get("email"), "kind": "oauth_login", "name": entry["name"]}
    for name in ("note", "mailbox"):
        if name in entry:
            body[name] = dict(entry[name]) if name == "mailbox" else entry[name]
    if "orgs" in entry:
        body["use"] = {"pool": False, "orgs": list(entry["orgs"])}
    try:
        _seal_put(account, record_id, 0, body, account.cred_keys())
    finally:
        body.pop("mailbox", {}).clear()
        body.clear()


def _start(account: Account, target: Target, record_id: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Write the record when the entry needs it, then send its `claude.login` (with its slot to a device that runs
    logins side by side)."""
    device, keys, side = target
    body: dict[str, Any] = {"mode": entry["mode"], "subscription_id": record_id}
    if side:
        body["extended"] = True
    if entry["extended"]:
        _create(account, record_id, entry)
        if "account" in entry:
            body["account"] = entry["account"]
    else:
        body.update({name: entry[name] for name in ("name", "note") if name in entry})
    if "email" in entry:
        body["email"] = entry["email"]
    return submit(account, (device, keys), "claude.login", body, slot=record_id if side else None)


def _wait_option(args: Any) -> None:
    if not 0 <= args.wait <= WAIT_MAX_S:
        raise PlatformError("invalid_argument")


def _run_login(
    account: Account, args: Any, context: Context, record_id: str, entry: dict[str, Any], *, parallel: bool = False
) -> dict[str, Any]:
    target = _device(account, args.device, parallel=parallel or entry["extended"])
    prepared = _start(account, target, record_id, entry)
    found = wait(account, prepared, args.wait, STEPS)
    if found["step"] == "authorize":
        show_in_browser(context.environ, found["url"], "authorize the Claude login at")
    return _result(record_id, prepared, found)


def _mailbox_option(args: Any, context: Context) -> dict[str, Any] | None:
    """The mailbox of the options (the password from stdin), None when no mailbox option is given."""
    given = (args.mailbox_host, args.mailbox_user, args.mailbox_email)
    if all(value is None for value in given) and not args.mailbox_password_stdin:
        return None
    if any(value is None for value in given) or not args.mailbox_password_stdin:
        raise PlatformError("invalid_argument", reason="mailbox")
    return {
        "imap_host": args.mailbox_host.strip(),
        "imap_port": args.mailbox_port,
        "imap_user": args.mailbox_user.strip(),
        "imap_password": context.read_stdin().removesuffix("\n"),
        "email": args.mailbox_email.strip(),
    }


def add(account: Account, args: Any, context: Context) -> dict[str, Any]:
    raw: dict[str, Any] = {"name": args.name, "note": args.note, "email": args.email, "automatic": args.automatic}
    if not label(args.name, LIMITS["name"]) or not _optional_label(args.note, LIMITS["note"]):
        raise PlatformError("invalid_argument", reason="label")
    _wait_option(args)
    raw["mailbox"] = _mailbox_option(args, context)
    raw["orgs"] = args.org
    if args.register_name is not None or args.birth_date is not None or args.consent:
        found: dict[str, Any] = {"name": args.register_name}
        if args.birth_date is not None:
            found["birth_date"] = args.birth_date
        if args.consent:
            found["consents"] = sorted(set(args.consent), key=CONSENTS.index)
        raw["account"] = found
    return _run_login(account, args, context, uuid7(), entry_of(raw))


def login(account: Account, args: Any, context: Context) -> dict[str, Any]:
    _wait_option(args)
    entry = entry_of({"name": "-", "email": args.email, "automatic": args.automatic})
    del entry["name"]
    record_id = uuid_argument(args.id)
    for meta in listed(account):
        if meta["id"] == record_id:
            if meta["primary"]:
                raise PlatformError("invalid_argument", reason="primary")
            return _run_login(account, args, context, record_id, entry, parallel=_own_mailbox(account, record_id))
    raise PlatformError("not_found")


def _own_mailbox(account: Account, record_id: str) -> bool:
    """Whether the record holds its own mailbox (its login then needs a device that reads it, ADR-100 section 7)."""
    answer = fetch(account, record_id)
    if answer is None:
        raise PlatformError("not_found")
    try:
        _header, body = open_own(account, record_id, answer, account.cred_keys())
    except Unverifiable as error:
        raise unverified(error.code) from None
    found = "mailbox" in body
    body.clear()
    return found


def _entries(args: Any, context: Context) -> list[dict[str, Any]]:
    """The checked entries of `add-many --file` (all of them before anything is sent)."""
    try:
        text = context.read_stdin() if args.file == "-" else Path(args.file).read_text(encoding="utf-8")[: MAX_FILE + 1]
        raw = json.loads(text) if len(text) <= MAX_FILE else None
    except (OSError, UnicodeDecodeError, ValueError):
        raw = None
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_MANY:
        raise PlatformError("invalid_argument", reason="file")
    entries = []
    for index, item in enumerate(raw):
        try:
            entries.append(entry_of(item))
        except PlatformError as error:
            error.fields["entry"] = index
            raise
    return entries


def add_many(account: Account, args: Any, context: Context) -> dict[str, Any]:
    """Several subscriptions at once (ADR-100 section 2): every login starts before any is followed."""
    _wait_option(args)
    entries = _entries(args, context)
    target = _device(account, args.device, parallel=True)
    started: list[tuple[str, dict[str, Any], dict[str, Any] | None]] = []
    results: dict[str, dict[str, Any]] = {}
    for entry in entries:
        record_id = uuid7()
        try:
            started.append((record_id, entry, _start(account, target, record_id, entry)))
        except PlatformError as error:
            started.append((record_id, entry, None))
            results[record_id] = {"subscription_id": record_id, "error": error.code, **error.fields}
    deadline = time.monotonic() + args.wait
    while True:
        open_ones = 0
        for record_id, _entry, prepared in started:
            if prepared is None or (record_id in results and results[record_id]["step"] in ("done", "failed")):
                continue
            found = progress(read(account, prepared))
            results[record_id] = {
                "subscription_id": record_id,
                "request_id": prepared["id"],
                "device_id": prepared["device"],
                **_shown(found),
            }
            step = _next(prepared["id"], found)
            if step is not None:
                results[record_id]["next"] = step
            open_ones += found["step"] not in STEPS
        if open_ones == 0 or time.monotonic() >= deadline:
            break
        time.sleep(devicereq.POLL_S)
    return {"subscriptions": [{"name": entry["name"], **results[record_id]} for record_id, entry, _prepared in started]}


def _point(text: str) -> dict[str, float]:
    parts = text.split(",")
    try:
        x, y = (float(part) for part in parts)
    except ValueError:
        raise PlatformError("invalid_argument", reason="click") from None
    if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
        raise PlatformError("invalid_argument", reason="click")
    return {"x": x, "y": y}


def _newest_frame(view: dict[str, Any]) -> dict[str, Any] | None:
    frames = [
        item["body"] for item in view["responses"] if item["status"] == "verified" and item["body"]["step"] == "verify"
    ]
    return frames[-1] if frames else None


def check(account: Account, args: Any, _context: Context) -> dict[str, Any]:
    """`subscriptions check` (module docstring): the state of one login, its newest picture and the person's click."""
    _wait_option(args)
    if args.click is not None and args.relay:
        raise PlatformError("invalid_argument", reason="click")
    point = None if args.click is None else _point(args.click)
    if point is not None and account.actor != account.user_id:
        raise PlatformError("invalid_argument", reason="human_only")
    prepared = resume(account, args.request, ("claude.login",))
    view = read(account, prepared)
    before = _newest_frame(view)
    if point is not None or args.relay:
        follow = {"click": point} if point is not None else {"relay": True}
        submit(account, (prepared["device"], prepared["keys"]), "claude.login", follow, prepared["id"])
        deadline = time.monotonic() + args.wait
        while True:
            view = read(account, prepared)
            frame, found = _newest_frame(view), progress(view)
            newer = frame is not None and (before is None or frame["frame"] > before["frame"])
            if newer or found["step"] not in ("verify", "progress", "waiting") or time.monotonic() >= deadline:
                break
            time.sleep(devicereq.POLL_S)
    found = progress(view)
    result = finish(prepared, _shown(found))
    frame = _newest_frame(view)
    if args.out is not None and frame is not None:
        Path(args.out).write_bytes(base64.b64decode(frame["image"]["data"]))
        result["picture"] = {
            "path": args.out,
            "frame": frame["frame"],
            "width": frame["image"]["width"],
            "height": frame["image"]["height"],
        }
    step = _next(prepared["id"], found)
    if step is not None:
        result["next"] = step
    return result


def mailbox(account: Account, args: Any, context: Context) -> dict[str, Any]:
    """Set, remove or check the own mailbox of a further subscription (ADR-100 sections 1 and 7)."""
    _wait_option(args)
    found = _mailbox_option(args, context)
    if args.check:
        if args.clear or found is not None:
            raise PlatformError("invalid_argument", reason="mailbox")
        record_id = uuid_argument(args.id)
        device, keys, _side = _device(account, args.device, parallel=True)
        prepared = submit(account, (device, keys), "mailbox.check", {"subscription_id": record_id})
        return {"subscription_id": record_id, **finish(prepared, wait(account, prepared, args.wait, STEPS))}
    if args.clear == (found is not None):
        raise PlatformError("invalid_argument", reason="mailbox")
    body_box = None if found is None else _mailbox_body(found)

    def edit(body: dict[str, Any]) -> None:
        body.pop("mailbox", None)
        if body_box is not None:
            body["mailbox"] = dict(body_box)

    result = change(account, uuid_argument(args.id), edit)
    return {**result, "mailbox": None if body_box is None else body_box["email"]}


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
        try:
            stored = _seal_put(account, record_id, version, body, keys)
        except PlatformError as error:
            if error.fields.get("hub_error") in CONCURRENT:
                continue
            raise
        finally:
            body.pop("mailbox", {}).clear()
            body.clear()
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
    def edit(body: dict[str, Any]) -> None:
        for name in ("token", "refresh_token", "relogin"):  # nothing left to renew (ADR-092 section 2)
            body.pop(name, None)

    result = change(account, uuid_argument(args.id), edit)
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


# --- who used how much (ADR-102) ---


def _usage_entry(item: object) -> dict[str, Any]:
    texts = ("subscription_id", "owner_id", "since", "until")
    if (
        not isinstance(item, dict)
        or not all(isinstance(item.get(name), str) for name in texts)
        or not isinstance(item.get("own"), bool)
        or not isinstance(item.get("primary"), bool)
        or not isinstance(item.get("people"), list)
        or not all(isinstance(person, dict) and isinstance(person.get("user_id"), str) for person in item["people"])
        or not (item.get("measured") is None or isinstance(item.get("measured"), dict))
        or not (item.get("total") is None or isinstance(item.get("total"), dict))
    ):
        raise unverified("response_invalid")
    return item


def usage_overview(account: Account, period: str, since: str | None, until: str | None) -> dict[str, Any]:
    """`subscriptions usage` (module docstring): the hub's figures, each own subscription named as in `list`."""
    if period not in USAGE_PERIODS or (period != "range" and (since or until)) or (period == "range" and not since):
        raise PlatformError("invalid_argument", reason="period")
    query = {"period": period}
    for name, value in (("since", since), ("until", until)):
        if value is not None:
            if not TIME_RE.fullmatch(value):
                raise PlatformError("invalid_argument", reason=name)
            query[name] = value
    answer = account.call("GET", f"/v1/me/subscriptions/usage?{urlencode(query)}")
    entries = [_usage_entry(item) for item in rows_of(answer, "subscriptions")]
    names: dict[str, dict[str, Any]] = {}
    if any(entry["own"] for entry in entries):
        names = {item["id"]: item for item in overview(account)["subscriptions"]}
    found = []
    for entry in entries:
        named = names.get(entry["subscription_id"], {})
        people = [{**person, "you": person["user_id"] == account.user_id} for person in entry["people"]]
        found.append({**entry, "name": named.get("name"), "email": named.get("email"), "people": people})
    result: dict[str, Any] = {
        "period": period,
        "subscriptions": found,
        "estimate": ESTIMATE_NOTE,
        "dfx_access": "agent-platform billing --org <org_id>",
    }
    for name in ("since", "until"):
        if isinstance(answer.get(name), str):
            result[name] = answer[name]
    return result


def _usage(account: Account, args: Any, _context: Context) -> dict[str, Any]:
    return usage_overview(account, args.period, args.since, args.until)


def _login_options(sub: Any) -> None:
    sub.add_argument("--automatic", action="store_true", help="sign in through the user's mailbox (no browser)")
    sub.add_argument("--email", help="with --automatic: the address to sign in with (default: the mailbox's)")
    sub.add_argument("--device", help="the platform device (default: an online one that can do it)")
    sub.add_argument("--wait", type=int, default=DEFAULT_WAIT_S, help="seconds to wait for the device (0-600)")


def _mailbox_options(sub: Any) -> None:
    sub.add_argument("--mailbox-host", help="the subscription's own mailbox: IMAP host")
    sub.add_argument("--mailbox-port", type=int, default=993, help="IMAP port (default 993)")
    sub.add_argument("--mailbox-user", help="IMAP user")
    sub.add_argument("--mailbox-email", help="the mailbox's own address")
    sub.add_argument("--mailbox-password-stdin", action="store_true", help="read the IMAP password from stdin")


def _register_usage(tree: Tree, group: Any) -> None:
    """`subscriptions usage` (ADR-102)."""
    sub = tree.command(
        group,
        "usage",
        _usage,
        "who used how much of which subscription: tokens, sessions and the estimated share of the quota per person",
        example="agent-platform subscriptions usage --period week",
        words="verbrauch kollegen wer wieviel tokens guthaben anteil geschätzt fenster woche zeitraum person"
        " usage colleagues who how much tokens share estimated window week period person",
    )
    sub.add_argument("--period", default="five_hour", help="five_hour (default), week or range")
    sub.add_argument("--since", help="with --period range: start (2026-11-01T00:00:00.000Z)")
    sub.add_argument("--until", help="with --period range: end (default now)")


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
    _register_usage(tree, group)
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
    _mailbox_options(sub)
    sub.add_argument("--org", action="append", help="an organization whose sessions may use it (repeatable)")
    sub.add_argument("--register-name", help="a new Claude account: the full name (registration)")
    sub.add_argument("--birth-date", help="a new Claude account: the date of birth YYYY-MM-DD")
    sub.add_argument(
        "--consent",
        action="append",
        choices=list(CONSENTS),
        help="a consent the account holder gave for the new account (repeatable; only these boxes are ticked)",
    )
    sub = tree.command(
        group,
        "add-many",
        add_many,
        "add several subscriptions at once, each logged in side by side in its own sandbox with its own mailbox",
        writes=True,
        example="agent-platform subscriptions add-many --file subscriptions.json --wait 120",
        words="mehrere abos hinzufügen parallel registrieren postfach several subscriptions batch register mailbox",
        then="agent-platform subscriptions check --request <request_id>",
    )
    sub.add_argument("--file", required=True, help="a JSON list of entries (name, note, email, mailbox, orgs, account)")
    sub.add_argument("--device", help="the platform device (default: an online one that can do it)")
    sub.add_argument("--wait", type=int, default=DEFAULT_WAIT_S, help="seconds to follow the logins (0-600)")
    sub = tree.command(
        group,
        "check",
        check,
        "the state of one login; the picture of a check for humans and a person's click into it",
        writes=True,
        example="agent-platform subscriptions check --request <request_id> --out frame.jpg",
        words="prüfung mensch captcha bild klicken fortschritt human check picture click progress verify",
        then="agent-platform subscriptions check --request <request_id> --click 0.31,0.52",
    )
    sub.add_argument("--request", required=True, help="request_id of the login")
    sub.add_argument("--out", help="write the newest picture of the check for humans to this JPEG file")
    sub.add_argument("--click", help="X,Y: a person's click into the picture (fractions of width and height)")
    sub.add_argument("--relay", action="store_true", help="give the automation up: continue with link and code")
    sub.add_argument("--wait", type=int, default=10, help="seconds to wait for the next picture after a click")
    sub = tree.command(
        group,
        "mailbox",
        mailbox,
        "set or remove the own mailbox of a subscription (password on stdin)",
        writes=True,
        example="printf '%s' \"$PASSWORD\" | agent-platform subscriptions mailbox --id <subscription_id> "
        "--mailbox-host imap.example.com --mailbox-user me --mailbox-email me@example.com --mailbox-password-stdin",
        words="postfach eigenes abo imap passwort mailbox own subscription password",
    )
    sub.add_argument("--id", required=True)
    _mailbox_options(sub)
    sub.add_argument("--clear", action="store_true", help="remove the own mailbox (the single mailbox is used again)")
    sub.add_argument("--check", action="store_true", help="let a platform device sign in to the own mailbox")
    sub.add_argument("--device", help="the platform device of --check (default: an online one that can do it)")
    sub.add_argument("--wait", type=int, default=DEFAULT_WAIT_S, help="seconds to wait for the check (0-600)")
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
