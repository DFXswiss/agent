"""`pair --session`: the pairing of the main orchestrator inside a platform session (ADR-082 sections 6.2, 6.3 and
9.7-9.8; 05-runner.md sections 17.1 and 17.4a).

Only in session mode (`agent_platform.relay`); the guest control process runs both steps as the account `agent-orch`
with the input on stdin. Nothing secret is printed: no private key, no handover, no `r`.

1. `pair --session` (`start`): stdin `{session_id, hub_url, hub_key: {id, public_key}, platform_root_key}` from the
   runner (its hub origin, its pinned hub key and the platform root key). The tool draws fresh keys, keeps them as the
   pending pairing (`pending.json` with `kind: "session"`, keys in `pending.key`, `0600` in the `0700` state
   directory, never in a keychain; a pending pairing of any kind is replaced) and answers `{state: "waiting",
   pairing_id, pub, pop, fp}`. The runner registers the pairing at the hub with its device signature; there is no
   link, no browser and no loopback listener.
2. `pair --session --handover` (`handover`): stdin `{pairing_id, ct, user_id, user_pub: {kem, sig}}` - the handover
   sealed by the user's browser, passed on unopened by the runner after it checked the user's signature, and the
   task owner's keys from the runner's verified log. The tool opens `ct` with the pending keys (remote handover
   format of ADR-067: `r` and the platform root key, which must equal the pinned one), claims through the relay and
   stores the state only when the claim opened exactly `user_pub` (and `GET /v1/me` names the same keys). A handover
   for another pairing, one that does not open or a failed claim is `pairing_failed` (with `reason`) and deletes the
   pending pairing; a transient failure (`hub_unreachable`, another refusal) keeps it for a repeat.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Mapping
from typing import Any

from agent_platform.errors import PlatformError
from agent_platform.pairing import Handover, Pairing
from agent_platform.relay import relay_path
from agent_platform.state import Home
from agent_runner.crypto import agent as agent_crypto
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.ids import is_uuid7

KIND = "session"
NAME = "Main orchestrator"
NEXT_WAITING = "The runner registers the pairing; the user approves it on the session's page."
MAX_CT = 4096


def _input(text: str, keys: set[str]) -> dict[str, Any]:
    """The JSON object on stdin with exactly `keys` (`invalid_argument` otherwise)."""
    try:
        value = json.loads(text)
    except (ValueError, RecursionError):
        raise PlatformError("invalid_argument") from None
    if not isinstance(value, dict) or set(value) != keys:
        raise PlatformError("invalid_argument")
    return value


def _key(value: object) -> str:
    try:
        return b64u_encode(b64u_decode(value, 32))
    except (CryptoError, ValueError, TypeError):
        raise PlatformError("invalid_argument") from None


def _pub(value: object) -> dict[str, str]:
    if not isinstance(value, dict) or set(value) != {"kem", "sig"}:
        raise PlatformError("invalid_argument")
    return {"kem": _key(value["kem"]), "sig": _key(value["sig"])}


def _uuid(value: object) -> str:
    if not isinstance(value, str) or not is_uuid7(value):
        raise PlatformError("invalid_argument")
    return value


def _in_session(home: Home) -> Home:
    """The state directory of a session pairing: private keys always in files (the VM has no keychain, and a test
    machine's keychain is no place for them)."""
    if relay_path() is None:
        raise PlatformError("invalid_argument")
    return dataclasses.replace(home, keychain=False)


def start(home: Home, text: str) -> dict[str, Any]:
    """Step 1 (module docstring)."""
    home = _in_session(home)
    given = _input(text, {"session_id", "hub_url", "hub_key", "platform_root_key"})
    session_id = _uuid(given["session_id"])
    hub_key = given["hub_key"]
    if not isinstance(hub_key, dict) or set(hub_key) != {"id", "public_key"} or not isinstance(hub_key["id"], str):
        raise PlatformError("invalid_argument")
    pinned = {"id": hub_key["id"], "public_key": _key(hub_key["public_key"])}
    root = _key(given["platform_root_key"])
    if not isinstance(given["hub_url"], str):
        raise PlatformError("invalid_argument")
    pairing = Pairing(home, given["hub_url"], NAME, session_id[:8], remote=True)
    previous = home.load_pending()
    if previous is not None:
        home.delete_pending(str(previous.get("pairing_id")), previous.get("key_store"))
    where = home.store_keys(pairing.pairing_id, pairing.agent, home.pending_key_file)
    home.save_pending(
        {
            "kind": KIND,
            "hub": pairing.origin,
            "hub_key": pinned,
            "platform_root_key": root,
            "pairing_id": pairing.pairing_id,
            "session_id": session_id,
            "label": {"name": pairing.name, "host": pairing.host},
            "key_store": where,
        }
    )
    public = pairing.agent.public()
    return {
        "state": "waiting",
        "pairing_id": pairing.pairing_id,
        "pub": {"kem": public["kem"], "sig": public["sig"]},
        "pop": agent_crypto.sign_pop(pairing.agent, pairing.pairing_id),
        "fp": pairing.fingerprint(),
        "next": NEXT_WAITING,
    }


def _restore(home: Home, pending: Mapping[str, Any]) -> Pairing:
    """The pending session pairing with its keys (`state_damaged` when unsound)."""
    try:
        agent = home.load_keys(str(pending["pairing_id"]), pending.get("key_store"), home.pending_key_file)
        label = pending["label"]
        pairing = Pairing(home, str(pending["hub"]), str(label["name"]), str(label["host"]), remote=True)
        pairing.pairing_id = _uuid(pending["pairing_id"])
        pairing.hub_key = {"id": str(pending["hub_key"]["id"]), "public_key": _key(pending["hub_key"]["public_key"])}
        pairing.config_root = _key(pending["platform_root_key"])
    except (KeyError, TypeError, PlatformError):
        raise PlatformError("state_damaged") from None
    pairing.agent = agent
    return pairing


def handover(home: Home, text: str) -> dict[str, Any]:
    """Step 2 (module docstring)."""
    home = _in_session(home)
    given = _input(text, {"pairing_id", "ct", "user_id", "user_pub"})
    pairing_id, user_id, user_pub = _uuid(given["pairing_id"]), _uuid(given["user_id"]), _pub(given["user_pub"])
    ct = given["ct"]
    if not isinstance(ct, str) or not 0 < len(ct) <= MAX_CT:
        raise PlatformError("invalid_argument")
    pending = home.load_pending()
    if pending is None or pending.get("kind") != KIND:
        raise PlatformError("no_pending_pairing")
    pairing = _restore(home, pending)
    try:
        if pairing.pairing_id != pairing_id:
            raise PlatformError("pairing_failed", reason="pairing_mismatch")
        try:
            secret, root_key = agent_crypto.open_handover(pairing.agent.kem, pairing_id, user_id, ct)
        except CryptoError:
            raise PlatformError("pairing_failed", reason="handover_invalid") from None
        client = pairing.client()
        try:
            result = pairing.complete_session(client, Handover(secret, root_key, user_id), user_pub)
        except PlatformError as error:
            if error.fields.get("hub_error") == "pairing_unknown":
                raise PlatformError("pairing_failed", reason="claim_lost") from None
            raise
        finally:
            client.close()
    except PlatformError as error:
        if error.code == "pairing_failed":
            home.delete_pending(pairing_id, pending.get("key_store"))
        raise
    home.delete_pending(pairing_id, pending.get("key_store"))
    return {"state": "connected", **result}
