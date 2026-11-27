"""Credential records and the device flows around them (06-web.md sections 8.1 and 8.2, 03-api.md sections 4.8 and
4.10; twins of `web/src/core/credentials/records.ts`, `credentials/login.ts` and `github/connection.ts`).

- `credentials list`: `GET /v1/me/credentials`, every record opened and checked like the web (owner the user, kind
  as listed, author the user or a device the verified logs entitle to the user's `CK`, signature, `CK` of the record's
  epoch, body exactly as `contracts/credentials/<kind>.json` allows); only the non-secret summary is printed, never a
  token, key or password. A record that fails is `unverifiable` with its code.
- `credentials set-mailbox`: the mailbox of the login automation; the password comes from stdin; the name, purpose
  and release of the stored primary mailbox stay (ADR-099).
- `credentials set-dfx-debug`: the debug sign-in of the DFX API (ADR-102): the signature from stdin (never on the
  command line), `--address`, `--api-url`, `--release <wg>` (repeatable; without it the stored release stays).
  `credentials release-dfx-debug --wg <wg>... | --none` changes only the release. The list shows the API URL and the
  released workgroups, never the address or the signature.
- `credentials delete --kind`: `DELETE /v1/me/credentials/{kind}` (`404` counts as done).
- `credentials check --kind`: the device request `credential.check` (API key, subscription) or `mailbox.check`.
- `github status|connect|disconnect`: the GitHub connection (the `github` record, written by the device that made the
  connection): its login, scopes, source (`device_flow` or `token`; records without it came from the device flow),
  creation time and whether the token is one the platform minted (`revocable`: made by a device flow), never
  the token. `connect` sends `github.connect` and opens the verification page in the user's browser with the user
  code; `connect --token -` (ADR-065) reads an existing token (a GitHub CLI login, `gh auth token`, or a personal
  access token) from stdin (never from the command line), trims it like the web's field and sends it only inside the
  request sealed with HPKE to the device, which checks it at GitHub and stores it (`done` with login, scopes and
  source, or a failure such as `token_refused`, `scope_missing`, `sso_required`). `disconnect` deletes the record; a
  revocable token is revoked at GitHub by the devices, any other stays valid there (`revoke_at` names the page).
- `github connect --from-gh` (KOR-65, rule E-29): the same as `--token -` with the token of this computer's GitHub CLI
  login (`gh auth token --hostname github.com`, `AGENT_PLATFORM_GH` names another `gh`); it reaches every repository
  the account can reach. No login is `github_login_missing`. `github connect --app oauth_app|github_app` asks for that
  device flow (default: the device's preferred one, the OAuth App when set up).
- GitHub identities (ADR-078, `agent_platform.identities`): `connect --identity` reconnects into that own record,
  `--signing-key` asks the device for an own signing key (`signing` in the answer), `--org` connects a company account
  of an organization the user is an admin of: the request goes to a device of that organization and names it in
  plaintext as well; the device's final answer carries the credential body, which the tool seals for the
  organization's devices and stores (`identities.store_company`; with `--identity` into that company record), then
  prints only `{status, identity_id, owner, org_id, version, login, source, signing?}`. A continued company flow
  (`--request`) needs `--org` (and `--identity`) again; the body is never printed. These options combine with each way
  of connecting: `--from-gh --org` stores the GitHub CLI login as a company account like `--token - --org`.
- `claude login`: the relay of the web (`claude.login {mode}`): the device answers the authorization link (opened in
  the user's browser), the code the user receives goes back as a follow-up request (stdin), the device stores the
  subscription record itself.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from typing import Any, cast

from agent_platform import devicereq, identities
from agent_platform.account import Account
from agent_platform.commands import Context, Tree
from agent_platform.credentials import (
    KINDS,
    MAX_RELEASES,
    PRINTABLE_RE,
    SOURCES,
    put_record,
    read_record,
    revocable,
    valid_body,
    valid_dfx_debug,
    valid_mailbox,
)
from agent_platform.devicereq import choose_device, finish, resume, show_in_browser, submit, wait
from agent_platform.errors import PlatformError
from agent_platform.eventio import rows_of, uuid_argument
from agent_runner.crypto.credential import open_credential, verify_credential
from agent_runner.crypto.encoding import parse_json
from agent_runner.crypto.errors import CryptoError

CHECK_KINDS = ("anthropic_api_key", "claude_subscription", "mailbox")
GITHUB_SETTINGS_URL = "https://github.com/settings/applications"
DEFAULT_WAIT_S = 60
GH_HOST = "github.com"
GH_TIMEOUT_S = 30


def summary(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    """What of a checked body may be shown: never a token, key or password."""
    if kind == "github":
        return {
            "login": body["login"],
            "scopes": body["scopes"],
            "source": body.get("source", SOURCES[0]),
            "created_at": body["created_at"],
            # minted by a device flow (the GitHub App's `ghu_` or the OAuth App's `gho_`, KOR-65): revoked on disconnect
            "revocable": revocable(body),
        }
    if kind == "claude_subscription":
        return {"email": body["email"], "origin": body["kind"], "expires_at": body.get("expires_at")}
    if kind == "mailbox":
        return {"email": body["email"], "host": body["imap_host"]}
    if kind == "dfx_debug":
        return {"api_url": body["api_url"], "wgs": list(body.get("use", {"wgs": []})["wgs"])}
    return {}


class Unverifiable(Exception):
    """A record that does not open, with its code (a crypto code or `params_invalid`)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _author_key(account: Account, author: object) -> bytes:
    if isinstance(author, str) and (author in (account.user_id, account.actor) or author in account.former_agents()):
        return account.author_key(author)
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
    if not isinstance(body, dict) or not valid_body(kind, body):
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


KEPT_MAILBOX_FIELDS = ("name", "purpose", "use")


def keep_release(body: Mapping[str, Any], _record: object, current: Mapping[str, Any] | None) -> None:
    """Keep the name, purpose and release of the stored primary mailbox (ADR-099 section 2) in a new version."""
    for key in KEPT_MAILBOX_FIELDS:
        if current is not None and key in current:
            cast(dict[str, Any], body).setdefault(key, current[key])


def set_mailbox(account: Account, args: Any, password: str) -> dict[str, Any]:
    body = {
        "imap_host": args.imap_host.strip(),
        "imap_port": args.imap_port,
        "imap_user": args.imap_user.strip(),
        "imap_password": password.removesuffix("\n"),
        "email": args.email.strip(),
    }
    if not valid_mailbox(body):
        raise PlatformError("invalid_argument")
    return {**put_record(account, "mailbox", body, keep_release), "kind": "mailbox"}


DFX_DEBUG = "dfx_debug"


def _releases(values: list[str] | None) -> list[str]:
    wgs = sorted({uuid_argument(wg) for wg in values or []})
    if len(wgs) > MAX_RELEASES:
        raise PlatformError("invalid_argument", reason="wg")
    return wgs


def keep_dfx_release(body: Mapping[str, Any], _record: object, current: Mapping[str, Any] | None) -> None:
    """Keep the release of the stored debug sign-in in a new version written without `--release` (ADR-102 section 3)."""
    if current is not None and "use" in current:
        cast(dict[str, Any], body).setdefault("use", current["use"])


def set_dfx_debug(account: Account, args: Any, text: str) -> dict[str, Any]:
    """`credentials set-dfx-debug`: the debug sign-in of the DFX API (ADR-102 section 2). The signature (the secret)
    comes from standard input only, never from the command line, like the password of `set-mailbox`; the wallet
    address and the API URL are options. `--release` replaces the release, without it the stored release stays."""
    wgs = _releases(args.release)
    body: dict[str, Any] = {
        "address": args.address.strip(),
        "signature": text.strip(),
        "api_url": args.api_url.strip().rstrip("/"),
    }
    if wgs:
        body["use"] = {"wgs": wgs}
    if not valid_dfx_debug(body):
        raise PlatformError("invalid_argument", reason="dfx_debug")
    result = put_record(account, DFX_DEBUG, body, None if wgs else keep_dfx_release)
    released = list(body.get("use", {"wgs": []})["wgs"])
    body.clear()
    return {**result, "kind": DFX_DEBUG, "api_url": args.api_url.strip().rstrip("/"), "wgs": released}


def release_dfx_debug(account: Account, args: Any) -> dict[str, Any]:
    """`credentials release-dfx-debug`: release the stored debug sign-in to exactly these workgroups (`--none`:
    to none); the record is opened, changed and sealed again, the sign-in itself is not asked for again."""
    wgs = _releases(args.wg)
    if args.none == bool(wgs):
        raise PlatformError("invalid_argument", reason="wg")
    _record, body = read_record(account, DFX_DEBUG)
    if body is None or not valid_dfx_debug(body):
        raise PlatformError("invalid_argument", reason="dfx_debug_missing")
    body.pop("use", None)
    if wgs:
        body["use"] = {"wgs": wgs}
    result = put_record(account, DFX_DEBUG, body)
    body.clear()
    return {**result, "kind": DFX_DEBUG, "wgs": wgs}


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


def token_input(text: str) -> str:
    """The token without surrounding white space (a copied line ends in a newline), as the web's `tokenInput`."""
    token = text.strip()
    if not PRINTABLE_RE.fullmatch(token):
        raise PlatformError("invalid_argument", reason="token_missing")
    return token


def gh_token(environ: Mapping[str, str]) -> str:
    """The token of this computer's GitHub CLI login (`gh auth token`), never printed or logged."""
    program = environ.get("AGENT_PLATFORM_GH", "") or shutil.which("gh", path=environ.get("PATH"))
    if not program or not os.access(program, os.X_OK):
        raise PlatformError("github_login_missing", reason="gh_missing")
    try:
        done = subprocess.run(
            [program, "auth", "token", "--hostname", GH_HOST],
            capture_output=True,
            text=True,
            timeout=GH_TIMEOUT_S,
            env=dict(environ),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise PlatformError("github_login_missing", reason="gh_failed") from None
    token = done.stdout.strip()
    if done.returncode != 0 or not PRINTABLE_RE.fullmatch(token):
        raise PlatformError("github_login_missing", reason="gh_not_logged_in")
    return token


def _connected(account: Account, args: Any, prepared: dict[str, Any], found: dict[str, Any]) -> dict[str, Any]:
    """The result of a step; a company account's final answer is stored (never printed), any other `body` refused."""
    result = finish(prepared, found)
    if found["step"] != "done":
        return result
    if args.org is not None:
        return {
            "request_id": prepared["id"],
            "device_id": prepared["device"],
            **identities.connected_company(account, args, found),
        }
    if "body" in found:
        raise PlatformError("invalid_argument", reason="org_missing", request_id=prepared["id"])
    return result


def github_connect(account: Account, args: Any, context: Context) -> dict[str, Any]:
    ways = [args.token is not None, args.from_gh, args.app is not None]
    if (args.token is not None and args.token != "-") or sum(ways) > 1 or (any(ways) and args.request is not None):
        raise PlatformError("invalid_argument")
    options = identities.connect_body(args)
    if args.request is not None:
        prepared = resume(account, args.request, ("github.connect",))
        return _connected(account, args, prepared, wait(account, prepared, args.wait, frozenset({"done", "failed"})))
    if args.org is not None:
        target = identities.org_device(account, args.org, args.device)
    else:
        target = choose_device(account, args.device)
    if args.token is not None or args.from_gh:
        token = gh_token(context.environ) if args.from_gh else token_input(context.read_stdin())
        prepared = submit(account, target, "github.connect", {"token": token, **options}, org=args.org)
        return _connected(account, args, prepared, wait(account, prepared, args.wait, frozenset({"done", "failed"})))
    body = options if args.app is None else {"app": args.app, **options}
    prepared = submit(account, target, "github.connect", body, org=args.org)
    found = wait(account, prepared, args.wait, frozenset({"device_code", "done", "failed"}))
    if found["step"] == "device_code":
        show_in_browser(context.environ, found["verification_uri"], f"enter the code {found['user_code']} at")
    return _connected(account, args, prepared, found)


def github_disconnect(account: Account) -> dict[str, Any]:
    """Like the web's section: whether the devices revoke the token at GitHub follows from the connection as it was
    read before; a token the platform did not mint stays valid at GitHub (`revoke_at`)."""
    revocable = github_status(account).get("revocable") is True
    credentials_delete(account, "github")
    if revocable:
        return {"status": "disconnected", "revoked_at_github": True}
    return {"status": "disconnected", "revoked_at_github": False, "revoke_at": GITHUB_SETTINGS_URL}


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


def _dfx_debug_command(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return set_dfx_debug(account, args, context.read_stdin())


def _dfx_release_command(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return release_dfx_debug(account, args)


def _check(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return credentials_check(account, args, context.environ)


def _github_status(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return github_status(account)


def _github_connect(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return github_connect(account, args, context)


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
    tree.command(
        group,
        "list",
        _list,
        "the user's credentials (summaries only, never a secret)",
        example="agent-platform credentials list",
        words="zugangsdaten anmeldedaten gespeichert übersicht secrets stored logins",
        trim="first",
    )
    sub = tree.command(
        group,
        "set-mailbox",
        _mailbox_command,
        "store the login mailbox (password on stdin)",
        writes=True,
        example="printf '%s' \"$PASSWORD\" | agent-platform credentials set-mailbox --imap-host imap.example.com "
        "--imap-user me --email me@example.com",
        words="postfach email mail imap passwort hinterlegen mailbox password inbox",
    )
    sub.add_argument("--imap-host", required=True)
    sub.add_argument("--imap-port", type=int, default=993)
    sub.add_argument("--imap-user", required=True)
    sub.add_argument("--email", required=True)
    sub = tree.command(
        group,
        "set-dfx-debug",
        _dfx_debug_command,
        "store the DFX API debug sign-in (signature on stdin); sessions of your tasks query "
        "the debug endpoint read-only (dfx.debug) only in the workgroups it is released to",
        writes=True,
        example="printf '%s' \"$SIGNATURE\" | agent-platform credentials set-dfx-debug --address <wallet address> "
        "--api-url https://api.example.com/v1 --release <wg_id>",
        words="dfx debug datenbank produktion prod zugang signatur wallet freigeben database production signature",
    )
    sub.add_argument("--address", required=True, help="the address of the wallet with the Debug role")
    sub.add_argument("--api-url", required=True, help="base URL of the DFX API including /v1 (https)")
    sub.add_argument(
        "--release", action="append", help="release it to this workgroup ID (repeatable; without: the stored release)"
    )
    sub = tree.command(
        group,
        "release-dfx-debug",
        _dfx_release_command,
        "release the stored DFX API debug sign-in to exactly these workgroups (--none: to none)",
        writes=True,
        example="agent-platform credentials release-dfx-debug --wg <wg_id>",
        words="dfx debug freigeben freigabe workgroup entziehen release revoke",
    )
    sub.add_argument("--wg", action="append", help="a workgroup ID (repeatable)")
    sub.add_argument("--none", action="store_true", help="release it to no workgroup")
    sub = tree.command(
        group,
        "delete",
        _delete,
        "delete a credential",
        writes=True,
        example="agent-platform credentials delete --kind mailbox",
        words="zugangsdaten löschen entfernen vergessen remove erase forget",
    )
    sub.add_argument("--kind", required=True, choices=list(KINDS))
    sub = tree.command(
        group,
        "check",
        _check,
        "let a device check a stored credential",
        example="agent-platform credentials check --kind claude_subscription",
        words="prüfen testen funktioniert gültig zugangsdaten verify test valid works",
        then="agent-platform credentials check --request <request_id>",
    )
    sub.add_argument("--kind", choices=list(CHECK_KINDS))
    _flow_options(sub)
    group = tree.group("github", "the GitHub connection")
    tree.command(
        group,
        "status",
        _github_status,
        "connected GitHub login, if any",
        example="agent-platform github status",
        words="verbunden githubkonto konto login connected account",
    )
    sub = tree.command(
        group,
        "connect",
        _github_connect,
        "connect GitHub: this computer's GitHub CLI login (--from-gh, no browser), a token or the device page",
        writes=True,
        example="agent-platform github connect --from-gh",
        words="verbinden anmelden repository zugriff token link login repo access gh cli fork organization",
        then="agent-platform github connect --request <request_id> --wait 120",
    )
    sub.add_argument("--token", help="- : store an existing GitHub token read from stdin (checked by the device)")
    sub.add_argument(
        "--from-gh", action="store_true", help="store the token of this computer's GitHub CLI login (`gh auth token`)"
    )
    sub.add_argument(
        "--app", choices=["oauth_app", "github_app"], help="the device flow to use (default: the device's preferred)"
    )
    sub.add_argument("--org", help="a company account of this organization (admins; stored for its devices only)")
    sub.add_argument("--identity", help="reconnect into this identity record (from `github accounts`)")
    sub.add_argument("--signing-key", action="store_true", help="also create and register an own SSH signing key")
    _flow_options(sub)
    tree.command(
        group,
        "disconnect",
        _github_disconnect,
        "remove the GitHub connection",
        writes=True,
        example="agent-platform github disconnect",
        words="trennen abmelden entfernen unlink logout remove",
    )
    group = tree.group("claude", "the Claude subscription")
    sub = tree.command(
        group,
        "login",
        _claude_login,
        "log a device in to the Claude subscription (relay)",
        writes=True,
        example="agent-platform claude login",
        words="einloggen anmelden gerät abo device subscription sign in",
        then="agent-platform claude login --request <request_id> --code",
    )
    sub.add_argument("--mode", choices=["manual", "automatic"], default="manual")
    sub.add_argument("--code", action="store_true", help="send the authorization code from stdin (with --request)")
    _flow_options(sub)
