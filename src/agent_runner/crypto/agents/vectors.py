"""Copied from agent-core src/agent_hub/agents/vectors.py at 952bb156fc9c.

Test vectors of the delegated agent access (11-agent-access.md section 3): `contracts/vectors/agent.json`.

Fingerprint, proof of possession, the handover secret with its bundle key, claim and claim hash, the identity sealed
to the agent and the label sealed to the user, all with fixed keys, nonces and HPKE ephemeral keys. Negative vectors
name an operation of `OPS` (this module), its arguments and the expected error code; the generator refuses a vector
whose code differs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.crypto import agent, keys
from agent_runner.crypto.bundle import bundle_plaintext
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.vectors import ACTORS, Section, fixed, fixed_id, identity

Json = dict[str, Any]
AGENT_ID = fixed_id("agent/one")
OTHER_AGENT = fixed_id("agent/two")


def _b(value: object) -> bytes:
    return b64u_decode(value)


def _open_identity(a: Json) -> object:
    kem = keys.x25519_private(_b(a["agent_kem_sk"]))
    return agent.open_identity(kem, _b(a["secret"]), a["agent_id"], a["user_id"], a["sealed"])


OPS: dict[str, Callable[[Json], object]] = {
    "verify_pop": lambda a: agent.verify_pop(a["pairing_id"], _b(a["kem"]), _b(a["sig"]), a["pop"]),
    "open_identity": _open_identity,
    "open_label": lambda a: agent.open_label(
        keys.x25519_private(_b(a["user_kem_sk"])), a["agent_id"], a["user_id"], a["sealed"]
    ),
    "claim": lambda a: agent.claim(_b(a["secret"]), a["agent_id"], a["user_id"]),
    "check_label": lambda a: agent.check_label(a["label"]),
}


def negative(name: str, op: str, args: Json, error: str) -> Json:
    """A negative vector; `op(args)` must fail with exactly `error`."""
    try:
        OPS[op](args)
    except CryptoError as refused:
        if refused.code == error:
            return {"name": name, "op": op, "args": args, "error": error}
        raise RuntimeError(f"vector {name}: {refused.code} instead of {error}") from None
    raise RuntimeError(f"vector {name}: no error instead of {error}")


def build_agent() -> Json:
    """Every format of 11-agent-access.md section 3."""
    bot, alice, user = identity("agent"), identity("alice"), ACTORS["alice"]
    kem_pub, sig_pub = bot.kem_public(), bot.sig_public()
    pop = agent.sign_pop(bot, AGENT_ID)
    secret = fixed("agent-secret")
    claim = agent.claim(secret, AGENT_ID, user)
    sealed = agent.seal_identity(
        kem_pub,
        secret,
        AGENT_ID,
        user,
        alice,
        nonce=fixed("agent-bundle-nonce", 12),
        ephemeral=keys.x25519_private(fixed("agent-bundle-eph")),
    )
    label = {"host": "Büro-Mac", "name": "Claude Code"}
    sealed_label = agent.seal_label(
        alice.kem_public(), AGENT_ID, user, label, keys.x25519_private(fixed("agent-label-eph"))
    )
    agent_sk = b64u_encode(fixed("agent/kem"))
    alice_sk = b64u_encode(fixed("alice/kem"))
    secret_text = b64u_encode(secret)
    opened = {"agent_kem_sk": agent_sk, "secret": secret_text, "agent_id": AGENT_ID, "user_id": user, "sealed": sealed}
    return {
        "about": "Delegated agent access of 11-agent-access.md section 3 (ADR-056).",
        "agent": {"id": AGENT_ID, "kem_sk": agent_sk, "sig_sk": b64u_encode(fixed("agent/sig")), **bot.public()},
        "user": {"id": user, "kem_sk": alice_sk, **alice.public()},
        "fingerprint": agent.fingerprint(kem_pub, sig_pub),
        "pop": {"text": b64u_encode(agent.pop_text(AGENT_ID, kem_pub, sig_pub)), "signature": pop},
        "secret": {
            "secret": secret_text,
            "bundle_key": b64u_encode(agent.bundle_key(secret, AGENT_ID, user)),
            "claim": b64u_encode(claim),
            "claim_hash": b64u_encode(agent.claim_hash(claim)),
        },
        "identity": {
            "info": b64u_encode(agent.identity_info(AGENT_ID, user)),
            "plaintext": bundle_plaintext(alice).decode(),
            "nonce": b64u_encode(fixed("agent-bundle-nonce", 12)),
            "ephemeral_sk": b64u_encode(fixed("agent-bundle-eph")),
            "sealed": sealed,
        },
        "label": {
            "value": label,
            "info": b64u_encode(agent.label_info(AGENT_ID, user)),
            "ephemeral_sk": b64u_encode(fixed("agent-label-eph")),
            "sealed": sealed_label,
        },
        "invalid": [
            negative(
                "pop_other_pairing",
                "verify_pop",
                {"pairing_id": OTHER_AGENT, "kem": bot.public()["kem"], "sig": bot.public()["sig"], "pop": pop},
                "signature_invalid",
            ),
            negative(
                "identity_without_secret",
                "open_identity",
                {**opened, "secret": b64u_encode(fixed("agent-other-secret"))},
                "decrypt_failed",
            ),
            negative("identity_other_user", "open_identity", {**opened, "user_id": ACTORS["bob"]}, "decrypt_failed"),
            negative("identity_other_agent", "open_identity", {**opened, "agent_id": OTHER_AGENT}, "decrypt_failed"),
            negative(
                "identity_other_key",
                "open_identity",
                {**opened, "agent_kem_sk": alice_sk, "sealed": sealed},
                "decrypt_failed",
            ),
            negative("identity_short", "open_identity", {**opened, "sealed": sealed[:40]}, "hpke_short"),
            negative(
                "label_to_agent_key",
                "open_label",
                {"user_kem_sk": agent_sk, "agent_id": AGENT_ID, "user_id": user, "sealed": sealed_label},
                "decrypt_failed",
            ),
            negative(
                "label_other_agent",
                "open_label",
                {"user_kem_sk": alice_sk, "agent_id": OTHER_AGENT, "user_id": user, "sealed": sealed_label},
                "decrypt_failed",
            ),
            negative(
                "secret_short",
                "claim",
                {"secret": b64u_encode(fixed("x", 16)), "agent_id": AGENT_ID, "user_id": user},
                "header_invalid",
            ),
            negative("label_bidi", "check_label", {"label": {"host": "a\u202eb", "name": "n"}}, "header_invalid"),
            negative("label_zero_width", "check_label", {"label": {"host": "h", "name": "n\u200b"}}, "header_invalid"),
            negative("label_newline", "check_label", {"label": {"host": "h\nx", "name": "n"}}, "header_invalid"),
            negative("label_empty", "check_label", {"label": {"host": "", "name": "n"}}, "header_invalid"),
            negative("label_padded", "check_label", {"label": {"host": " h", "name": "n"}}, "header_invalid"),
            negative("label_long", "check_label", {"label": {"host": "h" * 65, "name": "n"}}, "header_invalid"),
            negative(
                "label_extra_key", "check_label", {"label": {"host": "h", "name": "n", "x": "y"}}, "header_invalid"
            ),
        ],
    }


SECTIONS = [Section("agent", build_agent)]
