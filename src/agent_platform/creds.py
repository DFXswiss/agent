"""Credential records and the device flows around them (06-web.md sections 8.1 and 8.2, 03-api.md sections 4.8 and
4.10; twins of `web/src/core/credentials/records.ts`, `credentials/login.ts` and `github/connection.ts`).

- `credentials list`: `GET /v1/me/credentials`, every record opened and checked like the web (owner the user, kind
  as listed, author the user or a device the verified logs entitle to the user's `CK`, signature, `CK` of the record's
  epoch, body exactly as `contracts/credentials/<kind>.json` allows); only the non-secret summary is printed, never a
  token, key or password. A record that fails is `unverifiable` with its code.
- `credentials set-mailbox`: the mailbox of the login automation; the password comes from stdin.
- `credentials delete --kind`: `DELETE /v1/me/credentials/{kind}` (`404` counts as done).
- `credentials check --kind`: the device request `credential.check` (API key, subscription) or `mailbox.check`.
- `github status|connect|disconnect`: the GitHub connection (the `github` record, written by the device that ran the
  device flow); `connect` sends `github.connect` and opens the verification page in the user's browser with the user
  code; `disconnect` deletes the record (the authorization at GitHub is revoked on its settings page).
- `claude login`: the relay of the web (`claude.login {mode}`): the device answers the authorization link (opened in
  the user's browser), the code the user receives goes back as a follow-up request (stdin), the device stores the
  subscription record itself.
"""

from __future__ import annotations

import re
from typing import Any

from agent_platform import devicereq
from agent_platform.account import Account
from agent_platform.commands import Context, Tree
from agent_platform.credentials import put_record
from agent_platform.devicereq import EMAIL_RE, LOGIN_RE, choose_device, finish, resume, show_in_browser, submit, wait
from agent_platform.errors import PlatformError
from agent_platform.eventio import rows_of
from agent_runner.crypto.credential import open_credential, verify_credential
from agent_runner.crypto.encoding import parse_json, parse_time
from agent_runner.crypto.errors import CryptoError

KINDS = ("github", "anthropic_api_key", "claude_subscription", "mailbox")
CHECK_KINDS = ("anthropic_api_key", "claude_subscription", "mailbox")
GITHUB_SETTINGS_URL = "https://github.com/settings/applications"
PRINTABLE_RE = re.compile(r"[!-~]{1,4096}")
HOST_RE = re.compile(r"[A-Za-z0-9.-]{1,253}")
IMAP_USER_RE = re.compile(r"[ -~]{1,256}")
SCOPE_RE = re.compile(r"[a-z0-9:_-]{1,64}")
ORIGINS = ("setup_token", "login_token")
DEFAULT_WAIT_S = 60


def _text(value: object, pattern: re.Pattern[str], most: int = 4096) -> bool:
    return isinstance(value, str) and len(value) <= most and pattern.fullmatch(value) is not None


def is_email(value: object) -> bool:
    return _text(value, EMAIL_RE, 254)


def _time(value: object) -> bool:
    try:
        parse_time(value)
    except CryptoError:
        return False
    return True


def _int(value: object, low: int, high: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and low <= value <= high


def _github(body: dict[str, Any]) -> bool:
    scopes = body.get("scopes")
    return (
        sorted(body) == ["created_at", "login", "scopes", "token", "user_id"]
        and _text(body["token"], PRINTABLE_RE)
        and _text(body["login"], LOGIN_RE)
        and _int(body["user_id"], 1, 2**53 - 1)
        and isinstance(scopes, list)
        and len(scopes) <= 64
        and all(_text(scope, SCOPE_RE) for scope in scopes)
        and _time(body["created_at"])
    )


def _api_key(body: dict[str, Any]) -> bool:
    return sorted(body) == ["key"] and _text(body["key"], PRINTABLE_RE)


def _subscription(body: dict[str, Any]) -> bool:
    keys = ["email", "kind", "token"] + (["expires_at"] if "expires_at" in body else [])
    return (
        sorted(body) == sorted(keys)
        and ("expires_at" not in body or _time(body["expires_at"]))
        and _text(body["token"], PRINTABLE_RE)
        and (body["email"] is None or is_email(body["email"]))
        and body["kind"] in ORIGINS
    )


def _mailbox(body: dict[str, Any]) -> bool:
    password = body.get("imap_password")
    return (
        sorted(body) == ["email", "imap_host", "imap_password", "imap_port", "imap_user"]
        and _text(body["imap_host"], HOST_RE)
        and _int(body["imap_port"], 1, 65535)
        and _text(body["imap_user"], IMAP_USER_RE)
        and isinstance(password, str)
        and 1 <= len(password) <= 1024
        and is_email(body["email"])
    )


BODIES = {"github": _github, "anthropic_api_key": _api_key, "claude_subscription": _subscription, "mailbox": _mailbox}


def summary(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """What of a checked body may be shown: never a token, key or password."""
    if kind == "github":
        return {"login": body["login"], "created_at": body["created_at"]}
    if kind == "claude_subscription":
        return {"email": body["email"], "origin": body["kind"], "expires_at": body.get("expires_at")}
    if kind == "mailbox":
        return {"email": body["email"], "host": body["imap_host"]}
    return {}


class Unverifiable(Exception):
    """A record that does not open, with its code (a crypto code or `params_invalid`)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _author_key(account: Account, author: object) -> bytes:
    if author == account.user_id:
        return account.identity.sig_public()
    found = devicereq.device_keys(account, author) if isinstance(author, str) else None
    if found is None:
        raise Unverifiable("author_not_writer")
    return found[1]


def open_record(account: Account, kind: str, record: object) -> dict[str, Any]:
    """The summary of a record (module docstring); `Unverifiable` with the code when it does not open."""
    header = record.get("header") if isinstance(record, dict) else None
    if not isinstance(header, dict) or header.get("owner") != account.user_id or header.get("kind") != kind:
        raise Unverifiable("header_invalid")
    key = _author_key(account, header.get("author"))
    try:
        checked = verify_credential(record, key)
        ck = account.cred_keys().get(int(str(checked["epoch"])))
        if ck is None:
            raise Unverifiable("epoch_stale")
        body = parse_json(open_credential(ck, record, key))
    except CryptoError as error:
        raise Unverifiable(error.code) from None
    if not isinstance(body, dict) or not BODIES[kind](body):
        raise Unverifiable("params_invalid")
    return {"version": checked["version"], "author": header["author"], **summary(kind, body)}


def fetch_record(account: Account, kind: str) -> object:
    try:
        answer = account.call("GET", f"/v1/me/credentials/{kind}")
    except PlatformError as error:
        if error.fields.get("hub_error") == "not_found":
            return None
        raise
    return answer.get("record")


def credentials_list(account: Account) -> dict[str, Any]:
    entries = []
    for item in rows_of(account.call("GET", "/v1/me/credentials"), "credentials"):
        kind = item.get("kind")
        if kind not in KINDS:
            continue
        record = fetch_record(account, str(kind))
        if record is None:
            continue
        listed = {key: item.get(key) for key in ("kind", "version", "epoch", "updated_at")}
        try:
            entries.append({**listed, "status": "verified", **open_record(account, str(kind), record)})
        except Unverifiable as error:
            entries.append({**listed, "status": "unverifiable", "code": error.code})
    entries.sort(key=lambda entry: KINDS.index(entry["kind"]))
    return {"credentials": entries}


def credentials_delete(account: Account, kind: str) -> dict[str, Any]:
    try:
        account.call("DELETE", f"/v1/me/credentials/{kind}")
    except PlatformError as error:
        if error.fields.get("hub_error") != "not_found":
            raise
    return {"kind": kind, "deleted": True}


def set_mailbox(account: Account, args: Any, password: str) -> dict[str, Any]:
    body = {
        "imap_host": args.imap_host.strip(),
        "imap_port": args.imap_port,
        "imap_user": args.imap_user.strip(),
        "imap_password": password.removesuffix("\n"),
        "email": args.email.strip(),
    }
    if not _mailbox(body):
        raise PlatformError("invalid_argument")
    return {**put_record(account, "mailbox", body), "kind": "mailbox"}


# --- device flows ---


def credentials_check(account: Account, args: Any, environ: Any) -> dict[str, Any]:
    if args.request is not None:
        prepared = resume(account, args.request, ("credential.check", "mailbox.check"))
    elif args.kind is None:
        raise PlatformError("invalid_argument")
    else:
        device = choose_device(account, args.device)
        if args.kind == "mailbox":
            prepared = submit(account, device, "mailbox.check", {})
        else:
            prepared = submit(account, device, "credential.check", {"kind": args.kind})
    return finish(prepared, wait(account, prepared, args.wait, frozenset({"done", "failed"})))


def github_status(account: Account) -> dict[str, Any]:
    listed = rows_of(account.call("GET", "/v1/me/credentials"), "credentials")
    record = fetch_record(account, "github") if any(item.get("kind") == "github" for item in listed) else None
    if record is None:
        return {"status": "disconnected"}
    try:
        return {"status": "connected", **open_record(account, "github", record)}
    except Unverifiable as error:
        return {"status": "unverifiable", "code": error.code}


def github_connect(account: Account, args: Any, environ: Any) -> dict[str, Any]:
    if args.request is not None:
        prepared = resume(account, args.request, ("github.connect",))
        return finish(prepared, wait(account, prepared, args.wait, frozenset({"done", "failed"})))
    prepared = submit(account, choose_device(account, args.device), "github.connect", {})
    found = wait(account, prepared, args.wait, frozenset({"device_code", "done", "failed"}))
    if found["step"] == "device_code":
        show_in_browser(environ, found["verification_uri"], f"enter the code {found['user_code']} at")
    return finish(prepared, found)


def github_disconnect(account: Account) -> dict[str, Any]:
    credentials_delete(account, "github")
    return {"status": "disconnected", "revoke_at": GITHUB_SETTINGS_URL}


def claude_login(account: Account, args: Any, context: Context) -> dict[str, Any]:
    if args.request is not None:
        prepared = resume(account, args.request, ("claude.login",))
        if args.code:
            code = context.read_stdin().strip()
            if not 1 <= len(code) <= 512 or not all("!" <= char <= "~" for char in code):
                raise PlatformError("invalid_argument")
            prepared = submit(
                account, (prepared["device"], prepared["keys"]), "claude.login", {"code": code}, prepared["id"]
            )
        return finish(prepared, wait(account, prepared, args.wait, frozenset({"done", "failed"})))
    if args.code:
        raise PlatformError("invalid_argument")
    prepared = submit(account, choose_device(account, args.device), "claude.login", {"mode": args.mode})
    found = wait(account, prepared, args.wait, frozenset({"authorize", "done", "failed"}))
    if found["step"] == "authorize":
        show_in_browser(context.environ, found["url"], "authorize the Claude login at")
    return finish(prepared, found)


# --- commands ---


def _list(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return credentials_list(account)


def _delete(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return credentials_delete(account, args.kind)


def _mailbox_command(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return set_mailbox(account, args, context.read_stdin())


def _check(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return credentials_check(account, args, context.environ)


def _github_status(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return github_status(account)


def _github_connect(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return github_connect(account, args, context.environ)


def _github_disconnect(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return github_disconnect(account)


def _claude_login(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return claude_login(account, args, context)


def _flow_options(sub: Any) -> None:
    sub.add_argument("--device", help="the execution device (default: an online one of the user)")
    sub.add_argument("--request", help="request_id of an earlier call: continue that flow")
    sub.add_argument("--wait", type=int, default=DEFAULT_WAIT_S, help="seconds to wait for the device (0-600)")


def register(tree: Tree) -> None:
    group = tree.group("credentials", "credential records")
    tree.command(group, "list", _list, "the user's credentials (summaries only, never a secret)")
    sub = tree.command(group, "set-mailbox", _mailbox_command, "store the login mailbox (password on stdin)")
    sub.add_argument("--imap-host", required=True)
    sub.add_argument("--imap-port", type=int, default=993)
    sub.add_argument("--imap-user", required=True)
    sub.add_argument("--email", required=True)
    sub = tree.command(group, "delete", _delete, "delete a credential")
    sub.add_argument("--kind", required=True, choices=list(KINDS))
    sub = tree.command(group, "check", _check, "let a device check a stored credential")
    sub.add_argument("--kind", choices=list(CHECK_KINDS))
    _flow_options(sub)
    group = tree.group("github", "the GitHub connection")
    tree.command(group, "status", _github_status, "connected GitHub login, if any")
    sub = tree.command(group, "connect", _github_connect, "connect GitHub through a device (opens the browser)")
    _flow_options(sub)
    tree.command(group, "disconnect", _github_disconnect, "remove the GitHub connection")
    group = tree.group("claude", "the Claude subscription")
    sub = tree.command(group, "login", _claude_login, "log a device in to the Claude subscription (relay)")
    sub.add_argument("--mode", choices=["manual", "automatic"], default="manual")
    sub.add_argument("--code", action="store_true", help="send the authorization code from stdin (with --request)")
    _flow_options(sub)
