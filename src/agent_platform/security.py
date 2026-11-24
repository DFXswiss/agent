"""Passkeys, recovery code, connected agents, platform invitations and the security page (11-agent-access.md sections
4, 5.3 and 8.4; twins of `web/src/core/account/passkeys.ts`, `account/recover.ts`, `agents/list.ts`,
`platform/invites.ts` and `web/src/ui/screens/security/build.ts`).

- `passkey list`: the passkeys of `GET /v1/me` and whether a recovery code exists (the recovery slot is not the unset
  slot of a registration without code, which only the identity can recognize: an agent with its own identity says
  `null`, unknown).
- `passkey remove --id`: `DELETE /v1/passkeys/{id}` (the hub refuses the last one with `last_passkey`).
- `passkey add`: a passkey needs the user's authenticator, so the tool opens the settings page in the user's browser.
- `recovery new`: draws a recovery code, seals the identity for the recovery slot with its `KEK_rec` and replaces
  the slot (`PUT /v1/me/bundles/recovery {bundle_ct, new_recovery_id}`; the agent's signed request is the proof the
  web client gets from a passkey assertion). The code is printed once (`secret_fields`). It needs the user's identity:
  an agent with its own identity opens the settings page in the user's browser instead, as for a passkey (ADR-082).
- `agents list`: `GET /v1/me/agents`, each label opened with the identity (an unknown agent when it does not open;
  always for an agent with its own identity, which holds no key the label is sealed to); `agents revoke`:
  `revoke.py`.
- `platform invite|list|revoke`: platform user invitations of users with the platform role `inviter`, or of every
  user while the switch `open_platform_invites` is on (both read from the verified platform log, never from the hub's
  word; ADR-079): the code is drawn here, only `invite_id`, `pk_inv` and the expiry go to the hub; the link is printed
  once. Without the role the hub lists and revokes only the user's own invitations.
- `platform settings` (ADR-079): the switches, the organizations granted the operator's subscriptions and the caller's
  platform roles of the verified platform log.
- `security`: what the security page shows: the hash of the delivered `build-manifest.json`, its version and commit,
  whether its root key fingerprint is the pinned platform root key's, and the pinned hub key.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.eventio import rows_of
from agent_platform.hub import ANON_TIMEOUT_S, segment
from agent_platform.pairing import open_browser
from agent_platform.revoke import agents_revoke
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.bundle import RECOVERY_SLOT, seal_bundle
from agent_runner.crypto.encoding import P, b64u_decode, format_time, is_b64u
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.kdf import hkdf, kek_from_recovery_code
from agent_runner.crypto.keys import Identity, ed25519_seed
from agent_runner.crypto.log.entries import PLATFORM_POLICIES, PLATFORM_ROLES
from agent_runner.crypto.recovery import format_recovery_code, generate_recovery_code, recovery_id

NONCE_BYTES = 12
RECOVERY_UNSET = b"recovery-unset\x00"
INVITER = "inviter"
OPEN_INVITES = "open_platform_invites"
INVITE_HOURS = 72
INVITE_HOURS_MAX = 720
INVITE_SKEW = timedelta(seconds=120)
MANIFEST_PATH = "/build-manifest.json"
MANIFEST_MAX = 64 * 1024
PASSKEY_FIELDS = ("credential_id", "created_at", "last_used_at", "prf_seen")
AGENT_FIELDS = (
    "agent_id",
    "state",
    "created_at",
    "confirmed_at",
    "claimed_at",
    "last_used_at",
    "revoked_at",
    "replaces",
    "own_identity",
    "held_identity",
    "gen",
    "revoked_reason",
)
INVITE_FIELDS = ("invite_id", "kind", "state", "created_by", "created_at", "expires_at", "registered", "used_at")


# --- passkeys and recovery ---


def unset_slot(identity: Identity, user_id: str, bundle_ct: str) -> bool:
    """The recovery slot is the unset one (no code was ever shown): its nonce is derived from the identity."""
    nonce = hkdf(ed25519_seed(identity.sig), P + RECOVERY_UNSET + user_id.encode())[:NONCE_BYTES]
    return b64u_decode(bundle_ct)[:NONCE_BYTES] == nonce


def recovery_code_exists(account: Account) -> bool | None:
    """Whether a recovery code was set; None (unknown) without the user's identity."""
    try:
        answer = account.call("GET", "/v1/me/bundles/recovery")
    except PlatformError as error:
        if error.fields.get("hub_error") == "not_found":
            return False
        raise
    bundle = answer.get("bundle_ct")
    if not isinstance(bundle, str) or not is_b64u(bundle):
        raise unverified("response_invalid")
    return None if account.identity is None else not unset_slot(account.identity, account.user_id, bundle)


def passkey_list(account: Account) -> dict[str, Any]:
    items = account.me().get("passkeys")
    if not isinstance(items, list):
        raise unverified("response_invalid")
    passkeys = [
        {key: item.get(key) for key in PASSKEY_FIELDS}
        for item in items
        if isinstance(item, dict) and isinstance(item.get("credential_id"), str)
    ]
    return {"passkeys": passkeys, "recovery_code": recovery_code_exists(account)}


def passkey_remove(account: Account, credential_id: str) -> dict[str, Any]:
    if not is_b64u(credential_id) or not credential_id:
        raise PlatformError("invalid_argument")
    account.call("DELETE", f"/v1/passkeys/{credential_id}")
    return {"removed": credential_id}


def passkey_add(account: Account, environ: Any) -> dict[str, Any]:
    url = f"{account.origin}/settings#settings-passkeys"
    open_browser(environ, url, f"agent-platform: open {url} and choose 'Add passkey'")
    return {"opened": url}


def recovery_new(account: Account, environ: Any) -> dict[str, Any]:
    if account.identity is None:
        url = f"{account.origin}/settings#settings-passkeys"
        open_browser(environ, url, f"agent-platform: open {url} and choose 'New recovery code'")
        return {"opened": url}
    raw = generate_recovery_code()
    code = format_recovery_code(raw)
    body = {
        "bundle_ct": seal_bundle(kek_from_recovery_code(raw), account.user_id, RECOVERY_SLOT, account.identity),
        "new_recovery_id": recovery_id(raw),
    }
    try:
        account.call("PUT", "/v1/me/bundles/recovery", body)
    except PlatformError as error:
        if error.fields.get("hub_error") != "forbidden":
            raise
        stored = account.call("GET", "/v1/me/bundles/recovery")
        if stored.get("bundle_ct") != body["bundle_ct"]:
            raise
    return {"recovery_code": code, "secret_fields": ["recovery_code"]}


# --- agents ---


def agents_list(account: Account) -> dict[str, Any]:
    agents = []
    for row in rows_of(account.call("GET", "/v1/me/agents"), "agents"):
        agent, sealed = row.get("agent_id"), row.get("label_ct")
        if not isinstance(agent, str) or not isinstance(sealed, str):
            continue
        label: dict[str, str] | None = None
        if account.identity is not None:
            try:
                label = agent_crypto.open_label(account.identity.kem, agent, account.user_id, sealed)
            except (CryptoError, ValueError):
                label = None
        agents.append(
            {**{key: row.get(key) for key in AGENT_FIELDS}, "label": label, "this": agent == account.agent_id}
        )
    return {"agents": agents}


# --- platform invitations ---


def _inviter(account: Account) -> None:
    """The role `inviter`, or anybody while the switch `open_platform_invites` is on (ADR-079), as the verified
    platform log says."""
    platform = account.platform()
    if not (platform.has_role(account.user_id, INVITER) or platform.policy(OPEN_INVITES)):
        raise PlatformError("not_inviter")


def platform_settings(account: Account) -> dict[str, Any]:
    """The switches, the organizations granted the operator's subscriptions and the caller's platform roles of the
    verified platform log (ADR-079); `may_invite` as `platform invite` decides."""
    platform = account.platform()
    roles = [role for role in PLATFORM_ROLES if platform.has_role(account.user_id, role)]
    return {
        "policies": {name: platform.policy(name) for name in PLATFORM_POLICIES},
        "operator_subscriptions": sorted(org for org, granted in platform.operator_subscriptions.items() if granted),
        "roles": roles,
        "may_invite": INVITER in roles or platform.policy(OPEN_INVITES),
    }


def platform_invite(account: Account, hours: int) -> dict[str, Any]:
    if not 1 <= hours <= INVITE_HOURS_MAX:
        raise PlatformError("invalid_argument")
    _inviter(account)
    code = invite_format.generate_code()
    secrets = invite_format.invite_secrets(code)
    span = min(timedelta(hours=hours), timedelta(hours=INVITE_HOURS_MAX) - INVITE_SKEW)
    expires = format_time(datetime.now(UTC) + span)
    body = {"invite_id": secrets.invite_id, "pk_inv": secrets.pk_inv, "kind": "user", "expires_at": expires}
    account.call("POST", "/v1/platform/invites", body)
    link = f"{account.origin}/invite#{invite_format.format_code(code)}"
    return {
        "invite_id": secrets.invite_id,
        "expires_at": expires,
        "invite_link": link,
        "secret_fields": ["invite_link"],
    }


def platform_list(account: Account) -> dict[str, Any]:
    _inviter(account)
    rows = rows_of(account.call("GET", "/v1/platform/invites"), "invites")
    return {"invites": [{key: row.get(key) for key in (*INVITE_FIELDS, "revoked_at")} for row in rows]}


def platform_revoke(account: Account, invite_id: str) -> dict[str, Any]:
    _inviter(account)
    if not is_b64u(invite_id) or not invite_id:
        raise PlatformError("invalid_argument")
    account.call("POST", f"/v1/platform/invites/{segment(invite_id)}/revoke", {})
    return {"revoked": invite_id}


# --- security page ---


def security(account: Account) -> dict[str, Any]:
    root = account.platform_root
    pinned = hashlib.sha256(b64u_decode(root, 32)).hexdigest() if root else None
    try:
        with httpx.Client(timeout=ANON_TIMEOUT_S, follow_redirects=False, trust_env=False) as client:
            response = client.get(account.origin + MANIFEST_PATH, headers={"Cache-Control": "no-store"})
    except httpx.HTTPError:
        raise PlatformError("hub_unreachable") from None
    raw = response.content[:MANIFEST_MAX]
    build = _manifest(raw) if response.status_code == 200 else None
    return {
        "hub": account.origin,
        "hub_key": account.state.get("hub_key"),
        "root_key_fingerprint": pinned,
        "build": build,
        "build_matches_root_key": build is not None and build["root_key_fingerprint"] == pinned,
    }


def _manifest(raw: bytes) -> dict[str, Any] | None:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(manifest, dict):
        return None
    fields = {key: manifest.get(key) for key in ("version", "commit", "root_key_fingerprint")}
    if not all(isinstance(value, str) and value for value in fields.values()):
        return None
    return {"hash": hashlib.sha256(raw).hexdigest(), **fields}


# --- commands ---


def _passkey_list(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return passkey_list(account)


def _passkey_remove(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return passkey_remove(account, args.id)


def _passkey_add(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return passkey_add(account, context.environ)


def _recovery_new(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return recovery_new(account, context.environ)


def _agents_list(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return agents_list(account)


def _agents_revoke(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return agents_revoke(account, args.id, args.all)


def _platform_invite(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return platform_invite(account, args.expires_hours)


def _platform_list(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return platform_list(account)


def _platform_revoke(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return platform_revoke(account, args.id)


def _platform_settings(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return platform_settings(account)


def _security(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return security(account)


def register(tree: Tree) -> None:
    group = tree.group("passkey", "the user's passkeys")
    tree.command(
        group,
        "list",
        _passkey_list,
        "passkeys and whether a recovery code exists",
        example="agent-platform passkey list",
        words="anmeldeschlüssel sicherheitsschlüssel wiederherstellung security key",
        trim="first",
    )
    sub = tree.command(
        group,
        "remove",
        _passkey_remove,
        "remove a passkey (not the last one)",
        example="agent-platform passkey remove --id <credential_id>",
        words="passkey entfernen anmeldeschlüssel löschen delete security key",
    )
    sub.add_argument("--id", required=True, help="credential_id of `passkey list`")
    tree.command(
        group,
        "add",
        _passkey_add,
        "open the settings page to add a passkey in the browser",
        example="agent-platform passkey add",
        words="passkey hinzufügen anmeldeschlüssel neu register new security key",
        then="agent-platform passkey list",
    )
    group = tree.group("recovery", "the recovery code")
    tree.command(
        group,
        "new",
        _recovery_new,
        "a new recovery code (printed once; the old one stops working); an agent with its own identity opens the"
        " settings page in the browser",
        example="agent-platform recovery new",
        words="notfallcode wiederherstellungscode wiederherstellung backup emergency",
    )
    group = tree.group("agents", "the user's connected AI agents")
    tree.command(
        group,
        "list",
        _agents_list,
        "agents with their names",
        example="agent-platform agents list",
        words="agenten verbundene ki assistenten connected ai assistants",
        trim="first",
    )
    sub = tree.command(
        group,
        "revoke",
        _agents_revoke,
        "disconnect an agent, or all",
        example="agent-platform agents revoke --id <agent_id>",
        words="abkoppeln agenten trennen abmelden ki disconnect logout ai",
    )
    sub.add_argument("--id")
    sub.add_argument("--all", action="store_true")
    group = tree.group("platform", "platform invitations and the platform's switches")
    sub = tree.command(
        group,
        "invite",
        _platform_invite,
        "a platform invitation (prints invite_link once)",
        example="agent-platform platform invite [--expires-hours 72]",
        words="neukunden einladen plattformeinladung registrierung signup new user",
        then="agent-platform platform list",
    )
    sub.add_argument("--expires-hours", type=int, default=INVITE_HOURS)
    tree.command(
        group,
        "list",
        _platform_list,
        "platform invitations, newest first",
        example="agent-platform platform list",
        words="plattformeinladungen anzeigen offene einladungen invitations open",
        trim="first",
    )
    sub = tree.command(
        group,
        "revoke",
        _platform_revoke,
        "revoke an open platform invitation",
        example="agent-platform platform revoke --id <invite_id>",
        words="plattformeinladung widerrufen zurückziehen cancel invitation withdraw",
    )
    sub.add_argument("--id", required=True)
    tree.command(
        group,
        "settings",
        _platform_settings,
        "the platform's switches, the organizations using the operator's subscriptions and your platform roles",
        example="agent-platform platform settings",
        words="plattform schalter freigaben einstellungen rollen betreiber abos switches policies roles operator",
        trim="first",
    )
    tree.command(
        tree.commands,
        "security",
        _security,
        "build hash, root key fingerprint and hub key",
        example="agent-platform security",
        words="echtheit prüfen sicherheit prüfsumme fingerabdruck manifest fingerprint integrity verify",
    )
