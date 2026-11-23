"""Copied from agent-core tests/platform/hub/crypto/test_agent.py at 0346281bb510.

CON-02 for the delegated agent access (11-agent-access.md section 3, ADR-056): `contracts/vectors/agent.json` is the
byte-for-byte output of the generator, every positive vector is reproduced and opened with independent code
(hashlib, pyca HKDF, HPKE and AES-GCM), and every negative vector fails with exactly its code."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives import hpke as pyca_hpke
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from agent_runner.crypto import agent, hpke, keys, vectors
from agent_runner.crypto.agents import vectors as agent_vectors
from agent_runner.crypto.bundle import bundle_plaintext
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError

VECTORS = Path(__file__).resolve().parents[4] / "contracts" / "vectors"
RAW = (VECTORS / "agent.json").read_bytes()
DATA: dict[str, Any] = json.loads(RAW)
PREFIX = b"agent.dfx.swiss/v1/"
SUITE = pyca_hpke.Suite(pyca_hpke.KEM.X25519, pyca_hpke.KDF.HKDF_SHA256, pyca_hpke.AEAD.AES_256_GCM)


def b(text: str) -> bytes:
    return b64u_decode(text)


def ids(agent_id: str, user_id: str) -> bytes:
    return json.dumps({"agent": agent_id, "user": user_id}, sort_keys=True, separators=(",", ":")).encode()


def hkdf(secret: bytes, info: bytes) -> bytes:
    return HKDF(hashes.SHA256(), 32, None, info).derive(secret)


def test_the_file_is_the_generator_output_byte_for_byte() -> None:
    (section,) = agent_vectors.SECTIONS
    assert section.name == "agent"
    assert section in vectors.discover()
    assert vectors.render(section).encode() == RAW


def test_fingerprint_and_proof_of_possession() -> None:
    bot = DATA["agent"]
    kem, sig = b(bot["kem"]), b(bot["sig"])
    expected = hashlib.sha256(PREFIX + b"agent-key\x00" + kem + sig).digest()[:16]
    assert b(DATA["fingerprint"]) == expected
    assert len(DATA["fingerprint"]) == 22
    assert agent.fingerprint(kem, sig) == DATA["fingerprint"]
    text = (
        PREFIX
        + b"agent-pair\x00"
        + json.dumps(
            {"kem": bot["kem"], "pairing_id": bot["id"], "sig": bot["sig"]}, sort_keys=True, separators=(",", ":")
        ).encode()
    )
    assert b(DATA["pop"]["text"]) == text
    Ed25519PublicKey.from_public_bytes(sig).verify(b(DATA["pop"]["signature"]), text)
    seed = Ed25519PrivateKey.from_private_bytes(b(bot["sig_sk"]))
    assert seed.public_key().public_bytes_raw() == sig
    identity = keys.Identity.from_raw(b(bot["kem_sk"]), b(bot["sig_sk"]))
    assert agent.sign_pop(identity, bot["id"]) == DATA["pop"]["signature"]
    agent.verify_pop(bot["id"], kem, sig, DATA["pop"]["signature"])


def test_secret_bundle_key_claim_and_claim_hash() -> None:
    bot, user, secret = DATA["agent"], DATA["user"], DATA["secret"]
    r = b(secret["secret"])
    both = ids(bot["id"], user["id"])
    assert b(secret["bundle_key"]) == hkdf(r, PREFIX + b"agent-bundle-key\x00" + both)
    assert b(secret["claim"]) == hkdf(r, PREFIX + b"agent-claim\x00" + both)
    assert b(secret["claim_hash"]) == hashlib.sha256(PREFIX + b"agent-claim-hash\x00" + b(secret["claim"])).digest()
    assert agent.bundle_key(r, bot["id"], user["id"]) == b(secret["bundle_key"])
    assert agent.claim(r, bot["id"], user["id"]) == b(secret["claim"])
    assert agent.claim_hash(b(secret["claim"])) == b(secret["claim_hash"])


def test_the_sealed_identity_opens_with_independent_code_and_is_reproduced() -> None:
    bot, user, case = DATA["agent"], DATA["user"], DATA["identity"]
    info = PREFIX + b"agent-bundle\x00" + ids(bot["id"], user["id"])
    assert b(case["info"]) == info
    inner = SUITE.decrypt(b(case["sealed"]), X25519PrivateKey.from_private_bytes(b(bot["kem_sk"])), info=info)
    nonce = inner[:12]
    assert nonce == b(case["nonce"])
    plaintext = AESGCM(b(DATA["secret"]["bundle_key"])).decrypt(nonce, inner[12:], info)
    assert plaintext.decode() == case["plaintext"]
    assert json.loads(plaintext)["kem"] == user["kem_sk"]
    opened = agent.open_identity(
        keys.x25519_private(b(bot["kem_sk"])), b(DATA["secret"]["secret"]), bot["id"], user["id"], case["sealed"]
    )
    assert bundle_plaintext(opened).decode() == case["plaintext"]
    assert opened.public() == {"kem": user["kem"], "sig": user["sig"]}
    resealed = agent.seal_identity(
        b(bot["kem"]),
        b(DATA["secret"]["secret"]),
        bot["id"],
        user["id"],
        opened,
        nonce=nonce,
        ephemeral=keys.x25519_private(b(case["ephemeral_sk"])),
    )
    assert resealed == case["sealed"]


def test_the_sealed_label_opens_with_independent_code_and_is_reproduced() -> None:
    bot, user, case = DATA["agent"], DATA["user"], DATA["label"]
    info = PREFIX + b"agent-label\x00" + ids(bot["id"], user["id"])
    assert b(case["info"]) == info
    plaintext = SUITE.decrypt(b(case["sealed"]), X25519PrivateKey.from_private_bytes(b(user["kem_sk"])), info=info)
    assert json.loads(plaintext) == case["value"]
    assert plaintext == json.dumps(case["value"], sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    user_kem = keys.x25519_private(b(user["kem_sk"]))
    assert agent.open_label(user_kem, bot["id"], user["id"], case["sealed"]) == case["value"]
    ephemeral = keys.x25519_private(b(case["ephemeral_sk"]))
    assert agent.seal_label(b(user["kem"]), bot["id"], user["id"], case["value"], ephemeral) == case["sealed"]


@pytest.mark.parametrize("case", DATA["invalid"], ids=lambda case: f"{case['op']}:{case['name']}")
def test_negative_vector(case: dict[str, Any]) -> None:
    with pytest.raises(CryptoError) as refused:
        agent_vectors.OPS[case["op"]](case["args"])
    assert refused.value.code == case["error"]


def test_the_negative_vectors_cover_every_operation_once_named() -> None:
    assert {case["op"] for case in DATA["invalid"]} == set(agent_vectors.OPS)
    names = [case["name"] for case in DATA["invalid"]]
    assert len(names) == len(set(names))


def test_negative_refuses_a_wrong_expectation() -> None:
    label = {"label": {"host": "", "name": "n"}}
    with pytest.raises(RuntimeError, match="header_invalid instead of decrypt_failed"):
        agent_vectors.negative("x", "check_label", label, "decrypt_failed")
    with pytest.raises(RuntimeError, match="no error instead of header_invalid"):
        agent_vectors.negative("x", "check_label", {"label": {"host": "h", "name": "n"}}, "header_invalid")


def test_a_label_must_be_canonical_body_json_and_hold_texts() -> None:
    bot, user = DATA["agent"], DATA["user"]
    info = agent.label_info(bot["id"], user["id"])
    user_kem = keys.x25519_private(b(user["kem_sk"]))
    spaced = hpke.seal(b(user["kem"]), info, b'{"host": "h", "name": "n"}')
    with pytest.raises(CryptoError) as refused:
        agent.open_label(user_kem, bot["id"], user["id"], b64u_encode(spaced))
    assert refused.value.code == "header_invalid"
    for value in ({"host": 5, "name": "n"}, {"host": "h"}, ["h", "n"]):
        with pytest.raises(CryptoError):
            agent.check_label(value)
    assert agent.check_label({"host": "Büro-Mac", "name": "x" * 64}) == {"host": "Büro-Mac", "name": "x" * 64}


def test_new_agent_keys_are_fresh_and_seal_with_random_values() -> None:
    first, second = agent.new_agent(), agent.new_agent()
    assert first.public() != second.public()
    user = keys.Identity.generate()
    secret = bytes(range(32))
    one = agent.seal_identity(first.kem_public(), secret, "a", "u", user)
    two = agent.seal_identity(first.kem_public(), secret, "a", "u", user)
    assert one != two
    opened = agent.open_identity(first.kem, secret, "a", "u", one)
    assert opened.public() == user.public()
    label = agent.seal_label(user.kem_public(), "a", "u", {"host": "h", "name": "n"})
    assert agent.open_label(user.kem, "a", "u", label) == {"host": "h", "name": "n"}
    with pytest.raises(CryptoError):
        agent.fingerprint(bytes(31), bytes(32))
    with pytest.raises(CryptoError):
        agent.claim_hash(bytes(16))
