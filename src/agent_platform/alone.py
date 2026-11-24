"""An account of an agent alone (rule E-27, KOR-62; 11-agent-access.md section 5.5): no person, no passkey, no
browser at any step.

- `register --own-account --display-name <name>` (`register_own`; invitation link on stdin or in
  `AGENT_PLATFORM_INVITE`, never on the command line): the tool draws the user's identity, a recovery code with its
  recovery slot, `CK_1` with the self-wrap signed by the identity, its own agent key pair and a handover secret `r` of
  its own, under which it seals the identity to itself (`agent-bundle`, exactly what a paired agent holds) and the
  label to the identity. It proves possession with signatures (method `agent-signature-v1`): the invitation key, the
  identity and the agent key sign the account text, and `POST /v1/agent-accounts` creates the account with this agent
  as its active main orchestrator. The tool stores its state like after a pairing (a previous state is replaced) and
  then does what the web client does after a registration: a platform invitation founds the private organization
  (`genesis{private:true}` with `platform_invite_id`), an org invitation is accepted (`org join`). The result is that
  of `register` plus the organization and the recovery code (`secret_fields`: the code is shown once and is the only
  way to take the account to another computer or back after the agent's key is lost).
- `recover --hub <url>` (`recover`; the recovery code on stdin): fresh agent keys, `POST /v1/recovery/begin`, the
  recovery slot opened with the code, `POST /v1/recovery/finish` with the new agent and the signatures over the
  recover text. The hub makes the new agent the main orchestrator and revokes the previous one (a passkey account is
  refused: `not_own_account`). The tool stores the state and replaces the recovery code (`recovery new`): the result
  carries the new code once (`secret_fields`), the old one no longer works.
- `agents confirm --link <connect link> --code <agent code>` (`confirm`): the main orchestrator of an account
  without a passkey confirms a pairing from another computer (`pair --remote` of the new agent) the way the page does
  it: link and hub must agree (fingerprint, mode, nonce), the agent code must match, then it hands the account's
  identity to the new agent (`bundle_ct`, ADR-082 section 7 unchanged: the successor is again the account's only
  holder and can confirm pairings and make recovery codes without a person), seals label and handover and
  `POST /v1/me/agents` makes the new agent the main orchestrator. An account that has a passkey by now would get the
  new form as a browser builds it (`delegate.delegation`), which the hub refuses from an agent (`not_own_account`). This
  agent is replaced by it (the hub revokes it at once; the tool deletes its state). The result names the return code the
  new agent needs (`pair --wait --return-code`). On an account with a passkey the hub refuses (`not_own_account`): there
  the person's passkey approval decides (concept idea 34).

Secrets never leave as program arguments or in outputs, except the recovery code in its `secret_fields`.
"""

from __future__ import annotations

import os
import secrets as random
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote, urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_platform import delegate
from agent_platform.account import Account
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.hub import anon_request, refused, segment
from agent_platform.orgs import PRIVATE_NAMES, genesis, locale, org_join
from agent_platform.pairing import DISPLAY_NAME_MAX, JS_TRIM, hub_config, parse_invite
from agent_platform.security import recovery_new
from agent_platform.state import Home
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.bundle import RECOVERY_SLOT, open_bundle, seal_bundle
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import is_uuid7, uuid7
from agent_runner.crypto.kdf import kek_from_recovery_code
from agent_runner.crypto.keys import Identity
from agent_runner.crypto.recovery import (
    format_recovery_code,
    generate_recovery_code,
    parse_recovery_code,
    recovery_id,
)
from agent_runner.crypto.wrap import sign_wrap, wrap_key
from agent_runner.sync.http import hub_origin

METHOD = agent_crypto.METHOD_AGENT_SIGNATURE
SECRET_BYTES = 32
CRED = "cred"
OWN_INVITE = "own_invite"
OWN_SCOPE = "own_scope"
NEXT_ORG_PENDING = (
    "The account exists (keep its recovery code), but its organization step failed (see org_failure): run the same "
    "`agent-platform register --own-account` command with the same invitation link again to finish it."
)
NEXT_CONFIRMED = (
    "Give the new agent the return code; it runs `agent-platform pair --wait --return-code <code>`. "
    "This agent is disconnected now."
)


def new_identity() -> Identity:
    """Fresh identity key pairs (X25519 and Ed25519), as the browser draws them at a registration."""
    return Identity(X25519PrivateKey.generate(), Ed25519PrivateKey.generate())


class Sealed:
    """A new agent with its keys and the user's identity sealed to it under a secret it drew itself."""

    def __init__(self, user_id: str, identity: Identity, name: str, host: str) -> None:
        self.agent_id = uuid7()
        self.agent = agent_crypto.new_agent()
        self.secret = random.token_bytes(SECRET_BYTES)
        self.label = agent_crypto.check_label({"name": name, "host": host})
        self.user_id = user_id
        self.bundle_ct = agent_crypto.seal_identity(
            self.agent.kem_public(), self.secret, self.agent_id, user_id, identity
        )
        claim = agent_crypto.claim(self.secret, self.agent_id, user_id)
        self.body = {
            "agent_id": self.agent_id,
            "pub": self.agent.public(),
            "bundle_ct": self.bundle_ct,
            "label_ct": agent_crypto.seal_label(identity.kem_public(), self.agent_id, user_id, self.label),
            "claim_hash": b64u_encode(agent_crypto.claim_hash(claim)),
        }

    def store(
        self, home: Home, origin: str, hub_key: Mapping[str, str], root_key: str, extra: Mapping[str, str] = {}
    ) -> None:
        """The state of this agent (a previous state is deleted first), as after a pairing."""
        try:
            previous = home.load()
            home.delete(str(previous.get("agent_id")), previous.get("key_store"))
        except PlatformError:
            pass
        where = home.store_keys(self.agent_id, self.agent)
        home.save(
            {
                "hub": origin,
                "hub_key": dict(hub_key),
                "platform_root_key": root_key,
                "agent_id": self.agent_id,
                "user_id": self.user_id,
                "label": self.label,
                "bundle_ct": self.bundle_ct,
                "secret": b64u_encode(self.secret),
                "key_store": where,
                **extra,
            }
        )


def _label(name: str, host: str) -> None:
    try:
        agent_crypto.check_label({"name": name, "host": host})
    except CryptoError:
        raise PlatformError("invalid_argument") from None


def _root_key(given: str | None, config_root: object) -> str:
    """The platform root key: `--platform-root-key` when given, else the hub's `/v1/config` (no browser hands one
    over; the residual risk of a hub that names its own root key is that of the web build it serves)."""
    value = given if given is not None else config_root
    try:
        return b64u_encode(b64u_decode(value, 32))
    except CryptoError:
        raise PlatformError("invalid_argument" if given is not None else "hub_refused", reason="root_key") from None


def _account_body(
    invite: invite_format.InviteSecrets, identity: Identity, user_id: str, sealed: Sealed
) -> dict[str, Any]:
    code = generate_recovery_code()
    cred_key = random.token_bytes(SECRET_BYTES)
    wrap = wrap_key(identity.kem_public(), scope=CRED, scope_id=user_id, epoch=1, recipient=user_id, key=cred_key)
    text = agent_crypto.account_text(
        agent_id=sealed.agent_id,
        agent_kem=sealed.agent.kem_public(),
        agent_sig=sealed.agent.sig_public(),
        invite_id=invite.invite_id,
        method=METHOD,
        user_id=user_id,
        user_kem=identity.kem_public(),
        user_sig=identity.sig_public(),
    )
    return {
        "code": code,
        "body": {
            "invite_id": invite.invite_id,
            "user_id": user_id,
            "identity": {"kem_pub": identity.public()["kem"], "sig_pub": identity.public()["sig"]},
            "recovery": {
                "recovery_id": recovery_id(code),
                "bundle_ct": seal_bundle(kek_from_recovery_code(code), user_id, RECOVERY_SLOT, identity),
            },
            "cred_wrap": {
                "epoch": 1,
                "wrap": wrap,
                "sig": sign_wrap(identity.sig, scope=CRED, scope_id=user_id, epoch=1, recipient=user_id, wrap=wrap),
            },
            "agent": sealed.body,
            "proof": {
                "method": METHOD,
                "inv_sig": agent_crypto.sign_text(invite.signing, text),
                "id_sig": agent_crypto.sign_text(identity.sig, text),
                "agent_sig": agent_crypto.sign_text(sealed.agent.sig, text),
            },
        },
    }


def register_own(
    home: Home,
    *,
    invite: str,
    display_name: str,
    name: str,
    host: str,
    root_key: str | None = None,
) -> dict[str, Any]:
    """`register --own-account` (module docstring)."""
    origin, code = parse_invite(invite)
    member = display_name.strip(JS_TRIM)
    if not 1 <= len(member) <= DISPLAY_NAME_MAX:
        raise PlatformError("invalid_argument")
    _label(name, host)
    secrets = invite_format.invite_secrets(code)
    pending = _pending_account(home, origin, secrets.invite_id)
    if pending is not None:
        return {**pending, "display_name": member, **_found_or_join(home, pending["scope"], invite, member, secrets)}
    lookup = anon_request(origin, "POST", "/v1/invites/lookup", {"invite_id": secrets.invite_id})
    if lookup.get("registered") is True:
        raise refused("invite_registered", 409)
    scope = lookup.get("scope")
    if scope not in ("platform", "org"):
        raise PlatformError("hub_refused", reason="response_invalid")
    hub_key, config_root = hub_config(origin)
    root = _root_key(root_key, config_root)
    identity, user_id = new_identity(), uuid7()
    sealed = Sealed(user_id, identity, name, host)
    prepared = _account_body(secrets, identity, user_id, sealed)
    answer = _send_once_more(origin, "/v1/agent-accounts", prepared["body"])
    if answer.get("user_id") != user_id or answer.get("agent_id") != sealed.agent_id:
        raise PlatformError("hub_refused", reason="response_invalid")
    sealed.store(home, origin, hub_key, root, {OWN_INVITE: secrets.invite_id, OWN_SCOPE: str(scope)})
    return {
        "agent_id": sealed.agent_id,
        "user_id": user_id,
        "hub": origin,
        "scope": scope,
        "display_name": member,
        **_found_or_join(home, str(scope), invite, member, secrets),
        "recovery_code": format_recovery_code(prepared["code"]),
        "secret_fields": ["recovery_code"],
    }


def _send_once_more(origin: str, path: str, body: object) -> dict[str, Any]:
    """An anonymous request sent again with the identical body when the first answer got lost (`hub_unreachable`):
    the hub answers an identical repeat with the stored answer."""
    try:
        return anon_request(origin, "POST", path, body)
    except PlatformError as error:
        if error.code != "hub_unreachable":
            raise
    return anon_request(origin, "POST", path, body)


def _pending_account(home: Home, origin: str, invite_id: str) -> dict[str, Any] | None:
    """The account this tool registered through `invite_id` whose organization step is still owed (a run of the same
    command again resumes it), else None."""
    try:
        state = home.load()
    except PlatformError:
        return None
    if state.get(OWN_INVITE) != invite_id or state.get("hub") != origin:
        return None
    return {"agent_id": state["agent_id"], "user_id": state["user_id"], "hub": origin, "scope": state.get(OWN_SCOPE)}


def _found_or_join(
    home: Home, scope: str, invite: str, member: str, secrets: invite_format.InviteSecrets
) -> dict[str, Any]:
    """The private organization (platform invitation) or the joined one (org invitation). A failure keeps the account
    and its result (with the recovery code): `org_failure` names the code, and the same command run again resumes."""
    account = Account.open(home)
    try:
        if scope == "platform":
            org = genesis(account, PRIVATE_NAMES[locale(account)]["org"], member, secrets.invite_id)
            joined: dict[str, Any] = {"org_id": org["org_id"], "default_wg": org["default_wg"]}
        else:
            found = org_join(account, invite, member)
            joined = {"org_id": found["org_id"], "role": found["role"]}
    except PlatformError as error:
        return {"org_id": None, "org_failure": error.code, "next": NEXT_ORG_PENDING}
    finally:
        account.close()
    state = {key: value for key, value in home.load().items() if key not in (OWN_INVITE, OWN_SCOPE)}
    home.save(state)
    return joined


def recover(
    home: Home, *, hub: str, code_text: str, name: str, host: str, root_key: str | None = None
) -> dict[str, Any]:
    """`recover` (module docstring)."""
    try:
        origin = hub_origin(hub).base_url
        code = parse_recovery_code(code_text.strip())
    except (ValueError, CryptoError):
        raise PlatformError("invalid_argument") from None
    _label(name, host)
    hub_key, config_root = hub_config(origin)
    root = _root_key(root_key, config_root)
    try:
        begun = anon_request(origin, "POST", "/v1/recovery/begin", {"recovery_id": recovery_id(code)})
    except PlatformError as error:
        if error.fields.get("hub_error") == "not_found":
            raise PlatformError("invalid_argument", reason="recovery_code_unknown") from None
        raise
    user_id, challenge_text = begun.get("user_id"), begun.get("challenge")
    try:
        if not is_uuid7(user_id):
            raise CryptoError("header_invalid")
        challenge = b64u_decode(challenge_text, agent_crypto.CHALLENGE_BYTES)
        identity = open_bundle(kek_from_recovery_code(code), str(user_id), RECOVERY_SLOT, str(begun.get("bundle_ct")))
    except CryptoError:
        raise PlatformError("hub_refused", reason="recovery_bundle_invalid") from None
    user = str(user_id)
    sealed = Sealed(user, identity, name, host)
    text = agent_crypto.recover_text(
        agent_id=sealed.agent_id,
        agent_kem=sealed.agent.kem_public(),
        agent_sig=sealed.agent.sig_public(),
        challenge=challenge,
        method=METHOD,
        user_id=user,
    )
    body = {
        "user_id": user,
        "challenge": b64u_encode(challenge),
        "proof": agent_crypto.sign_text(identity.sig, text),
        "agent": {**sealed.body, "method": METHOD, "agent_sig": agent_crypto.sign_text(sealed.agent.sig, text)},
    }
    try:
        answer = anon_request(origin, "POST", "/v1/recovery/finish", body)
    except PlatformError as error:
        if error.fields.get("hub_error") == "forbidden":
            raise PlatformError("not_own_account") from None
        raise
    if answer.get("user_id") != user or answer.get("agent_id") != sealed.agent_id:
        raise PlatformError("hub_refused", reason="response_invalid")
    sealed.store(home, origin, hub_key, root)
    account = Account.open(home)
    try:
        fresh = recovery_new(account, {})
    finally:
        account.close()
    return {"agent_id": sealed.agent_id, "user_id": user, "hub": origin, **fresh}


def parse_connect_link(text: str, origin: str) -> dict[str, str]:
    """The five parameters of a remote pairing link `<origin>/connect#p=…&f=…&m=remote&a=…&h=…` of this hub
    (`invalid_argument` otherwise)."""
    parts = urlsplit(text.strip())
    if f"{parts.scheme}://{parts.netloc}" != origin or parts.path != "/connect" or parts.query:
        raise PlatformError("invalid_argument")
    fields: dict[str, str] = {}
    for item in parts.fragment.split("&"):
        key, _, value = item.partition("=")
        if key in fields:
            raise PlatformError("invalid_argument")
        fields[key] = unquote(value)
    if set(fields) != {"p", "f", "m", "a", "h"} or fields["m"] != "remote" or not is_uuid7(fields["p"]):
        raise PlatformError("invalid_argument")
    _label(fields["a"], fields["h"])
    return fields


def confirm(account: Account, *, link: str, code: str) -> dict[str, Any]:
    """`agents confirm` (module docstring)."""
    fields = parse_connect_link(link, account.origin)
    try:
        typed = agent_crypto.parse_code(code, agent_crypto.AGENT_CODE_LENGTH)
    except CryptoError:
        raise PlatformError("invalid_argument") from None
    pairing_id = fields["p"]
    found = account.call("GET", f"/v1/agent-pairings/{segment(pairing_id)}")
    try:
        pub = agent_crypto.public_keys(found.get("pub"))
        kem_pub, sig_pub = b64u_decode(pub["kem"], 32), b64u_decode(pub["sig"], 32)
        nonce = b64u_decode(found.get("nonce"), agent_crypto.NONCE_BYTES)
        gen = found.get("gen")
        if not isinstance(gen, int) or isinstance(gen, bool) or gen < 1:
            raise CryptoError("header_invalid")
        if found.get("pairing_id") != pairing_id or found.get("state") != "open" or found.get("remote") is not True:
            raise CryptoError("header_invalid")
        if agent_crypto.fingerprint(kem_pub, sig_pub) != fields["f"]:
            raise CryptoError("header_invalid")
    except CryptoError:
        raise PlatformError("pairing_failed", reason="pairing_mismatch") from None
    expected = agent_crypto.agent_code(pairing_id, fields["f"], nonce, account.origin)
    if typed != expected:
        raise PlatformError("pairing_failed", reason="agent_code_mismatch")
    root = account.platform_root
    if root is None:
        raise PlatformError("state_damaged")
    secret = random.token_bytes(SECRET_BYTES)
    user = account.user_id
    label = {"name": fields["a"], "host": fields["h"]}
    sealed = {
        "pairing_id": pairing_id,
        "label_ct": agent_crypto.seal_label(account.user_kem_public(), pairing_id, user, label),
        "claim_hash": b64u_encode(agent_crypto.claim_hash(agent_crypto.claim(secret, pairing_id, user))),
        "handover_ct": agent_crypto.seal_handover(kem_pub, pairing_id, user, secret, root),
    }

    alone = not account.me().get("passkeys")

    def send() -> dict[str, Any]:
        if alone:  # the account's identity goes to the successor (ADR-082 section 7)
            identity = delegate.holder(account)
            own = {"bundle_ct": agent_crypto.seal_identity(kem_pub, secret, pairing_id, user, identity)}
        else:
            own = delegate.delegation(account, agent_id=pairing_id, pub=pub, gen=gen, secret=secret)
        return account.call("POST", "/v1/me/agents", {**sealed, **own})

    try:
        answer = delegate.submit(account, send)
    except PlatformError as error:
        if error.fields.get("hub_error") == "forbidden":
            raise PlatformError("not_own_account") from None
        raise
    if answer.get("agent_id") != pairing_id:
        raise PlatformError("hub_refused", reason="response_invalid")
    account.home.delete(account.agent_id, account.state.get("key_store"))
    back = agent_crypto.return_code(secret, pairing_id, user)
    return {
        "agent_id": pairing_id,
        "replaced": account.agent_id,
        "return_code": agent_crypto.format_code(back),
        "next": NEXT_CONFIRMED,
    }


# --- commands ---


def _recover(args: Any, context: Context) -> dict[str, Any]:
    text = context.environ.get("AGENT_PLATFORM_RECOVERY_CODE", "") or context.read_stdin()
    if not text.strip():
        raise PlatformError("invalid_argument")
    return recover(
        context.home, hub=args.hub, code_text=text, name=args.name, host=args.host, root_key=args.platform_root_key
    )


def _confirm(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return confirm(account, link=args.link, code=args.code)


def default_host() -> str:
    return (os.uname().nodename.split(".")[0] or "computer")[:64]


def register(tree: Tree) -> None:
    sub = tree.add(
        tree.commands,
        "recover",
        _recover,
        "take an account without a passkey to this computer with its recovery code (stdin)",
        common=False,
        example='agent-platform recover --hub https://agent.dfx.swiss --name "Claude Code" < recovery-code.txt',
        words="wiederherstellen umziehen wiederherstellungscode recovery code restore move computer",
        then="agent-platform status",
    )
    sub.add_argument("--hub", required=True)
    sub.add_argument("--name", default="Claude Code")
    sub.add_argument("--host", default=default_host())
    sub.add_argument("--platform-root-key", help="platform root key (b64u) instead of the hub's")
    group = tree.group("agents", "the user's connected AI agents")
    sub = tree.command(
        group,
        "confirm",
        _confirm,
        "make the agent of a remote pairing link the main orchestrator (no passkey)",
        example='agent-platform agents confirm --link "<connect_link>" --code XXXXX-XXXXX',
        words="nachfolger bestätigen agent ersetzen confirm successor replace agent",
    )
    sub.add_argument("--link", required=True, help="the link of `pair --remote` of the new agent")
    sub.add_argument("--code", required=True, help="the agent code of the new agent")
