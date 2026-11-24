"""Pairing an agent that runs on another computer (ADR-067, 11-agent-access.md section 5.4).

The agent cannot reach a listener on the user's computer, so the pairing runs in two calls of the tool:

1. `pair --hub <url> --remote` (`start_pair`) or `register --remote` (`start_register`): fresh keys, the pairing at
   the hub with `remote: true` (an old hub refuses the field: `hub_too_old`), and at once the link and the agent code
   the agent gives its user. The link of `pair` is `<origin>/connect#p=…&f=…&m=remote&a=…&h=…` and carries no secret;
   the link of `register` is the invitation link with these parameters and `u=<display name>` and is printed only as
   `invite_link`, listed in `secret_fields` (the invitation code is meant for the user alone). The pending pairing is
   kept in `pending.json` (`0600`) with its private keys in the keychain item `agent-platform` / `<pairing id>` where
   available, else in `pending.key` (`0600`). A second pending pairing is refused (`pairing_pending`) unless
   `--replace-pending` deletes the first. An existing paired state stays until a new pairing completes.
2. `pair --wait [--return-code <code>] [--timeout <s>]` (`wait`): works on the pending pairing (`no_pending_pairing`
   when there is none; a lock on `pending.lock` keeps a second `--wait` out: `pairing_busy`). It polls
   `GET /v1/agent-pairings/{id}` every `POLL_S` for at most `--timeout` seconds (default 120, at most 600) and returns
   `"state":"waiting"` while the user has not confirmed. Once confirmed: without `--return-code` it returns
   `"state":"confirmed"` (the agent asks its user for the code the page shows); with it, it fetches the handover
   (`GET /v1/agent/handover`), opens it with its key and checks the return code **before** it claims or stores
   anything: a hub that forged the handover does not know the code the page derived from the browser's secret. One
   mismatch or a handover that does not open ends the pairing (`pairing_failed`). Then it claims, opens and checks the
   identity like the loopback pairing, stores the state (replacing a previous one) and returns like `pair`.

A malformed `--return-code` is `invalid_argument` and keeps the pairing (no attempt is used); a return code given
while the pairing is still open waits for the confirmation like a plain `--wait` and returns `"state":"waiting"` at
the timeout without using the attempt. Terminal cases delete `pending.json` and its key item: success, expiry
before the confirmation or the claim (`pairing_timeout`: the hub answers `pairing_unknown`) and failure
(`pairing_failed`). A transient failure (`hub_unreachable`, another refusal) keeps the pending pairing for a retry.
Before it claims, the tool marks the pending pairing (`claim_sent`): when a retry then finds the pairing gone
(`pairing_unknown`), the claim may have succeeded at the hub with its answer lost, so the hub may list an active agent
without local state. That is `pairing_failed` with `reason: claim_lost`, whose next step asks the user to disconnect
that agent in the settings (`errors.NEXT_FOR_REASON`), not a timeout.
Outputs never contain the handover, the secret `r`, the claim or a private key; the texts of `next` are fixed here.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Iterator, Mapping
from typing import Any
from urllib.parse import quote

from agent_platform.errors import PlatformError
from agent_platform.hub import AgentClient, anon_request, refused, segment
from agent_platform.pairing import DISPLAY_NAME_MAX, JS_TRIM, Handover, Pairing, parse_invite
from agent_platform.state import KEYCHAIN, Home
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto import invite as invite_format
from agent_runner.crypto.encoding import b64u_encode
from agent_runner.crypto.errors import CryptoError

LOCK_FILE = "pending.lock"
WAIT_DEFAULT_S = 120
WAIT_MAX_S = 600
POLL_S = 2.0
NEXT_WAITING = (
    "Give your user the link and the code; the user opens the link, types the code and confirms with the passkey. "
    "Then run `agent-platform pair --wait`."
)
NEXT_CONFIRMED = (
    "The user confirmed. Ask your user for the code the page shows, then run "
    "`agent-platform pair --wait --return-code <code>`."
)


@contextlib.contextmanager
def _locked(home: Home) -> Iterator[None]:
    """Hold the lock of the pending pairing (`pairing_busy` while another process holds it)."""
    home.ensure_dir()
    descriptor = os.open(home.path / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PlatformError("pairing_busy") from None
        yield
    finally:
        os.close(descriptor)


def _discard(home: Home, pending: Mapping[str, Any], *, keep_keychain: bool = False) -> None:
    home.delete_pending(str(pending.get("pairing_id")), pending.get("key_store"), keep_keychain=keep_keychain)


def _begin(home: Home, pairing: Pairing, replace: bool, extra: Mapping[str, Any]) -> None:
    """Refuse or replace an existing pending pairing, create the remote pairing and keep it pending."""
    previous = home.load_pending()
    if previous is not None:
        if not replace:
            raise PlatformError("pairing_pending")
        _discard(home, previous)
    pairing.register()
    where = home.store_keys(pairing.pairing_id, pairing.agent, home.pending_key_file)
    home.save_pending(
        {
            "hub": pairing.origin,
            "hub_key": pairing.hub_key,
            "platform_root_key": pairing.config_root if isinstance(pairing.config_root, str) else None,
            "pairing_id": pairing.pairing_id,
            "nonce": b64u_encode(pairing.nonce),
            "mode": agent_crypto.MODE_REMOTE,
            "expires_at": pairing.expires_at,
            "label": {"name": pairing.name, "host": pairing.host},
            "key_store": where,
            **extra,
        }
    )


def start_pair(
    home: Home, *, hub: str, name: str, host: str, replace: bool, message: str | None = None
) -> dict[str, Any]:
    """`pair --remote` (module comment, step 1)."""
    pairing = Pairing(home, hub, name, host, remote=True, message=message)
    with _locked(home):
        _begin(home, pairing, replace, {"kind": "pair"})
    return {
        "state": "waiting",
        "hub": pairing.origin,
        "connect_link": f"{pairing.origin}/connect#{pairing.remote_fragment()}",
        "code": pairing.agent_code(),
        "expires_at": pairing.expires_at,
        "next": NEXT_WAITING,
    }


def start_register(
    home: Home, *, invite: str, display_name: str, name: str, host: str, replace: bool, message: str | None = None
) -> dict[str, Any]:
    """`register --remote` (module comment, step 1): the invitation link with the pairing, marked secret."""
    origin, code = parse_invite(invite)
    member = display_name.strip(JS_TRIM)
    if not 1 <= len(member) <= DISPLAY_NAME_MAX:
        raise PlatformError("invalid_argument")
    lookup = anon_request(
        origin, "POST", "/v1/invites/lookup", {"invite_id": invite_format.invite_secrets(code).invite_id}
    )
    if lookup.get("registered") is True:
        raise refused("invite_registered", 409)
    pairing = Pairing(home, origin, name, host, remote=True, message=message)
    scope = lookup.get("scope") if isinstance(lookup.get("scope"), str) else None
    with _locked(home):
        _begin(home, pairing, replace, {"kind": "register", "display_name": member, "scope": scope})
    link = f"{origin}/invite#{invite_format.format_code(code)}&{pairing.remote_fragment()}&u={quote(member, safe='')}"
    return {
        "state": "waiting",
        "hub": origin,
        "invite_link": link,
        "secret_fields": ["invite_link"],
        "code": pairing.agent_code(),
        "expires_at": pairing.expires_at,
        "scope": scope,
        "next": NEXT_WAITING,
    }


def _lookup(pairing: Pairing) -> dict[str, Any]:
    """The pairing at the hub; `pairing_timeout` once it is gone (expired, or claimed or revoked meanwhile)."""
    try:
        answer = anon_request(pairing.origin, "GET", f"/v1/agent-pairings/{segment(pairing.pairing_id)}")
    except PlatformError as error:
        if error.fields.get("hub_error") == "pairing_unknown":
            raise PlatformError("pairing_timeout") from None
        raise
    if answer.get("pairing_id") != pairing.pairing_id or answer.get("state") not in ("open", "confirmed"):
        raise PlatformError("hub_refused", reason="response_invalid")
    return answer


def _poll(pairing: Pairing, timeout: float) -> dict[str, Any]:
    """Poll until the pairing is confirmed or `timeout` passed; the last answer."""
    deadline = time.monotonic() + timeout
    while True:
        answer = _lookup(pairing)
        if answer["state"] == "confirmed" or time.monotonic() + POLL_S > deadline:
            return answer
        time.sleep(POLL_S)


def _handover(pairing: Pairing, client: AgentClient, typed: str) -> Handover:
    """The handover of the hub, opened and checked against the return code the user typed (`pairing_failed`)."""
    try:
        found = client.call("GET", "/v1/agent/handover")
    except PlatformError as error:
        if error.fields.get("hub_error") == "pairing_unknown":
            raise PlatformError("pairing_timeout") from None
        raise
    user_id = found.get("user_id")
    if found.get("agent_id") != pairing.pairing_id or not isinstance(user_id, str):
        raise PlatformError("pairing_failed", reason="handover_invalid")
    try:
        secret, root_key = agent_crypto.open_handover(
            pairing.agent.kem, pairing.pairing_id, user_id, str(found.get("handover_ct"))
        )
    except CryptoError:
        raise PlatformError("pairing_failed", reason="handover_invalid") from None
    if not agent_crypto.return_code_matches(secret, pairing.pairing_id, user_id, typed):
        raise PlatformError("pairing_failed", reason="return_code_mismatch")
    return Handover(secret, root_key, user_id)


def _complete(home: Home, pending: Mapping[str, Any], pairing: Pairing, typed: str) -> dict[str, Any]:
    client = pairing.client()
    try:
        handover = _handover(pairing, client, typed)
        home.save_pending({**pending, "claim_sent": True})
        try:
            result = pairing.complete(client, handover)
        except PlatformError as error:
            if error.fields.get("hub_error") == "pairing_unknown":
                raise PlatformError("pairing_timeout") from None
            raise
    finally:
        client.close()
    stored = home.load()
    _discard(home, pending, keep_keychain=stored.get("key_store") == KEYCHAIN)
    answer: dict[str, Any] = {"state": "connected", **result, "hub": pairing.origin, "kind": pending.get("kind")}
    if pending.get("kind") == "register":
        answer["display_name"] = pending.get("display_name")
        answer["scope"] = pending.get("scope")
    return answer


def wait(home: Home, *, return_code: str | None, timeout: int) -> dict[str, Any]:
    """`pair --wait` (module comment, step 2)."""
    if not 0 <= timeout <= WAIT_MAX_S:
        raise PlatformError("invalid_argument")
    if return_code is not None:
        try:
            agent_crypto.parse_code(return_code, agent_crypto.RETURN_CODE_LENGTH)
        except CryptoError:
            raise PlatformError("invalid_argument") from None
    if home.load_pending() is None:
        raise PlatformError("no_pending_pairing")
    with _locked(home):
        pending = home.load_pending()
        if pending is None:  # finished by another `--wait` meanwhile
            raise PlatformError("no_pending_pairing")
        agent = home.load_keys(str(pending.get("pairing_id")), pending.get("key_store"), home.pending_key_file)
        pairing = Pairing.restore(home, pending, agent)
        try:
            answer = _poll(pairing, timeout)
            if answer["state"] == "open":
                return {
                    "state": "waiting",
                    "hub": pairing.origin,
                    "expires_at": answer.get("expires_at"),
                    "next": NEXT_WAITING,
                }
            if return_code is None:
                return {
                    "state": "confirmed",
                    "hub": pairing.origin,
                    "expires_at": answer.get("expires_at"),
                    "next": NEXT_CONFIRMED,
                }
            return _complete(home, pending, pairing, return_code)
        except PlatformError as error:
            if error.code in ("pairing_timeout", "pairing_failed"):
                _discard(home, pending)
            if error.code == "pairing_timeout" and pending.get("claim_sent") is True:
                raise PlatformError("pairing_failed", reason="claim_lost") from None
            raise
