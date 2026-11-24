"""GitHub identities (ADR-076, key model KM-35; 03-api.md section 4.8, 11-agent-access.md section 8.4; twin of
`web/src/core/github/identities.ts`, `screens/github-accounts`, `settings/sections/github`,
`screens/wg-settings/GithubDefault.tsx` and `screens/task/TaskIdentity.tsx`).

An identity is one `github` credential record, named by its record ID; its owner is the user (a personal account) or
an organization (a company account, sealed to the organization's devices only, which no browser and no agent can
open).

- `github accounts`: `GET /v1/me/github-identities` (metadata and the defaults of organizations and workgroups), then
  every own record (`GET /v1/github-identities/{id}`), checked like `github status` checks the primary one: the answer
  names the user as owner (`owner_kind` `user`), the header names the record (`id`), the user and the kind `github`;
  the author is the user or a device the verified logs entitle to the user's `CK`; signature; the user's `CK` of the
  record's epoch; the body exactly as `contracts/credentials/github.json` allows. Only login, display name, source,
  scopes, whether it holds a signing key, the connection time and whether the platform minted the token are printed,
  never the token or the key seed. A record that does not open is `unverifiable` with its code; a company record is
  `company` with its organization and times (KM-35).
- `github primary --identity`: `PUT /v1/github-identities/{id}/primary`; `github remove --identity`:
  `DELETE /v1/github-identities/{id}` (`404` counts as done); for an own record the result says, as `github
  disconnect` does, whether the devices revoke the token at GitHub (a token the platform minted) or where to revoke it.
- `github default set (--org | --wg) (--identity | --none)`: `PUT /v1/github-defaults/{scope}/{scope_id}`.
- `task github-identity --task (--identity | --none)`: `PUT /v1/tasks/{task}/github-identity`. A `409 peer_outdated`
  of a choice or default names the devices to update (`devices`, `agent_platform.hub`).
- Company accounts (`github connect --org`, `store_company`): the final answer of the device carries the credential
  body for the requesting admin. The tool writes it into the record being reconnected (same ID, version + 1) or a new
  UUIDv7 (version 1) under a fresh 32-byte key of a new epoch of the organization's `cred` scope (the epoch after the
  newest one the hub lists for the organization's records), wrapped (and each wrap signed) for every reader device of
  the organization (the active devices assigned to one of its workgroups, keys from the verified log, decertified
  platform devices left out), the body sealed under it (header owner the organization, author the user), all in one
  `PUT /v1/github-identities/{id}` with `key {epoch, wraps}`; the key is dropped afterwards. One retry after
  `version_conflict` or `epoch_stale` (another admin wrote meanwhile) with a fresh list; when the record's version did
  not change, the conflict was the epoch and the hub's `detail.expected` names it.
- `rewrap_own` (`github rewrap`, and after every rotation of `CK`, `credkey`): own non-primary records under an epoch
  older than the newest own `CK` are re-sealed under it (same ID, version + 1, author the user); records that do not
  open are left alone, a concurrent write (`version_conflict`, `epoch_stale`) waits for the next run.
- `names`: how the operations of a task show their identity (`task operations`): the login of an own account, the
  organization of a company account, `other` for anything else.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from agent_platform.account import Account, unverified
from agent_platform.batch import make_wrap
from agent_platform.commands import Context, Tree
from agent_platform.credentials import SOURCES, valid_github
from agent_platform.devicereq import Keys, device_keys
from agent_platform.errors import PlatformError
from agent_platform.eventio import rows_of, uuid_argument
from agent_platform.hub import segment
from agent_platform.reader import excluded
from agent_runner.crypto.credential import new_header, open_credential, seal_credential, verify_credential
from agent_runner.crypto.encoding import b64u_decode, body_json, format_time, parse_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log.wraps import CRED, WrapSlot, cred_readers

KIND = "github"
USER, ORG = "user", "org"
SCOPES = ("org", "wg")
REVOCABLE_PREFIX = "ghu_"
GITHUB_SETTINGS_URL = "https://github.com/settings/applications"
CONCURRENT = frozenset({"version_conflict", "epoch_stale"})
MAX_EPOCH = 2**31 - 1


class Unverifiable(Exception):
    """An own record that does not open, with its code (a crypto code or `params_invalid`)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Meta:
    """What the hub lists of an identity (`github_identity_meta`)."""

    id: str
    owner_id: str
    owner_kind: str
    primary: bool
    version: int
    epoch: int
    created_at: str
    updated_at: str


# --- reading ---


def _count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _meta(item: dict[str, Any]) -> Meta:
    texts = ("id", "owner_id", "created_at", "updated_at")
    if (
        not all(isinstance(item.get(name), str) for name in texts)
        or item.get("owner_kind") not in (USER, ORG)
        or not isinstance(item.get("primary"), bool)
        or not _count(item.get("version"))
        or not _count(item.get("epoch"))
    ):
        raise unverified("response_invalid")
    return Meta(
        item["id"],
        item["owner_id"],
        item["owner_kind"],
        item["primary"],
        item["version"],
        item["epoch"],
        item["created_at"],
        item["updated_at"],
    )


def _default(item: dict[str, Any]) -> dict[str, Any]:
    texts = ("scope_id", "identity_id", "set_by", "set_at")
    if item.get("scope") not in SCOPES or not all(isinstance(item.get(name), str) for name in texts):
        raise unverified("response_invalid")
    return {name: item[name] for name in ("scope", *texts)}


def list_identities(account: Account) -> tuple[list[Meta], list[dict[str, Any]]]:
    """`GET /v1/me/github-identities`: the identities and the defaults (`response_invalid` when malformed)."""
    answer = account.call("GET", "/v1/me/github-identities")
    if not isinstance(answer.get("identities"), list) or not isinstance(answer.get("defaults"), list):
        raise unverified("response_invalid")
    identities = [_meta(item if isinstance(item, dict) else {}) for item in answer["identities"]]
    defaults = [_default(item if isinstance(item, dict) else {}) for item in answer["defaults"]]
    return identities, defaults


def fetch_identity(account: Account, identity: str) -> dict[str, Any] | None:
    """`GET /v1/github-identities/{id}`: the hub's answer, None when the record is gone."""
    try:
        return account.call("GET", f"/v1/github-identities/{segment(identity)}")
    except PlatformError as error:
        if error.fields.get("hub_error") == "not_found":
            return None
        raise


def _author_key(account: Account, author: object) -> bytes:
    if author == account.user_id:
        return account.identity.sig_public()
    found = device_keys(account, author) if isinstance(author, str) else None
    if found is None:
        raise Unverifiable("author_not_writer")
    return found[1]


def open_own(
    account: Account, identity: str, answer: dict[str, Any], keys: dict[int, bytes]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The checked header and body of the own record `identity` (module docstring); `Unverifiable` otherwise."""
    record = answer.get("record")
    header = record.get("header") if isinstance(record, dict) else None
    if (
        answer.get("owner_id") != account.user_id
        or answer.get("owner_kind") != USER
        or not isinstance(header, dict)
        or header.get("id") != identity
        or header.get("owner") != account.user_id
        or header.get("kind") != KIND
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
    if not isinstance(body, dict) or not valid_github(body):
        raise Unverifiable("params_invalid")
    return checked, body


def account_of(body: dict[str, Any]) -> dict[str, Any]:
    """What may be shown of an opened body (never the token or the key seed)."""
    return {
        "login": body["login"],
        **({"name": body["name"]} if "name" in body else {}),
        "source": body.get("source", SOURCES[0]),
        "scopes": body["scopes"],
        "signing": "signing_key" in body,
        "created_at": body["created_at"],
        "revocable": str(body["token"]).startswith(REVOCABLE_PREFIX),
    }


def _entry(account: Account, meta: Meta, keys: dict[int, bytes]) -> dict[str, Any] | None:
    """One identity as `github accounts` prints it; None when an own record is gone meanwhile."""
    listed = {"id": meta.id, "owner_kind": meta.owner_kind, "primary": meta.primary}
    times = {"version": meta.version, "epoch": meta.epoch, "updated_at": meta.updated_at}
    if meta.owner_kind == ORG:
        return {**listed, "status": "company", "org_id": meta.owner_id, "created_at": meta.created_at, **times}
    answer = fetch_identity(account, meta.id)
    if answer is None:
        return None
    try:
        _header, body = open_own(account, meta.id, answer, keys)
    except Unverifiable as error:
        return {**listed, "status": "unverifiable", "code": error.code, **times}
    return {**listed, "status": "verified", **account_of(body), **times}


def github_accounts(account: Account) -> dict[str, Any]:
    """`github accounts` (module docstring)."""
    identities, defaults = list_identities(account)
    keys = account.cred_keys() if any(meta.owner_kind == USER for meta in identities) else {}
    entries = [_entry(account, meta, keys) for meta in identities]
    return {"identities": [entry for entry in entries if entry is not None], "defaults": defaults}


def names(account: Account) -> dict[str, dict[str, Any]]:
    """The name of every identity the user can see (module docstring); empty when the list cannot be read."""
    try:
        listed = github_accounts(account)["identities"]
    except PlatformError:
        return {}
    found: dict[str, dict[str, Any]] = {}
    for entry in listed:
        if entry["status"] == "verified":
            found[entry["id"]] = {"kind": "login", "login": entry["login"]}
        elif entry["status"] == "company":
            found[entry["id"]] = {"kind": "company", "org_id": entry["org_id"]}
    return found


def name_of(found: dict[str, dict[str, Any]], identity: object) -> dict[str, Any] | None:
    """The name of `identity` (an operation's `github_identity`), None when the operation names none."""
    if not isinstance(identity, str):
        return None
    return found.get(identity, {"kind": "other"})


# --- changes ---


def make_primary(account: Account, identity: str) -> dict[str, Any]:
    account.call("PUT", f"/v1/github-identities/{segment(uuid_argument(identity))}/primary", {})
    return {"identity_id": identity, "primary": True}


def _revocable(account: Account, identity: str) -> bool:
    """Whether the devices revoke the own record's token at GitHub, from the record as it is read now (a record that
    does not open counts as not revocable, so the result names the page)."""
    answer = fetch_identity(account, identity)
    if answer is None:
        return False
    try:
        body = open_own(account, identity, answer, account.cred_keys())[1]
    except Unverifiable:
        return False
    return bool(account_of(body)["revocable"])


def remove(account: Account, identity: str) -> dict[str, Any]:
    """`github remove` (module docstring): for an own record whether the token is revoked at GitHub."""
    uuid_argument(identity)
    identities, _defaults = list_identities(account)
    meta = next((item for item in identities if item.id == identity), None)
    revocable = _revocable(account, identity) if meta is not None and meta.owner_kind == USER else None
    try:
        account.call("DELETE", f"/v1/github-identities/{segment(identity)}")
    except PlatformError as error:
        if error.fields.get("hub_error") != "not_found":
            raise
    result: dict[str, Any] = {"status": "removed", "identity_id": identity}
    if meta is not None:
        result |= {"owner_kind": meta.owner_kind, "primary": meta.primary}
    if revocable is True:
        return {**result, "revoked_at_github": True}
    if revocable is False:
        return {**result, "revoked_at_github": False, "revoke_at": GITHUB_SETTINGS_URL}
    return result


def _choice(args: Any) -> str | None:
    if args.none:
        return None
    return uuid_argument(args.identity)


def set_default(account: Account, scope: str, scope_id: str, identity: str | None) -> dict[str, Any]:
    target = f"/v1/github-defaults/{scope}/{segment(uuid_argument(scope_id))}"
    account.call("PUT", target, {"identity_id": identity})
    return {"scope": scope, "scope_id": scope_id, "identity_id": identity}


def set_task_identity(account: Account, task: str, identity: str | None) -> dict[str, Any]:
    target = f"/v1/tasks/{segment(uuid_argument(task))}/github-identity"
    account.call("PUT", target, {"identity_id": identity})
    return {"task_id": task, "github_identity": identity}


# --- company accounts ---


def org_device(account: Account, org: str, device: str | None) -> tuple[str, Keys]:
    """The device for a company account's `github.connect`: one of `GET /v1/me/devices` listed for `org` (the named
    one, else online first) that the org's verified log entitles to the user's `CK`; only an admin may ask."""
    log = account.org_log(uuid_argument(org))
    if not log.state.is_admin(account.user_id):
        raise PlatformError("not_admin")
    readers = cred_readers([log.state], account.user_id, excluded(account, log)) - {account.user_id}
    wanted = None if device is None else uuid_argument(device)
    rows = [
        row
        for row in rows_of(account.call("GET", "/v1/me/devices"), "devices")
        if row.get("org_id") == org and row.get("device_id") in readers and wanted in (None, row["device_id"])
    ]
    if not rows:
        raise PlatformError("no_device")
    rows.sort(key=lambda row: (row.get("online") is not True, row["device_id"]))
    chosen = str(rows[0]["device_id"])
    found = log.state.principals[chosen]
    return chosen, (b64u_decode(found.kem, 32), b64u_decode(found.sig, 32))


class _Conflict(Exception):
    """A concurrent write of a company record: the version that was sent and the hub's `detail.expected`."""

    def __init__(self, version: int, expected: int | None) -> None:
        super().__init__("conflict")
        self.version = version
        self.expected = expected


def _expected(error: PlatformError) -> int | None:
    value = error.detail.get("expected")
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_EPOCH else None


def _store_once(
    account: Account, org: str, body: dict[str, Any], reconnect: str | None, previous: _Conflict | None
) -> dict[str, Any]:
    log = account.org_log(org)
    if not log.state.is_admin(account.user_id):
        raise PlatformError("not_admin")
    readers = sorted(cred_readers([log.state], org, excluded(account, log)))
    if not readers:
        raise PlatformError("no_device")
    identities, _defaults = list_identities(account)
    records = [meta for meta in identities if meta.owner_kind == ORG and meta.owner_id == org]
    known = next((meta for meta in records if meta.id == reconnect), None)
    if reconnect is not None and known is None:
        raise PlatformError("not_found")
    expected = 0 if known is None else known.version
    epoch = max([0, *(meta.epoch for meta in records)]) + 1
    if previous is not None and previous.version == expected and previous.expected is not None:
        epoch = max(epoch, previous.expected)  # the version was right: the hub named the epoch it expects
    record_id = uuid7() if known is None else known.id
    key = secrets.token_bytes(32)
    kems = {reader: b64u_decode(log.state.principals[reader].kem, 32) for reader in readers}
    wraps = [make_wrap(account.identity, WrapSlot(CRED, org, epoch, r), kems[r], key) for r in readers]
    fields = new_header(
        id=record_id,
        owner=org,
        kind=KIND,
        version=expected + 1,
        epoch=epoch,
        author=account.user_id,
        ts=format_time(datetime.now(UTC)),
    )
    record = seal_credential(key, fields, body_json(body), account.identity.sig)
    del key  # the admin cannot read the record back (module docstring)
    put = {"owner_id": org, "record": record, "expected_version": expected, "key": {"epoch": epoch, "wraps": wraps}}
    try:
        answer = account.call("PUT", f"/v1/github-identities/{segment(record_id)}", put)
    except PlatformError as error:
        if error.fields.get("hub_error") in CONCURRENT:
            raise _Conflict(expected, _expected(error)) from None
        raise
    if answer.get("version") != expected + 1:
        raise unverified("response_invalid")
    return {"identity_id": record_id, "version": expected + 1, "epoch": epoch}


def store_company(account: Account, org: str, body: dict[str, Any], reconnect: str | None) -> dict[str, Any]:
    """Seal and store the body of a company account of `org` (module docstring); one retry after a concurrent write."""
    try:
        return _store_once(account, org, body, reconnect, None)
    except _Conflict as conflict:
        account.forget(org)
        try:
            return _store_once(account, org, body, reconnect, conflict)
        except _Conflict:
            raise PlatformError("hub_refused", hub_error="version_conflict", status=409) from None


# --- re-sealing under the newest own key ---


def _reseal(account: Account, meta: Meta, keys: dict[int, bytes], latest: int) -> bool:
    answer = fetch_identity(account, meta.id)
    if answer is None:
        return False
    try:
        header, body = open_own(account, meta.id, answer, keys)
    except Unverifiable:
        return False
    version = int(str(header["version"]))
    fields = new_header(
        id=meta.id,
        owner=account.user_id,
        kind=KIND,
        version=version + 1,
        epoch=latest,
        author=account.user_id,
        ts=format_time(datetime.now(UTC)),
    )
    record = seal_credential(keys[latest], fields, body_json(body), account.identity.sig)
    put = {"owner_id": account.user_id, "record": record, "expected_version": version}
    try:
        account.call("PUT", f"/v1/github-identities/{segment(meta.id)}", put)
    except PlatformError as error:
        if error.fields.get("hub_error") in CONCURRENT:
            return False
        raise
    return True


def rewrap_own(account: Account) -> int:
    """Own non-primary records under an older `CK` epoch, re-sealed under the newest (module docstring); the count."""
    identities, _defaults = list_identities(account)
    own = [meta for meta in identities if meta.owner_kind == USER and meta.owner_id == account.user_id]
    stale = [meta for meta in own if not meta.primary]
    keys = account.cred_keys() if stale else {}
    if not keys:
        return 0
    latest = max(keys)
    return sum(1 for meta in stale if meta.epoch < latest and _reseal(account, meta, keys, latest))


# --- the connect of a company account (`github connect --org`) ---


def connected_company(account: Account, args: Any, found: dict[str, Any]) -> dict[str, Any]:
    """The result of a finished company connect: the body stored for `args.org`, never printed."""
    body = found.get("body")
    if not isinstance(body, dict):
        raise PlatformError("device_request_failed", failure="params_invalid")
    stored = store_company(account, args.org, body, args.identity)
    return {
        "status": "connected",
        "identity_id": stored["identity_id"],
        "owner": ORG,
        "org_id": args.org,
        "version": stored["version"],
        "login": body["login"],
        "source": body.get("source", SOURCES[0]),
        **({"signing": found["signing"]} if "signing" in found else {}),
    }


def connect_body(args: Any) -> dict[str, Any]:
    """The `github.connect` request options: the company account's organization, the own record to reconnect (a
    company record is chosen by this client, not by the device) and the signing key."""
    body: dict[str, Any] = {}
    identity = None if args.identity is None else uuid_argument(args.identity)
    if args.org is not None:
        body["org_id"] = uuid_argument(args.org)
    elif identity is not None:
        body["identity_id"] = identity
    if args.signing_key:
        body["signing_key"] = True
    return body


# --- commands ---


def _accounts(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return github_accounts(account)


def _primary(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return make_primary(account, args.identity)


def _remove(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return remove(account, args.identity)


def _default_set(account: Account, args: Any, context: Context) -> dict[str, Any]:
    scope, scope_id = (ORG, args.org) if args.org is not None else ("wg", args.wg)
    return set_default(account, scope, scope_id, _choice(args))


def _rewrap(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return {"resealed": rewrap_own(account)}


def _task_identity(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return set_task_identity(account, args.task, _choice(args))


def _choice_options(sub: Any) -> None:
    choice = sub.add_mutually_exclusive_group(required=True)
    choice.add_argument("--identity", help="the identity's record ID (from `github accounts`)")
    choice.add_argument("--none", action="store_true", help="clear the choice (automatic)")


def register(tree: Tree) -> None:
    group = tree.group("github", "the GitHub connection")
    tree.command(
        group,
        "accounts",
        _accounts,
        "own and company GitHub accounts and the defaults (never a token)",
        example="agent-platform github accounts",
        words="githubkonten firmenkonto identitäten konten accounts identities company",
        then="agent-platform github primary --identity <identity_id>",
    )
    sub = tree.command(
        group,
        "primary",
        _primary,
        "make an own GitHub account the primary one",
        example="agent-platform github primary --identity <identity_id>",
        words="hauptkonto hauptidentität primär primary main",
    )
    sub.add_argument("--identity", required=True)
    sub = tree.command(
        group,
        "remove",
        _remove,
        "remove a GitHub account (own, or a company one as admin)",
        writes=True,
        example="agent-platform github remove --identity <identity_id>",
        words="kontoentfernen identitätlöschen removeaccount deleteidentity",
    )
    sub.add_argument("--identity", required=True)
    tree.command(
        group,
        "rewrap",
        _rewrap,
        "re-seal own further accounts under the newest credential key",
        writes=True,
        example="agent-platform github rewrap",
        words="neuversiegeln umschlüsseln reseal rewrap",
    )
    defaults = tree.group("default", "default GitHub identities of organizations and workgroups", group)
    sub = tree.command(
        defaults,
        "set",
        _default_set,
        "set or clear the default GitHub identity of an org or workgroup",
        example="agent-platform github default set --org <org_id> --identity <identity_id>",
        words="standardidentität voreinstellung defaultidentity preset",
    )
    scope = sub.add_mutually_exclusive_group(required=True)
    scope.add_argument("--org")
    scope.add_argument("--wg")
    _choice_options(sub)
    tasks = tree.group("task", "tasks (sealed and opened on this computer)")
    sub = tree.command(
        tasks,
        "github-identity",
        _task_identity,
        "choose the task's GitHub identity",
        example="agent-platform task github-identity --task <task_id> --identity <identity_id>",
        words="aufgabenidentität taskidentity githubidentität taskaccount",
    )
    sub.add_argument("--task", required=True)
    _choice_options(sub)
