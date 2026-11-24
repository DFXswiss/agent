"""Copied from src/agent_hub/crypto/vectors.py.

Deterministic test vectors of 04-crypto.md section 10 (`contracts/vectors/`), after Spike D `make_vectors.py`.

Every vector file is one *section*: a name (the file is `<name>.json`) and a pure `build()` that returns the JSON
document. The sections of this package are listed in `SECTIONS`; further sections are discovered, so that later
packages add a file instead of editing this one (ADR-027 section 4):

- `agent_runner/crypto/<area>/vectors.py` with a module attribute `SECTIONS` (e.g.
  `agent_runner.crypto.log.vectors`), and
- `contracts/tools/vectors_*.py` next to `make_vectors.py`, also with `SECTIONS`.

All inputs are fixed (`fixed(label)`), including AES-GCM nonces, HKDF salts and HPKE ephemeral keys, so the output is
byte-identical on every run. Negative vectors (`"invalid"` lists) name an operation of `OPS`, its arguments and the
expected error code; the generator runs each one and refuses to write a vector whose code differs. The formats are
described in `contracts/vectors/README.md`.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import runpy
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import agent_runner.crypto
from agent_runner.crypto import (
    aead,
    bundle,
    credential,
    dedupe,
    encoding,
    enroll,
    event,
    hpke,
    ids,
    invite,
    kdf,
    keys,
    recovery,
    request,
    wrap,
)
from agent_runner.crypto import identity as identity_format
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError

Json = dict[str, Any]
PACKAGE_ROOT = Path(agent_runner.crypto.__file__).parent
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Section:
    """One vector file: `contracts/vectors/<name>.json` = `build()`."""

    name: str
    build: Callable[[], Json]


def fixed(label: str, size: int = 32) -> bytes:
    """Deterministic bytes for a label (at most 32)."""
    return hashlib.sha256(b"agent.dfx.swiss vectors v1 " + label.encode("ascii")).digest()[:size]


def fixed_id(label: str, unix_ms: int = 1_795_000_000_000) -> str:
    """A deterministic UUIDv7."""
    return ids.uuid7(unix_ms, fixed("id/" + label, 10))


def _json(value: object) -> Json:
    return cast(Json, value)


def _b(value: object) -> bytes:
    return b64u_decode(value)


# --- Operations that negative vectors name (the same names exist in every implementation). ---

OPS: dict[str, Callable[[Json], object]] = {
    "b64u_decode": lambda a: encoding.b64u_decode(a["text"], a.get("length")),
    "canonical": lambda a: encoding.canonical(a["value"]),
    "parse_time": lambda a: encoding.parse_time(a["text"]),
    "kek_from_prf": lambda a: kdf.kek_from_prf(_b(a["prf_output"]), _b(a["credential_id"])),
    "kek_from_recovery_code": lambda a: kdf.kek_from_recovery_code(_b(a["code"])),
    "recovery_id": lambda a: recovery.recovery_id(_b(a["code"])),
    "invite_secrets": lambda a: invite.invite_secrets(_b(a["code"])),
    "ed25519_verify": lambda a: keys.ed25519_verify(_b(a["public_key"]), _b(a["signature"]), _b(a["message"])),
    "p256_verify_raw": lambda a: keys.p256_verify_raw(_b(a["public_key"]), _b(a["signature"]), _b(a["message"])),
    "open_bundle": lambda a: bundle.open_bundle(_b(a["kek"]), a["user_id"], a["slot"], a["sealed"]),
    "hpke_open": lambda a: hpke.open_(keys.x25519_private(_b(a["recipient_kem_sk"])), _b(a["info"]), _b(a["sealed"])),
    "unwrap_key": lambda a: wrap.unwrap_key(
        keys.x25519_private(_b(a["recipient_kem_sk"])),
        scope=a["scope"],
        scope_id=a["scope_id"],
        epoch=a["epoch"],
        recipient=a["recipient"],
        wrap=a["wrap"],
    ),
    "verify_wrap": lambda a: wrap.verify_wrap(
        _b(a["wrapper_sig_pk"]),
        scope=a["scope"],
        scope_id=a["scope_id"],
        epoch=a["epoch"],
        recipient=a["recipient"],
        wrap=a["wrap"],
        signature=a["sig"],
    ),
    "verify_event": lambda a: event.verify_event(a["event"], _b(a["author_sig_pk"])),
    "open_event": lambda a: event.open_event(_b(a["scope_key"]), a["event"], _b(a["author_sig_pk"])),
    "verify_credential": lambda a: credential.verify_credential(a["record"], _b(a["author_sig_pk"])),
    "open_credential": lambda a: credential.open_credential(_b(a["cred_key"]), a["record"], _b(a["author_sig_pk"])),
    "verify_request": lambda a: request.verify_request(a["envelope"], _b(a["user_sig_pk"])),
    "open_request": lambda a: request.open_request(
        keys.x25519_private(_b(a["device_kem_sk"])), a["envelope"], _b(a["user_sig_pk"])
    ),
    "verify_response": lambda a: request.verify_response(a["envelope"], _b(a["device_sig_pk"])),
    "open_response": lambda a: request.open_response(
        keys.x25519_private(_b(a["user_kem_sk"])), a["envelope"], _b(a["device_sig_pk"])
    ),
    "parse_code": lambda a: invite.parse_code(a["text"]),
    "parse_recovery_code": lambda a: recovery.parse_recovery_code(a["text"]),
    "open_package": lambda a: invite.open_package(invite.invite_secrets(_b(a["code"])), a["sealed"]),
    "verify_proof": lambda a: recovery.verify_proof(
        _b(a["identity_sig_pk"]), a["user_id"], _b(a["challenge"]), a["proof"]
    ),
    "verify_enrollment": lambda a: enroll.verify_enrollment(a["body"], _b(a["pk_inv"])),
    "verify_register": lambda a: invite.verify_register(
        _b(a["pk_inv"]), a["user_id"], _b(a["challenge"]), a["inv_sig"]
    ),
    "dedupe_fields": lambda a: dedupe.dedupe_fields(a["type"], a["params"]),
}


def run_op(op: str, args: Json) -> object:
    """Run one named operation (used by the generator and by the tests to replay negative vectors)."""
    return OPS[op](args)


def negative(name: str, op: str, args: Json, error: str) -> Json:
    """A negative vector; the generator checks that `op(args)` fails with exactly `error`."""
    try:
        run_op(op, args)
    except CryptoError as refused:
        if refused.code == error:
            return {"name": name, "op": op, "args": args, "error": error}
        raise RuntimeError(f"vector {name}: {refused.code} instead of {error}") from None
    raise RuntimeError(f"vector {name}: no error instead of {error}")


# --- Fixed actors. ---

ORG = fixed_id("org")
WG = fixed_id("wg-default")
TASK = fixed_id("task")
SESSION = fixed_id("session")
TS = "2026-11-21T12:00:00.000Z"


def identity(label: str) -> keys.Identity:
    """The fixed identity of an actor."""
    return keys.Identity.from_raw(fixed(label + "/kem"), fixed(label + "/sig"))


ACTORS = {"alice": fixed_id("user/alice"), "bob": fixed_id("user/bob"), "device": fixed_id("device/one")}


def _identities() -> Json:
    out = {}
    for name, actor_id in ACTORS.items():
        ident = identity(name)
        out[name] = {
            "id": actor_id,
            "kem_sk": b64u_encode(fixed(name + "/kem")),
            "sig_sk": b64u_encode(fixed(name + "/sig")),
            **ident.public(),
        }
    return out


def _pk(name: str) -> str:
    return identity(name).public()["sig"]


# --- Sections. ---


def build_encoding() -> Json:
    """b64u, canonical JSON, body JSON, times and UUIDv7."""
    raw_cases = [b"", b"f", b"fo", b"foo", b"\xfb\xff", bytes(range(16))]
    canonical_cases: list[object] = [
        {"b": 1, "a": [True, False, None], "B": "x", "~": "~"},
        {"nested": {"z": {"y": [], "x": {}}, "a": ""}, "n": -(2**53 - 1), "m": 2**53 - 1},
        {"text": 'quote " backslash \\ and \\u0041 literally'},
        [],
        "plain",
    ]
    body_cases: list[object] = [
        {"text": "Grüsse \u2013 Alice 👋", "a": 1},
        {"ctl": "\x01\n\t\x7f", "sep": "\u2028\u2029", "slash": "/"},
        {"\U0001f600": "astral key", "\uffff": "last BMP key", "\u00e9": "latin key", "z": "ascii key"},
    ]
    return {
        "about": "Encodings of 04-crypto.md section 1. Byte strings as hex in this file only.",
        "b64u": [{"hex": raw.hex(), "b64u": b64u_encode(raw)} for raw in raw_cases],
        "canonical": [{"value": value, "canonical": encoding.canonical(value).decode()} for value in canonical_cases],
        "body_json": [{"value": value, "body_json": encoding.body_json(value).decode()} for value in body_cases],
        "time": [
            {"unix_ms": ms, "text": encoding.format_time(EPOCH + timedelta(milliseconds=ms))}
            for ms in (0, 1_795_003_200_123, 1_795_003_200_999)
        ],
        "uuid7": [
            {
                "unix_ms": 1_795_000_000_000,
                "random_hex": fixed("uuid", 10).hex(),
                "uuid": ids.uuid7(1_795_000_000_000, fixed("uuid", 10)),
            },
            {"unix_ms": 0, "random_hex": "ff" * 10, "uuid": ids.uuid7(0, b"\xff" * 10)},
        ],
        "invalid": [
            negative("b64u_padding", "b64u_decode", {"text": "Zg=="}, "header_invalid"),
            negative("b64u_trailing_bits", "b64u_decode", {"text": "Zh"}, "header_invalid"),
            negative("b64u_length_mod_4_is_1", "b64u_decode", {"text": "Zm9vY"}, "header_invalid"),
            negative("b64u_standard_alphabet", "b64u_decode", {"text": "-_+/"}, "header_invalid"),
            negative("b64u_whitespace", "b64u_decode", {"text": "Zm9v "}, "header_invalid"),
            negative("b64u_wrong_length", "b64u_decode", {"text": "Zm9v", "length": 4}, "header_invalid"),
            negative("canonical_non_ascii_value", "canonical", {"value": {"name": "Zoë"}}, "non_ascii"),
            negative("canonical_non_ascii_key", "canonical", {"value": {"é": 1}}, "non_ascii"),
            negative("canonical_control_char", "canonical", {"value": {"a": "line\nbreak"}}, "non_ascii"),
            negative("canonical_delete_char", "canonical", {"value": ["\x7f"]}, "non_ascii"),
            negative("canonical_float", "canonical", {"value": {"a": 1.5}}, "header_invalid"),
            negative("canonical_unsafe_integer", "canonical", {"value": {"a": 2**53}}, "header_invalid"),
            negative("canonical_non_ascii_before_float", "canonical", {"value": {"a": 1.5, "b": "é"}}, "non_ascii"),
            negative("time_without_millis", "parse_time", {"text": "2026-11-21T12:00:00Z"}, "header_invalid"),
            negative("time_offset", "parse_time", {"text": "2026-11-21T12:00:00.000+00:00"}, "header_invalid"),
            negative("time_impossible_date", "parse_time", {"text": "2026-02-30T12:00:00.000Z"}, "header_invalid"),
            negative(
                "time_non_ascii_digits", "parse_time", {"text": "\uff12026-11-21T12:00:00.000Z"}, "header_invalid"
            ),
        ],
    }


def build_kdf() -> Json:
    """Every derivation of section 3."""
    prf, cred_id = fixed("prf"), fixed("credential-id", 16)
    rec, inv = fixed("recovery-code", 16), fixed("invite-code", 16)
    scope_key, salt = fixed("wg-key"), fixed("salt")
    seed = fixed("device/sig")
    return {
        "about": "Derivations and labels of 04-crypto.md section 3. Byte strings are b64u.",
        "prefix": encoding.P.decode(),
        "prf_input": b64u_encode(kdf.prf_input()),
        "kek_from_prf": {
            "prf_output": b64u_encode(prf),
            "credential_id": b64u_encode(cred_id),
            "kek": b64u_encode(kdf.kek_from_prf(prf, cred_id)),
        },
        "recovery": {
            "code": b64u_encode(rec),
            "kek": b64u_encode(kdf.kek_from_recovery_code(rec)),
            "recovery_id": recovery.recovery_id(rec),
        },
        "invite": {
            "code": b64u_encode(inv),
            "invite_id": invite.invite_id(inv),
            "package_key": b64u_encode(kdf.package_key(inv)),
            "sk_inv_seed": b64u_encode(kdf.invite_sign_seed(inv)),
            "pk_inv": invite.invite_secrets(inv).pk_inv,
        },
        "event_key": {
            "scope_key": b64u_encode(scope_key),
            "salt": b64u_encode(salt),
            "key": b64u_encode(kdf.event_key(scope_key, salt)),
        },
        "dedupe_key": {"workgroup_key": b64u_encode(scope_key), "key": b64u_encode(kdf.dedupe_key(scope_key))},
        "credential_key": {
            "credential_scope_key": b64u_encode(fixed("ck")),
            "salt": b64u_encode(salt),
            "key": b64u_encode(kdf.credential_key(fixed("ck"), salt)),
        },
        "storage_key": {"device_sig_seed": b64u_encode(seed), "key": b64u_encode(kdf.storage_key(seed))},
        "invalid": [
            negative(
                "prf_31_bytes",
                "kek_from_prf",
                {"prf_output": b64u_encode(prf[:31]), "credential_id": b64u_encode(cred_id)},
                "prf_length",
            ),
            negative(
                "prf_64_bytes",
                "kek_from_prf",
                {"prf_output": b64u_encode(prf * 2), "credential_id": b64u_encode(cred_id)},
                "prf_length",
            ),
            negative(
                "recovery_code_15_bytes",
                "kek_from_recovery_code",
                {"code": b64u_encode(rec[:15])},
                "recovery_code_length",
            ),
            negative(
                "recovery_id_17_bytes", "recovery_id", {"code": b64u_encode(rec + b"\x00")}, "recovery_code_length"
            ),
            negative(
                "invite_code_17_bytes", "invite_secrets", {"code": b64u_encode(inv + b"\x00")}, "invite_code_length"
            ),
        ],
    }


SMALL_ORDER_IDENTITY = bytes([1]) + bytes(31)
SMALL_ORDER_8 = bytes.fromhex("26e8958fc2b227b045c3f489f2ef98f0d5dfac05d3c63339b13802886d53fc05")


def build_keys() -> Json:
    """Identities, Ed25519 and P-256 raw formats."""
    message = b"agent.dfx.swiss key vector"
    alice = identity("alice")
    session = keys.p256_private(fixed("session-p256"))
    p256_sig = keys.p256_sign_raw(session, message, deterministic=True)
    ed_sig = keys.ed25519_sign(alice.sig, message)
    noncanonical = ed_sig[:32] + (int.from_bytes(ed_sig[32:], "little") + keys.ED25519_ORDER).to_bytes(32, "little")
    return {
        "about": "Raw key formats: X25519/Ed25519 32 bytes, P-256 65-byte point and r||s signatures (RFC 6979 here).",
        "identities": _identities(),
        "ed25519": {"public_key": _pk("alice"), "message": b64u_encode(message), "signature": b64u_encode(ed_sig)},
        "p256": {
            "private_scalar": b64u_encode(fixed("session-p256")),
            "public_key": b64u_encode(keys.p256_public_raw(session)),
            "message": b64u_encode(message),
            "signature": b64u_encode(p256_sig),
        },
        "invalid": [
            negative(
                "ed25519_other_message",
                "ed25519_verify",
                {"public_key": _pk("alice"), "signature": b64u_encode(ed_sig), "message": b64u_encode(message + b"!")},
                "signature_invalid",
            ),
            negative(
                "ed25519_short_signature",
                "ed25519_verify",
                {"public_key": _pk("alice"), "signature": b64u_encode(ed_sig[:63]), "message": b64u_encode(message)},
                "signature_invalid",
            ),
            # boundary cases (ATS-03, AUD-01 B-11): S + L is the same signature in a non-canonical form; a
            # small-order public key with R = the identity and S = 0 verifies for any message without the check
            negative(
                "ed25519_noncanonical_s",
                "ed25519_verify",
                {
                    "public_key": _pk("alice"),
                    "signature": b64u_encode(noncanonical),
                    "message": b64u_encode(message),
                },
                "signature_invalid",
            ),
            negative(
                "ed25519_small_order_key",
                "ed25519_verify",
                {
                    "public_key": b64u_encode(SMALL_ORDER_IDENTITY),
                    "signature": b64u_encode(SMALL_ORDER_IDENTITY + bytes(32)),
                    "message": b64u_encode(message),
                },
                "signature_invalid",
            ),
            negative(
                "ed25519_small_order_key_order8",
                "ed25519_verify",
                {
                    "public_key": b64u_encode(SMALL_ORDER_8),
                    "signature": b64u_encode(SMALL_ORDER_IDENTITY + bytes(32)),
                    "message": b64u_encode(message),
                },
                "signature_invalid",
            ),
            negative(
                "p256_other_message",
                "p256_verify_raw",
                {
                    "public_key": b64u_encode(keys.p256_public_raw(session)),
                    "signature": b64u_encode(p256_sig),
                    "message": b64u_encode(b"x"),
                },
                "signature_invalid",
            ),
            negative(
                "p256_zero_r",
                "p256_verify_raw",
                {
                    "public_key": b64u_encode(keys.p256_public_raw(session)),
                    "signature": b64u_encode(bytes(32) + p256_sig[32:]),
                    "message": b64u_encode(message),
                },
                "signature_invalid",
            ),
            negative(
                "p256_compressed_point",
                "p256_verify_raw",
                {
                    "public_key": b64u_encode(b"\x02" + keys.p256_public_raw(session)[1:33]),
                    "signature": b64u_encode(p256_sig),
                    "message": b64u_encode(message),
                },
                "signature_invalid",
            ),
        ],
    }


def build_bundle() -> Json:
    """Identity bundle in a passkey slot and in the recovery slot (section 4.1)."""
    alice, user = identity("alice"), ACTORS["alice"]
    prf, cred_id, rec = fixed("prf"), fixed("credential-id", 16), fixed("recovery-code", 16)
    kek, kek_rec = kdf.kek_from_prf(prf, cred_id), kdf.kek_from_recovery_code(rec)
    slot = bundle.passkey_slot(cred_id)
    sealed = bundle.seal_bundle(kek, user, slot, alice, fixed("bundle-nonce", 12))
    sealed_rec = bundle.seal_bundle(kek_rec, user, "recovery", alice, fixed("recovery-nonce", 12))
    bad_plain = encoding.canonical({"kem": alice.public()["kem"], "sig": _pk("alice"), "v": 2})
    bad_version = b64u_encode(aead.seal_blob(kek, bad_plain, bundle.bundle_aad(user, slot), fixed("bad-nonce", 12)))
    return {
        "about": "Identity bundle of 04-crypto.md section 4.1.",
        "user_id": user,
        "plaintext": bundle.bundle_plaintext(alice).decode(),
        "passkey": {
            "prf_output": b64u_encode(prf),
            "credential_id": b64u_encode(cred_id),
            "slot": slot,
            "kek": b64u_encode(kek),
            "aad": b64u_encode(bundle.bundle_aad(user, slot)),
            "nonce": b64u_encode(fixed("bundle-nonce", 12)),
            "sealed": sealed,
        },
        "recovery": {
            "code": b64u_encode(rec),
            "slot": "recovery",
            "kek": b64u_encode(kek_rec),
            "nonce": b64u_encode(fixed("recovery-nonce", 12)),
            "sealed": sealed_rec,
        },
        "invalid": [
            negative(
                "other_slot",
                "open_bundle",
                {"kek": b64u_encode(kek), "user_id": user, "slot": "recovery", "sealed": sealed},
                "decrypt_failed",
            ),
            negative(
                "other_user",
                "open_bundle",
                {"kek": b64u_encode(kek), "user_id": ACTORS["bob"], "slot": slot, "sealed": sealed},
                "decrypt_failed",
            ),
            negative(
                "other_kek",
                "open_bundle",
                {"kek": b64u_encode(kek_rec), "user_id": user, "slot": slot, "sealed": sealed},
                "decrypt_failed",
            ),
            negative(
                "truncated",
                "open_bundle",
                {"kek": b64u_encode(kek), "user_id": user, "slot": slot, "sealed": sealed[:32]},
                "decrypt_failed",
            ),
            negative(
                "bad_slot_name",
                "open_bundle",
                {"kek": b64u_encode(kek), "user_id": user, "slot": "passkey:", "sealed": sealed},
                "header_invalid",
            ),
            negative(
                "non_ascii_user",
                "open_bundle",
                {"kek": b64u_encode(kek), "user_id": "usér", "slot": slot, "sealed": sealed},
                "non_ascii",
            ),
            negative(
                "plaintext_version_2",
                "open_bundle",
                {"kek": b64u_encode(kek), "user_id": user, "slot": slot, "sealed": bad_version},
                "header_invalid",
            ),
        ],
    }


def build_wrap() -> Json:
    """Three HPKE wraps of one workgroup key (two users, one device) with fixed ephemeral keys (section 4.2)."""
    scope_key, wrapper = fixed("wg-key"), identity("alice")
    wraps = []
    for name in ("alice", "bob", "device"):
        eph = fixed("ephemeral/" + name)
        recipient = ACTORS[name]
        sealed = wrap.wrap_key(
            identity(name).kem_public(),
            scope="wg",
            scope_id=WG,
            epoch=3,
            recipient=recipient,
            key=scope_key,
            ephemeral=keys.x25519_private(eph),
        )
        wraps.append(
            {
                "recipient": recipient,
                "recipient_kem_pk": identity(name).public()["kem"],
                "recipient_kem_sk": b64u_encode(fixed(name + "/kem")),
                "ephemeral_sk": b64u_encode(eph),
                "scope": "wg",
                "scope_id": WG,
                "epoch": 3,
                "info": b64u_encode(wrap.wrap_info("wg", WG, 3, recipient)),
                "wrap": sealed,
                **{
                    k: b64u_encode(v)
                    for k, v in hpke.setup_values(
                        keys.x25519_private(eph), identity(name).kem_public(), wrap.wrap_info("wg", WG, 3, recipient)
                    ).items()
                },
                "wrapped_by": ACTORS["alice"],
                "wrapper_sig_pk": _pk("alice"),
                "sig": wrap.sign_wrap(wrapper.sig, scope="wg", scope_id=WG, epoch=3, recipient=recipient, wrap=sealed),
            }
        )
    bob = wraps[1]
    short = b64u_encode(_b(bob["wrap"])[:47])
    base = {k: bob[k] for k in ("recipient_kem_sk", "scope", "scope_id", "epoch", "recipient", "wrap")}
    sig_args = {k: bob[k] for k in ("wrapper_sig_pk", "scope", "scope_id", "epoch", "recipient", "wrap", "sig")}
    return {
        "about": "HPKE RFC 9180 base mode wraps of 04-crypto.md section 4.2 (KEM 0x0020, KDF 0x0001, AEAD 0x0002).",
        "scope_key": b64u_encode(scope_key),
        "wraps": wraps,
        "invalid": [
            negative("hpke_short", "unwrap_key", {**base, "wrap": short}, "hpke_short"),
            negative(
                "hpke_short_raw",
                "hpke_open",
                {"recipient_kem_sk": bob["recipient_kem_sk"], "info": bob["info"], "sealed": short},
                "hpke_short",
            ),
            negative("other_recipient_id", "unwrap_key", {**base, "recipient": ACTORS["alice"]}, "decrypt_failed"),
            negative("other_epoch", "unwrap_key", {**base, "epoch": 4}, "decrypt_failed"),
            negative("other_scope", "unwrap_key", {**base, "scope": "org"}, "decrypt_failed"),
            negative(
                "other_recipient_key",
                "unwrap_key",
                {**base, "recipient_kem_sk": b64u_encode(fixed("alice/kem"))},
                "decrypt_failed",
            ),
            negative("unknown_scope", "unwrap_key", {**base, "scope": "team"}, "header_invalid"),
            negative("epoch_zero", "unwrap_key", {**base, "epoch": 0}, "header_invalid"),
            negative(
                "small_order_enc",
                "unwrap_key",
                {**base, "wrap": b64u_encode(bytes(32) + _b(bob["wrap"])[32:])},
                "decrypt_failed",
            ),
            negative("signature_other_epoch", "verify_wrap", {**sig_args, "epoch": 4}, "signature_invalid"),
            negative(
                "signature_other_wrapper",
                "verify_wrap",
                {**sig_args, "wrapper_sig_pk": _pk("bob")},
                "signature_invalid",
            ),
        ],
    }


def _event_header(label: str, scope: str, kind: str, task: str = "", session: str = "") -> dict[str, object]:
    return event.new_header(
        id=fixed_id("event/" + label),
        org=ORG,
        scope=scope,
        scope_id=WG if scope == "wg" else ORG,
        epoch=3,
        author=ACTORS["alice"],
        kind=kind,
        ts=TS,
        lh=7,
        wg=WG if scope == "wg" else "",
        task=task,
        session=session,
    )


def _salt_nonce(label: str) -> tuple[bytes, bytes]:
    return fixed("salt/" + label), fixed("nonce/" + label, 12)


_DROP = object()


def _replace_header(sealed: Json, **changes: object) -> Json:
    header = {**sealed["header"], **changes}
    return {**sealed, "header": {k: v for k, v in header.items() if v is not _DROP}}


def build_event() -> Json:
    """One event per scope with a non-ASCII body (section 4.3) and every refusal of E1, E2, E4, E8."""
    alice = identity("alice")
    wg_key, org_key = fixed("wg-key"), fixed("org-key")
    wg_body = encoding.body_json({"text": "Bitte fasse PR #12 zusammen. Grüsse \u2013 Alice", "attachments": []})
    org_body = encoding.body_json({"name": "Zürich Ops 🏔"})
    wg_event: Json = _json(
        event.seal_event(
            wg_key,
            _event_header("wg", "wg", "prompt", task=TASK),
            wg_body,
            alice.sig,
            fixed_salt_nonce=_salt_nonce("wg"),
        )
    )
    org_event: Json = _json(
        event.seal_event(
            org_key,
            _event_header("org", "org", "org.profile"),
            org_body,
            alice.sig,
            fixed_salt_nonce=_salt_nonce("org"),
        )
    )
    terminal_header = _event_header("terminal", "wg", "session.terminal", task=TASK, session=SESSION)
    oversized: Json = _json(
        event.seal_event(
            wg_key,
            {**terminal_header, "kind": "session.output"},
            bytes(64 * 1024),
            alice.sig,
            fixed_salt_nonce=_salt_nonce("terminal"),
        )
    )
    oversized_terminal = {**oversized, "header": {**oversized["header"], "kind": "session.terminal"}}
    pk = _pk("alice")

    def verify(name: str, sealed: Json, error: str, author: str = pk) -> Json:
        return negative(name, "verify_event", {"event": sealed, "author_sig_pk": author}, error)

    return {
        "about": "Events of 04-crypto.md section 4.3 (header = AAD, signature over header and raw ct).",
        "events": [
            {
                "scope_key": b64u_encode(wg_key),
                "author_sig_pk": pk,
                "body_json": wg_body.decode(),
                "aad": encoding.canonical(wg_event["header"]).decode(),
                "k_event": b64u_encode(kdf.event_key(wg_key, _salt_nonce("wg")[0])),
                "event": wg_event,
            },
            {
                "scope_key": b64u_encode(org_key),
                "author_sig_pk": pk,
                "body_json": org_body.decode(),
                "aad": encoding.canonical(org_event["header"]).decode(),
                "k_event": b64u_encode(kdf.event_key(org_key, _salt_nonce("org")[0])),
                "event": org_event,
            },
        ],
        "limits": {"ct_max": event.MAX_CT_BYTES, "terminal_ct_max": event.MAX_TERMINAL_CT_BYTES},
        "invalid": [
            verify("missing_key", _replace_header(wg_event, lh=_DROP), "header_invalid"),
            verify("extra_key_seq", _replace_header(wg_event, seq=1), "header_invalid"),
            verify("extra_key_device", _replace_header(wg_event, device=""), "header_invalid"),
            verify("version_2", _replace_header(wg_event, v=2), "header_invalid"),
            verify("version_true", _replace_header(wg_event, v=True), "header_invalid"),
            verify("alg_unknown", _replace_header(wg_event, alg="A128GCM-HKDF"), "header_invalid"),
            verify("non_ascii_kind", _replace_header(wg_event, kind="prömpt"), "non_ascii"),
            verify("non_ascii_before_float", _replace_header(wg_event, epoch=3.5, kind="prömpt"), "non_ascii"),
            verify("org_scope_wg_for_other_kind", _replace_header(org_event, wg=WG), "header_invalid"),
            verify(
                "uppercase_id", _replace_header(wg_event, id=str(wg_event["header"]["id"]).upper()), "header_invalid"
            ),
            verify(
                "uuid_v4_author",
                _replace_header(wg_event, author="0b9e3d2c-1f4a-4c8e-9a6b-2d5f7e8c1a3b"),
                "header_invalid",
            ),
            verify("scope_unknown", _replace_header(wg_event, scope="task"), "header_invalid"),
            verify("scope_id_not_wg", _replace_header(wg_event, scope_id=ORG), "header_invalid"),
            verify("org_scope_id_not_org", _replace_header(org_event, scope_id=WG), "header_invalid"),
            verify("epoch_zero", _replace_header(wg_event, epoch=0), "header_invalid"),
            verify("lh_negative", _replace_header(wg_event, lh=-1), "header_invalid"),
            verify("ts_seconds", _replace_header(wg_event, ts="2026-11-21T12:00:00Z"), "header_invalid"),
            verify("salt_16_bytes", _replace_header(wg_event, salt=b64u_encode(fixed("s", 16))), "header_invalid"),
            verify(
                "nonce_padded",
                _replace_header(wg_event, nonce=str(wg_event["header"]["nonce"]) + "="),
                "header_invalid",
            ),
            verify("float_epoch", _replace_header(wg_event, epoch=3.5), "header_invalid"),
            verify("extra_object_key", {**wg_event, "seq": 1}, "header_invalid"),
            verify("ct_not_b64u", {**wg_event, "ct": str(wg_event["ct"]) + "="}, "header_invalid"),
            verify("terminal_over_64_kib", oversized_terminal, "size_exceeded"),
            verify("header_changed_after_signing", _replace_header(wg_event, epoch=4), "signature_invalid"),
            verify("other_author_key", wg_event, "signature_invalid", _pk("bob")),
            verify("signature_not_b64u", {**wg_event, "sig": "*"}, "signature_invalid"),
            verify(
                "signature_over_b64u_text",
                {
                    **wg_event,
                    "sig": b64u_encode(
                        alice.sig.sign(
                            encoding.P
                            + kdf.LABEL_EVENT_SIG
                            + encoding.canonical(wg_event["header"])
                            + str(wg_event["ct"]).encode()
                        )
                    ),
                },
                "signature_invalid",
            ),
            negative(
                "other_scope_key",
                "open_event",
                {"scope_key": b64u_encode(org_key), "event": wg_event, "author_sig_pk": pk},
                "decrypt_failed",
            ),
        ],
    }


def build_credential() -> Json:
    """A `github` credential record (section 4.4)."""
    alice, ck = identity("alice"), fixed("ck")
    header = credential.new_header(
        id=fixed_id("credential/github"),
        owner=ACTORS["alice"],
        kind="github",
        version=1,
        epoch=1,
        author=ACTORS["alice"],
        ts=TS,
    )
    body = encoding.body_json(
        {"token": "test-token-not-real", "login": "octo-cat", "user_id": 42, "scopes": ["repo"], "created_at": TS}
    )
    record: Json = _json(
        credential.seal_credential(ck, header, body, alice.sig, fixed_salt_nonce=_salt_nonce("credential"))
    )
    pk = _pk("alice")
    return {
        "about": "Credential record of 04-crypto.md section 4.4 (k_cred from CK and header.salt).",
        "cred_key": b64u_encode(ck),
        "author_sig_pk": pk,
        "body_json": body.decode(),
        "record": record,
        "k_cred": b64u_encode(kdf.credential_key(ck, _salt_nonce("credential")[0])),
        "invalid": [
            negative(
                "unknown_kind",
                "verify_credential",
                {"record": _replace_header(record, kind="gitlab"), "author_sig_pk": pk},
                "header_invalid",
            ),
            negative(
                "version_zero",
                "verify_credential",
                {"record": _replace_header(record, version=0), "author_sig_pk": pk},
                "header_invalid",
            ),
            negative(
                "event_header",
                "verify_credential",
                {"record": _replace_header(record, lh=1), "author_sig_pk": pk},
                "header_invalid",
            ),
            negative(
                "version_bumped",
                "verify_credential",
                {"record": _replace_header(record, version=2), "author_sig_pk": pk},
                "signature_invalid",
            ),
            negative(
                "device_key",
                "verify_credential",
                {"record": record, "author_sig_pk": _pk("device")},
                "signature_invalid",
            ),
            negative(
                "other_ck",
                "open_credential",
                {"cred_key": b64u_encode(fixed("ck-2")), "record": record, "author_sig_pk": pk},
                "decrypt_failed",
            ),
        ],
    }


def build_request() -> Json:
    """A device request, its follow-up and two responses (section 4.5)."""
    alice, device = identity("alice"), identity("device")
    first = fixed_id("request/login")

    def req_header(label: str, follows: str) -> Json:
        return {
            "v": 1,
            "id": fixed_id("request/" + label),
            "user": ACTORS["alice"],
            "device": ACTORS["device"],
            "kind": "claude.login",
            "follows": follows,
            "ts": TS,
            "enc_alg": request.ENC_ALG,
        }

    def resp_header(label: str, seq: int, final: bool) -> Json:
        return {
            "v": 1,
            "id": fixed_id("response/" + label),
            "user": ACTORS["alice"],
            "device": ACTORS["device"],
            "kind": "claude.login",
            "in_reply_to": first,
            "seq": seq,
            "final": final,
            "ts": TS,
            "enc_alg": request.ENC_ALG,
        }

    requests: list[Json] = []
    for label, follows, body in (("login", "", {"mode": "manual"}), ("code", first, {"code": "abc-123"})):
        plain = encoding.body_json(body)
        eph = fixed("request-ephemeral/" + label)
        envelope = _json(
            request.seal_request(
                req_header(label, follows), device.kem_public(), plain, alice.sig, keys.x25519_private(eph)
            )
        )
        requests.append({"ephemeral_sk": b64u_encode(eph), "body_json": plain.decode(), "envelope": envelope})
    responses: list[Json] = []
    for label, seq, final, body in (
        ("authorize", 1, False, {"step": "authorize", "url": "https://example.com/authorize"}),
        ("done", 2, True, {"step": "done"}),
    ):
        plain = encoding.body_json(body)
        eph = fixed("response-ephemeral/" + label)
        envelope = _json(
            request.seal_response(
                resp_header(label, seq, final), alice.kem_public(), plain, device.sig, keys.x25519_private(eph)
            )
        )
        responses.append({"ephemeral_sk": b64u_encode(eph), "body_json": plain.decode(), "envelope": envelope})
    login: Json = requests[0]["envelope"]
    authorize: Json = responses[0]["envelope"]
    short_header = req_header("short", "")
    short_ct = b"\x00" * 40
    short = {
        "header": short_header,
        "ct": b64u_encode(short_ct),
        "sig": b64u_encode(alice.sig.sign(request.signature_text(short_header, short_ct))),
    }
    user_pk, device_pk = _pk("alice"), _pk("device")
    return {
        "about": "Device request/response envelopes of 04-crypto.md section 4.5 (HPKE info binds the header).",
        "user": {"kem_sk": b64u_encode(fixed("alice/kem")), "kem_pk": alice.public()["kem"], "sig_pk": user_pk},
        "device": {"kem_sk": b64u_encode(fixed("device/kem")), "kem_pk": device.public()["kem"], "sig_pk": device_pk},
        "requests": requests,
        "responses": responses,
        "invalid": [
            negative(
                "request_with_response_keys",
                "verify_request",
                {"envelope": authorize, "user_sig_pk": device_pk},
                "header_invalid",
            ),
            negative(
                "response_with_request_keys",
                "verify_response",
                {"envelope": login, "device_sig_pk": user_pk},
                "header_invalid",
            ),
            negative(
                "follows_missing",
                "verify_request",
                {"envelope": _replace_header(login, follows=_DROP), "user_sig_pk": user_pk},
                "header_invalid",
            ),
            negative(
                "final_as_integer",
                "verify_response",
                {"envelope": _replace_header(authorize, final=0), "device_sig_pk": device_pk},
                "header_invalid",
            ),
            negative(
                "seq_zero",
                "verify_response",
                {"envelope": _replace_header(authorize, seq=0), "device_sig_pk": device_pk},
                "header_invalid",
            ),
            negative(
                "enc_alg_unknown",
                "verify_request",
                {"envelope": _replace_header(login, enc_alg="HPKE-P256"), "user_sig_pk": user_pk},
                "header_invalid",
            ),
            negative(
                "kind_changed",
                "verify_request",
                {"envelope": _replace_header(login, kind="github.connect"), "user_sig_pk": user_pk},
                "signature_invalid",
            ),
            negative(
                "signed_by_device", "verify_request", {"envelope": login, "user_sig_pk": device_pk}, "signature_invalid"
            ),
            negative(
                "other_device_key",
                "open_request",
                {"device_kem_sk": b64u_encode(fixed("bob/kem")), "envelope": login, "user_sig_pk": user_pk},
                "decrypt_failed",
            ),
            negative(
                "short_ciphertext",
                "open_request",
                {"device_kem_sk": b64u_encode(fixed("device/kem")), "envelope": short, "user_sig_pk": user_pk},
                "hpke_short",
            ),
            negative(
                "response_other_user_key",
                "open_response",
                {"user_kem_sk": b64u_encode(fixed("bob/kem")), "envelope": authorize, "device_sig_pk": device_pk},
                "decrypt_failed",
            ),
        ],
    }


def build_invite() -> Json:
    """Invitation code, derivations and package (sections 3 and 4.6)."""
    code = fixed("invite-code", 16)
    secrets = invite.invite_secrets(code)
    package: Json = {
        "org": ORG,
        "org_name": "Gemüsekooperative Zürich",
        "wg_names": {WG: "Allgemein ✓"},
        "founder": _pk("alice"),
        "head": {"seq": 4, "hash": b64u_encode(fixed("log-head"))},
        "keys": [
            {"scope": "org", "scope_id": ORG, "epoch": 1, "key": b64u_encode(fixed("org-key"))},
            {"scope": "wg", "scope_id": WG, "epoch": 3, "key": b64u_encode(fixed("wg-key"))},
        ],
        "hub_key": {"id": "hub-1", "public_key": b64u_encode(fixed("hub-public"))},
        "v": 1,
    }
    nonce = fixed("package-nonce", 12)
    sealed = invite.seal_package(secrets, package, nonce)
    bad = {**package, "v": 2}
    bad_sealed = b64u_encode(
        aead.seal_blob(secrets.package_key, encoding.body_json(bad), invite.package_aad(secrets.invite_id), nonce)
    )
    text = invite.format_code(code)
    challenge, user = fixed("register-challenge"), ACTORS["alice"]
    register = {
        "pk_inv": secrets.pk_inv,
        "user_id": user,
        "challenge": b64u_encode(challenge),
        "inv_sig": invite.sign_register(secrets, user, challenge),
    }
    return {
        "about": "Invitation code text, derivations, sealed package and registration proof of 04-crypto.md sections 3"
        " and 4.6 (proof: ADR-043).",
        "code": b64u_encode(code),
        "text": text,
        "parse": [
            {"text": text, "code": b64u_encode(code)},
            {"text": text.lower().replace("-", " "), "code": b64u_encode(code)},
            {"text": text.replace("-", ""), "code": b64u_encode(code)},
        ],
        "invite_id": secrets.invite_id,
        "package_key": b64u_encode(secrets.package_key),
        "pk_inv": secrets.pk_inv,
        "package": package,
        "package_body_json": encoding.body_json(package).decode(),
        "package_aad": b64u_encode(invite.package_aad(secrets.invite_id)),
        "nonce": b64u_encode(nonce),
        "sealed": sealed,
        "register": {**register, "text": b64u_encode(invite.register_text(user, challenge))},
        "invalid": [
            negative("code_25_chars", "parse_code", {"text": text[:-1]}, "invite_code_length"),
            negative("code_27_chars", "parse_code", {"text": text + "A"}, "invite_code_length"),
            negative("code_digit_one", "parse_code", {"text": "1" + text[1:]}, "invite_code_invalid"),
            negative("code_padding", "parse_code", {"text": text + "======"}, "invite_code_invalid"),
            negative(
                "code_trailing_bits",
                "parse_code",
                {"text": text[:-1] + ("B" if text[-1] != "B" else "D")},
                "invite_code_invalid",
            ),
            negative("code_other_whitespace", "parse_code", {"text": text.replace("-", "\t")}, "invite_code_invalid"),
            negative("code_long_s_folds_to_s", "parse_code", {"text": "\u017f" + text[1:]}, "invite_code_invalid"),
            negative(
                "package_other_code",
                "open_package",
                {"code": b64u_encode(fixed("other-code", 16)), "sealed": sealed},
                "invite_code_invalid",
            ),
            negative(
                "package_truncated",
                "open_package",
                {"code": b64u_encode(code), "sealed": sealed[:20]},
                "invite_code_invalid",
            ),
            negative(
                "package_version_2", "open_package", {"code": b64u_encode(code), "sealed": bad_sealed}, "header_invalid"
            ),
            negative(
                "register_other_challenge",
                "verify_register",
                {**register, "challenge": b64u_encode(fixed("other-challenge"))},
                "signature_invalid",
            ),
            negative(
                "register_other_user", "verify_register", {**register, "user_id": ACTORS["bob"]}, "signature_invalid"
            ),
            negative(
                "register_other_code",
                "verify_register",
                {**register, "pk_inv": invite.invite_secrets(fixed("other-code", 16)).pk_inv},
                "signature_invalid",
            ),
            negative(
                "register_short_challenge",
                "verify_register",
                {**register, "challenge": b64u_encode(challenge[:16])},
                "header_invalid",
            ),
            negative("register_non_ascii_user", "verify_register", {**register, "user_id": "\u00e4"}, "non_ascii"),
            negative("register_empty_user", "verify_register", {**register, "user_id": ""}, "header_invalid"),
        ],
    }


def build_recovery() -> Json:
    """Recovery code text, `recovery_id`, `KEK_rec` and the recovery proof (sections 3 and 5.5)."""
    code, alice = fixed("recovery-code", 16), identity("alice")
    challenge, user = fixed("recovery-challenge"), ACTORS["alice"]
    proof = recovery.sign_proof(alice.sig, user, challenge)
    args = {"identity_sig_pk": _pk("alice"), "user_id": user, "challenge": b64u_encode(challenge), "proof": proof}
    text = recovery.format_recovery_code(code)
    return {
        "about": "Recovery code of 04-crypto.md sections 3 and 5.5 (same text form as invitation codes).",
        "code": b64u_encode(code),
        "text": text,
        "recovery_id": recovery.recovery_id(code),
        "kek": b64u_encode(kdf.kek_from_recovery_code(code)),
        "proof": {**args, "text": b64u_encode(recovery.proof_text(user, challenge))},
        "invalid": [
            negative("text_25_chars", "parse_recovery_code", {"text": text[:-1]}, "recovery_code_length"),
            negative("text_bad_char", "parse_recovery_code", {"text": "0" + text[1:]}, "invite_code_invalid"),
            negative(
                "proof_other_challenge",
                "verify_proof",
                {**args, "challenge": b64u_encode(fixed("other-challenge"))},
                "signature_invalid",
            ),
            negative("proof_other_user", "verify_proof", {**args, "user_id": ACTORS["bob"]}, "signature_invalid"),
            negative(
                "proof_short_challenge",
                "verify_proof",
                {**args, "challenge": b64u_encode(challenge[:16])},
                "header_invalid",
            ),
        ],
    }


def build_enroll() -> Json:
    """Platform device enrollment text and both signatures (section 3)."""
    device = identity("device")
    secrets = invite.invite_secrets(fixed("device-invite-code", 16))
    capacity = {  # device capacity of 02-data-model.md section 2.1
        "vm_memory_budget_mib": 131072,
        "vm_memory_used_mib": 0,
        "vm_slots_total": 8,
        "vm_slots_free": 8,
        "pool_ready": 2,
    }
    body = {
        "device_id": ACTORS["device"],
        "kind": "platform",
        "invite_id": secrets.invite_id,
        "pub": device.public(),
        "agent_version": "0.1.0",
        "os": "macos",
        "arch": "arm64",
        "capacity": capacity,
    }
    signed_body = enroll.sign_enrollment(body, device.sig, secrets.signing)
    pk_inv = secrets.pk_inv
    without_enroll = {k: v for k, v in signed_body.items() if k != "enroll_sig"}
    return {
        "about": "Enrollment text of 04-crypto.md section 3: P || 'enroll\\0' || canonical(body without signatures).",
        "invite_code": b64u_encode(fixed("device-invite-code", 16)),
        "pk_inv": pk_inv,
        "text": enroll.enrollment_text(body).decode(),
        "body": signed_body,
        "invalid": [
            negative(
                "other_invite_key",
                "verify_enrollment",
                {"body": signed_body, "pk_inv": _pk("alice")},
                "signature_invalid",
            ),
            negative(
                "capacity_changed",
                "verify_enrollment",
                {
                    "body": {**signed_body, "capacity": {**capacity, "vm_slots_free": 7}},
                    "pk_inv": pk_inv,
                },
                "signature_invalid",
            ),
            negative(
                "enroll_sig_missing",
                "verify_enrollment",
                {"body": without_enroll, "pk_inv": pk_inv},
                "signature_invalid",
            ),
            negative(
                "pub_missing",
                "verify_enrollment",
                {"body": {k: v for k, v in signed_body.items() if k != "pub"}, "pk_inv": pk_inv},
                "header_invalid",
            ),
        ],
    }


def build_dedupe() -> Json:
    """Dedupe tags for every operation type with duplicate protection (section 4.10, 03-api.md section 9)."""
    wk = fixed("wg-key")
    dk = kdf.dedupe_key(wk)
    sha = "A" * 40
    params: dict[str, Json] = {
        "github.checkout": {"repo": "Example-Org/Example-Repo", "ref": sha, "session": SESSION},
        "github.create_branch": {"repo": "example-org/example-repo", "branch": "refs/heads/feature/ä-umlaut"},
        "github.commit": {
            "repo": "example-org/example-repo",
            "branch": "feature/x",
            "parent_sha": sha,
            "tree_sha": "b" * 40,
        },
        "github.push": {"repo": "example-org/example-repo", "branch": "feature/x", "commit_sha": "c" * 40},
        "github.create_pull_request": {"repo": "example-org/example-repo", "head": "feature/x", "base": "develop"},
        "github.comment": {"repo": "example-org/example-repo", "number": 12, "body": "Looks good \u2013 danke!"},
        "github.create_issue": {"repo": "Example-Org/Example-Repo", "title": "Anmeldung schlägt fehl"},
        "subagent.request": {"session": SESSION, "purpose": "review", "prompt": "Prüfe den Diff."},
    }
    tags = []
    for op_type in sorted(dedupe.DEDUPE_TYPES):
        fields = dedupe.dedupe_fields(op_type, params[op_type])
        tags.append(
            {
                "type": op_type,
                "params": params[op_type],
                "fields": fields,
                "tag": dedupe.dedupe_tag(dk, op_type, fields),
            }
        )
    idem = dedupe.idempotency_fields("client-key-1", TASK)
    tags.append(
        {
            "type": "github.push",
            "idempotency_key": "client-key-1",
            "task": TASK,
            "fields": idem,
            "tag": dedupe.dedupe_tag(dk, "github.push", idem),
        }
    )
    return {
        "about": "Dedupe tags of 04-crypto.md section 4.10 with the normalised field sets of agent_hub.crypto.dedupe.",
        "workgroup_key": b64u_encode(wk),
        "dedupe_key": b64u_encode(dk),
        "tags": tags,
        "invalid": [
            negative(
                "type_without_dedupe",
                "dedupe_fields",
                {"type": "task.set_title", "params": {"title": "x"}},
                "header_invalid",
            ),
            negative(
                "repo_missing",
                "dedupe_fields",
                {"type": "github.push", "params": {"branch": "x", "commit_sha": sha}},
                "header_invalid",
            ),
            negative(
                "number_zero",
                "dedupe_fields",
                {"type": "github.comment", "params": {"repo": "a/b", "number": 0, "body": "x"}},
                "header_invalid",
            ),
        ],
    }


def build_identity() -> Json:
    """Identity proof, the user ID derived from a platform invitation and the account code (section 3, ADR-060)."""
    code = fixed("identity/code", 16)
    secrets = invite.invite_secrets(code)
    invite_ts = "2026-11-21T12:34:56.789Z"
    user = identity_format.invited_user_id(secrets.invite_id, invite_ts)
    owner = identity("identity/owner").public()
    other = identity("identity/other").public()
    codes = []
    typed_codes = (
        "abcd-efgh-2345",
        " ABCD EFGH 2345 ",
        "0BCD-1FGH-8345",
        "ABCD-EFGH-234",
        "ABCD-EFGH-23456",
        "AB9D-EFGH-2345",
    )
    for typed in typed_codes:
        try:
            codes.append({"text": typed, "code": identity_format.parse_account_code(typed)})
        except CryptoError as refused:
            codes.append({"text": typed, "error": refused.code})
    account = identity_format.account_code(user, owner)
    return {
        "about": "Identity proof id_sig, derived user ID and account code of 04-crypto.md section 3 (ADR-060).",
        "invite": {
            "code": b64u_encode(code),
            "invite_id": secrets.invite_id,
            "pk_inv": secrets.pk_inv,
            "ts": invite_ts,
        },
        "user_id": user,
        "pub": owner,
        "other_pub": other,
        "text": b64u_encode(identity_format.identity_text(user, owner)),
        "id_sig": identity_format.sign_identity(secrets.signing, user, owner),
        "id_sig_other_keys_valid": identity_format.identity_valid(
            secrets.pk_inv, user, other, identity_format.sign_identity(secrets.signing, user, owner)
        ),
        "account_code": account,
        "account_code_text": identity_format.format_account_code(account),
        "account_code_other_keys": identity_format.account_code(user, other),
        "parse": codes,
        "invalid": [],
    }


SECTIONS: tuple[Section, ...] = (
    Section("encoding", build_encoding),
    Section("kdf", build_kdf),
    Section("keys", build_keys),
    Section("bundle", build_bundle),
    Section("wrap", build_wrap),
    Section("event", build_event),
    Section("credential", build_credential),
    Section("request", build_request),
    Section("invite", build_invite),
    Section("recovery", build_recovery),
    Section("enroll", build_enroll),
    Section("dedupe", build_dedupe),
    Section("identity", build_identity),
)


# --- Registry and command line. ---


def _sections_of(namespace: Mapping[str, object], origin: str) -> list[Section]:
    found = namespace.get("SECTIONS")
    if not isinstance(found, list | tuple) or not all(isinstance(item, Section) for item in found):
        raise TypeError(f"{origin}: SECTIONS must be a sequence of Section")
    return list(found)


def discover(tools_dir: Path | None = None, package_root: Path = PACKAGE_ROOT) -> list[Section]:
    """`SECTIONS` of this module, of every `<package_root>/<area>/vectors.py` and of `<tools_dir>/vectors_*.py`.

    Order: this module, then areas and tool files sorted by name. Duplicate section names are an error.
    """
    sections = list(SECTIONS)
    for area in sorted(path.parent.name for path in package_root.glob("*/vectors.py")):
        if area != "crypto":
            module = importlib.import_module(f"agent_runner.crypto.{area}.vectors")
            sections += _sections_of(vars(module), module.__name__)
    if tools_dir is not None:
        for path in sorted(tools_dir.glob("vectors_*.py")):
            sections += _sections_of(runpy.run_path(str(path)), path.name)
    names = [section.name for section in sections]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise ValueError(f"duplicate vector sections: {', '.join(duplicates)}")
    return sections


def render(section: Section) -> str:
    """The file content: sorted keys, two-space indent, UTF-8 (non-ASCII kept), final newline."""
    return json.dumps(section.build(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def stale(out_dir: Path, sections: Sequence[Section]) -> list[str]:
    """File names whose content differs from the generator (missing files included)."""
    result = []
    for section in sections:
        path = out_dir / f"{section.name}.json"
        if not path.is_file() or path.read_text(encoding="utf-8") != render(section):
            result.append(path.name)
    return result


def write(out_dir: Path, sections: Sequence[Section]) -> list[str]:
    """Write every section; returns the names of the files that changed."""
    changed = stale(out_dir, sections)
    out_dir.mkdir(parents=True, exist_ok=True)
    for section in sections:
        if f"{section.name}.json" in changed:
            (out_dir / f"{section.name}.json").write_text(render(section), encoding="utf-8")
    return changed


def main(argv: Sequence[str] | None, contracts_root: Path) -> int:
    """`make_vectors.py [--check]`: write (or compare) `<contracts_root>/vectors/*.json`.

    Sections are discovered as described in the module docstring, tool files in `<contracts_root>/tools/`.
    `--check` writes nothing and exits 1 if a file is missing or differs.
    """
    parser = argparse.ArgumentParser(description="contracts/vectors generator (04-crypto.md section 10)")
    parser.add_argument("--check", action="store_true", help="compare without writing")
    args = parser.parse_args(argv)
    out_dir = contracts_root / "vectors"
    sections = discover(contracts_root / "tools")
    if args.check:
        outdated = stale(out_dir, sections)
        for name in outdated:
            print(f"stale: {name}", file=sys.stderr)
        return 1 if outdated else 0
    for name in write(out_dir, sections):
        print(f"wrote {name}")
    return 0
