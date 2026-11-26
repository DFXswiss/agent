"""Mailboxes of the user that sessions read through the platform (KOR-74, ADR-099; 03-api.md section 4.8,
11-agent-access.md section 8.4; rules E-31, E-04).

Every mailbox is a `mailbox` record of the user (`contracts/credentials/mailbox.json`), sealed under the user's `CK`;
the hub never sees host, user, password, address, name or purpose. The primary one is the mailbox of the automatic
login (`credentials set-mailbox` writes it too). Sessions never see a password: they read a mailbox released to their
workgroup with the operation `mail.find`.

- `mailbox list`: `GET /v1/me/mailboxes`, then every record (`GET /v1/mailboxes/{id}`), checked like every own record
  (owner, ID and kind of the header, author the user, the actor, a former agent of the user or a device the verified
  logs entitle to the user's `CK`, signature, the user's `CK` of the record's epoch, a body of the contract). Per
  mailbox: `name`, `email`, `purpose`, `primary`, the IMAP host, port and user and `wgs` (the sealed release; the
  hub's mirror is named `mirror` only when it differs), never the password. A record that does not open is
  `unverifiable` with its code; one gone between the list and its read is left out.
- `mailbox add`: a new record (UUIDv7, version 1, `expected_version` 0) with the password from stdin; the name is
  unique among the user's mailboxes.
- `mailbox change` and `mailbox release`: the record is opened, changed and sealed again under the user's newest `CK`
  (version + 1, author the actor), `PUT /v1/mailboxes/{id}`; one retry after `version_conflict`/`epoch_stale`.
  Every write sends the plaintext mirror `use {wgs}` of the release it sealed (`{wgs: []}` when none; the sealed body
  then has no `use`).
- `mailbox check`: the device request `mailbox.check` with the opened body (`{mailbox}`); the device tries the login.
- `mailbox delete`: `DELETE /v1/mailboxes/{id}` (`404` counts as done).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree
from agent_platform.credentials import MAILBOX_NAME_RE, MAX_PURPOSE, MAX_RELEASES, valid_mailbox
from agent_platform.devicereq import choose_device, device_keys, finish, submit, wait
from agent_platform.errors import PlatformError
from agent_platform.eventio import rows_of, uuid_argument
from agent_platform.hub import segment
from agent_runner.crypto.credential import new_header, open_credential, seal_credential, verify_credential
from agent_runner.crypto.encoding import body_json, format_time, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7

KIND = "mailbox"
CONCURRENT = frozenset({"version_conflict", "epoch_stale"})
DEFAULT_PORT = 993
DEFAULT_WAIT_S = 60
SHOWN = ("name", "email", "purpose", "imap_host", "imap_port", "imap_user")


class Unverifiable(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- the hub's list and records ---


def _meta(item: object) -> dict[str, Any]:
    if (
        not isinstance(item, dict)
        or not isinstance(item.get("id"), str)
        or not isinstance(item.get("primary"), bool)
        or not (item.get("use") is None or isinstance(item.get("use"), dict))
    ):
        raise unverified("response_invalid")
    return item


def listed(account: Account) -> list[dict[str, Any]]:
    """`GET /v1/me/mailboxes` (`response_invalid` when malformed)."""
    return [_meta(item) for item in rows_of(account.call("GET", "/v1/me/mailboxes"), "mailboxes")]


def fetch(account: Account, record_id: str) -> dict[str, Any] | None:
    """`GET /v1/mailboxes/{id}`; None when the record is gone."""
    try:
        return account.call("GET", f"/v1/mailboxes/{segment(record_id)}")
    except PlatformError as error:
        if error.fields.get("hub_error") == "not_found":
            return None
        raise


def _author_key(account: Account, author: object) -> bytes:
    if isinstance(author, str) and (author in (account.user_id, account.actor) or author in account.former_agents()):
        return account.author_key(author)
    found = device_keys(account, author) if isinstance(author, str) else None
    if found is None:
        raise Unverifiable("author_not_writer")
    return found[1]


def open_own(
    account: Account, record_id: str, answer: dict[str, Any], keys: dict[int, bytes]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The checked header and body of an own mailbox record (module docstring); `Unverifiable` otherwise."""
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
    if not isinstance(body, dict) or not valid_mailbox(body):
        raise Unverifiable("params_invalid")
    return checked, body


def _wgs(body: dict[str, Any]) -> list[str]:
    return list(body.get("use", {"wgs": []})["wgs"])


def _opened(account: Account) -> list[tuple[dict[str, Any], dict[str, Any] | str]]:
    """Every own mailbox still there: `(meta, body)` or `(meta, code)` when it does not open."""
    found = listed(account)
    keys = account.cred_keys() if found else {}
    result: list[tuple[dict[str, Any], dict[str, Any] | str]] = []
    for meta in found:
        answer = fetch(account, meta["id"])
        if answer is None:
            continue
        try:
            result.append((meta, open_own(account, meta["id"], answer, keys)[1]))
        except Unverifiable as error:
            result.append((meta, error.code))
    return result


def _entry(meta: dict[str, Any], body: dict[str, Any] | str) -> dict[str, Any]:
    entry: dict[str, Any] = {"id": meta["id"], "primary": meta["primary"]}
    if isinstance(body, str):
        return {**entry, "status": "unverifiable", "code": body}
    wgs = _wgs(body)
    entry.update({name: body.get(name) for name in SHOWN}, status="verified", wgs=wgs)
    mirror = (meta.get("use") or {}).get("wgs", [])
    if sorted(mirror) != sorted(wgs):
        entry["mirror"] = mirror
    return entry


def overview(account: Account) -> dict[str, Any]:
    """`mailbox list` (module docstring): the primary one first, then by name, the unverifiable ones last."""
    entries = [_entry(meta, body) for meta, body in _opened(account)]
    entries.sort(key=lambda item: (not item["primary"], "name" not in item, item.get("name") or "", item["id"]))
    return {"mailboxes": entries}


def _valid_name(name: str) -> None:
    if not MAILBOX_NAME_RE.fullmatch(name):
        raise PlatformError("invalid_argument", reason="name")


def _unique(account: Account, name: str, record_id: str) -> None:
    """`invalid_argument` for a name that already names another own mailbox (one that opens)."""
    for meta, body in _opened(account):
        if meta["id"] != record_id and isinstance(body, dict) and body.get("name") == name:
            raise PlatformError("invalid_argument", reason="name_taken")


def _purpose(value: str | None) -> str | None:
    if value is not None and not 0 < len(value) <= MAX_PURPOSE:
        raise PlatformError("invalid_argument", reason="purpose")
    return value


# --- writes ---


def _seal_put(account: Account, record_id: str, body: dict[str, Any], version: int) -> dict[str, Any] | None:
    """Seal `body` as version + 1 and store it at `version` with the mirror of its release; None after a concurrent
    write (module docstring)."""
    epoch, key = account.cred_key()
    fields = new_header(
        id=record_id,
        owner=account.user_id,
        kind=KIND,
        version=version + 1,
        epoch=epoch,
        author=account.actor,
        ts=format_time(datetime.now(UTC)),
    )
    record = seal_credential(key, fields, body_json(body), account.signer.sig)
    put = {"record": record, "expected_version": version, "use": {"wgs": _wgs(body)}}
    try:
        stored = account.call("PUT", f"/v1/mailboxes/{segment(record_id)}", put)
    except PlatformError as error:
        if error.fields.get("hub_error") in CONCURRENT:
            return None
        raise
    return {"mailbox_id": record_id, "version": stored.get("version")}


def create(account: Account, body: dict[str, Any]) -> dict[str, Any]:
    record_id = uuid7()
    for _attempt in range(2):
        stored = _seal_put(account, record_id, body, 0)
        if stored is not None:
            return stored
    raise PlatformError("hub_refused", hub_error="version_conflict")


def change(account: Account, record_id: str, edit: Any) -> dict[str, Any]:
    """Open the own mailbox, apply `edit(body)` and seal it again (module docstring); its version."""
    for _attempt in range(2):
        answer = fetch(account, record_id)
        if answer is None:
            raise PlatformError("not_found")
        try:
            header, body = open_own(account, record_id, answer, account.cred_keys())
        except Unverifiable as error:
            raise unverified(error.code) from None
        edit(body)
        if not valid_mailbox(body):
            raise PlatformError("invalid_argument", reason="mailbox")
        stored = _seal_put(account, record_id, body, int(str(header["version"])))
        body.clear()
        if stored is not None:
            return stored
    raise PlatformError("hub_refused", hub_error="version_conflict")


def _release(values: list[str] | None) -> list[str]:
    wgs = sorted({uuid_argument(wg) for wg in values or []})
    if len(wgs) > MAX_RELEASES:
        raise PlatformError("invalid_argument", reason="wg")
    return wgs


def add(account: Account, args: Any, context: Context) -> dict[str, Any]:
    wgs = _release(args.wg)
    _valid_name(args.name)
    body: dict[str, Any] = {
        "imap_host": args.imap_host.strip(),
        "imap_port": args.imap_port,
        "imap_user": args.imap_user.strip(),
        "imap_password": context.read_stdin().removesuffix("\n"),
        "email": args.email.strip(),
        "name": args.name,
    }
    if _purpose(args.purpose) is not None:
        body["purpose"] = args.purpose
    if wgs:
        body["use"] = {"wgs": wgs}
    if not valid_mailbox(body):
        raise PlatformError("invalid_argument", reason="mailbox")
    _unique(account, args.name, "")
    result = create(account, body)
    body.clear()
    return {**result, "name": args.name, "wgs": wgs}


def change_command(account: Account, args: Any, context: Context) -> dict[str, Any]:
    record_id = uuid_argument(args.id)
    _purpose(args.purpose)
    if args.name is not None:
        _valid_name(args.name)
        _unique(account, args.name, record_id)
    given = {
        "name": args.name,
        "email": None if args.email is None else args.email.strip(),
        "imap_host": None if args.imap_host is None else args.imap_host.strip(),
        "imap_port": args.imap_port,
        "imap_user": None if args.imap_user is None else args.imap_user.strip(),
        "purpose": args.purpose,
        "imap_password": context.read_stdin().removesuffix("\n") if args.password_stdin else None,
    }
    changes = {name: value for name, value in given.items() if value is not None}
    if not changes:
        raise PlatformError("invalid_argument", reason="nothing_to_change")
    result = change(account, record_id, lambda body: body.update(changes))
    return {**result, "changed": sorted("password" if name == "imap_password" else name for name in changes)}


def release(account: Account, args: Any, _context: Context) -> dict[str, Any]:
    wgs = _release(args.wg)
    if args.none == bool(wgs):
        raise PlatformError("invalid_argument", reason="wg")

    def edit(body: dict[str, Any]) -> None:
        body.pop("use", None)
        if wgs:
            body["use"] = {"wgs": list(wgs)}

    result = change(account, uuid_argument(args.id), edit)
    return {**result, "wgs": wgs}


def check(account: Account, args: Any, _context: Context) -> dict[str, Any]:
    record_id = uuid_argument(args.id)
    answer = fetch(account, record_id)
    if answer is None:
        raise PlatformError("not_found")
    try:
        _header, body = open_own(account, record_id, answer, account.cred_keys())
    except Unverifiable as error:
        raise unverified(error.code) from None
    prepared = submit(account, choose_device(account, args.device), "mailbox.check", {"mailbox": body})
    body.clear()
    found = wait(account, prepared, args.wait, frozenset({"done", "failed"}))
    return {"mailbox_id": record_id, **finish(prepared, found)}


def delete(account: Account, args: Any, _context: Context) -> dict[str, Any]:
    record_id = uuid_argument(args.id)
    try:
        account.call("DELETE", f"/v1/mailboxes/{segment(record_id)}")
    except PlatformError as error:
        if error.fields.get("hub_error") != "not_found":
            raise
    return {"mailbox_id": record_id, "status": "deleted"}


def _list(account: Account, _args: Any, _context: Context) -> dict[str, Any]:
    return overview(account)


def register(tree: Tree) -> None:
    group = tree.group("mailbox", "mailboxes that sessions read with mail.find (never the password)")
    tree.command(
        group,
        "list",
        _list,
        "every own mailbox: name, address, purpose, primary, released workgroups (never the password)",
        example="agent-platform mailbox list",
        words="postfächer auflisten übersicht mailboxes overview list inboxes",
    )
    sub = tree.command(
        group,
        "add",
        add,
        "add a mailbox (password on stdin); sessions read it only where it is released",
        writes=True,
        example="printf '%s' \"$PASSWORD\" | agent-platform mailbox add --name signup --email bot@example.com "
        "--imap-host imap.example.com --imap-user bot@example.com",
        words="postfach hinzufügen anlegen imap passwort add mailbox inbox new",
    )
    sub.add_argument("--name", required=True, help="unique among your mailboxes: [a-z0-9][a-z0-9._-]{0,63}")
    sub.add_argument("--email", required=True)
    sub.add_argument("--imap-host", required=True)
    sub.add_argument("--imap-port", type=int, default=DEFAULT_PORT)
    sub.add_argument("--imap-user", required=True)
    sub.add_argument("--purpose", help="what it is for (up to 500 characters)")
    sub.add_argument("--wg", action="append", help="release it to this workgroup ID (repeatable)")
    sub = tree.command(
        group,
        "change",
        change_command,
        "change a mailbox; what is not given stays (--password-stdin: a new password on stdin)",
        writes=True,
        example='agent-platform mailbox change --id <mailbox_id> --purpose "sign-up mails"',
        words="postfach ändern bearbeiten passwort umbenennen change edit mailbox rename password",
    )
    sub.add_argument("--id", required=True)
    sub.add_argument("--name")
    sub.add_argument("--email")
    sub.add_argument("--imap-host")
    sub.add_argument("--imap-port", type=int)
    sub.add_argument("--imap-user")
    sub.add_argument("--purpose")
    sub.add_argument("--password-stdin", action="store_true", help="read a new password from stdin")
    sub = tree.command(
        group,
        "release",
        release,
        "which workgroups' sessions may read a mailbox with mail.find (--none: no one)",
        writes=True,
        example="agent-platform mailbox release --id <mailbox_id> --wg <wg_id>",
        words="postfach freigeben arbeitsgruppe sitzungen release share workgroup sessions mail.find",
    )
    sub.add_argument("--id", required=True)
    sub.add_argument("--wg", action="append", help="a workgroup ID whose sessions may read it (repeatable)")
    sub.add_argument("--none", action="store_true", help="release it to no workgroup")
    sub = tree.command(
        group,
        "check",
        check,
        "let a device sign in to a mailbox (mailbox.check)",
        example="agent-platform mailbox check --id <mailbox_id>",
        words="postfach prüfen testen anmeldung check mailbox test login",
        then="agent-platform credentials check --request <request_id>",
    )
    sub.add_argument("--id", required=True)
    sub.add_argument("--device", help="the execution device (default: an online one of the user)")
    sub.add_argument("--wait", type=int, default=DEFAULT_WAIT_S, help="seconds to wait for the device (0-600)")
    sub = tree.command(
        group,
        "delete",
        delete,
        "delete a mailbox (sessions can no longer read it)",
        writes=True,
        example="agent-platform mailbox delete --id <mailbox_id>",
        words="postfach löschen entfernen delete remove mailbox",
    )
    sub.add_argument("--id", required=True)
