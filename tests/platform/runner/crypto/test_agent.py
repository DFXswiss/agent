"""Copied from tests/platform/hub/crypto/test_agent.py.

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

from agent_runner.crypto import aead, agent, hpke, keys, vectors
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


def test_a_request_message_follows_the_label_rules_up_to_500_characters() -> None:
    assert (
        agent.check_message("Hallo \u2013 <b>ich</b> bin's: https://example.com")
        == "Hallo \u2013 <b>ich</b> bin's: https://example.com"
    )
    assert agent.check_message("ü" * 500) == "ü" * 500
    for value in ("", "x" * 501, "a\nb", "a\u202eb", "x ", 5, None):
        with pytest.raises(CryptoError) as refused:
            agent.check_message(value)
        assert refused.value.code == "header_invalid"


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


CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def crockford(raw: bytes, length: int) -> str:
    number = int.from_bytes(raw, "big")
    bits = len(raw) * 8
    return "".join(CROCKFORD[(number >> (bits - 5 * (index + 1))) & 31] for index in range(length))


def test_the_remote_agent_code_is_reproduced_with_independent_code() -> None:
    bot, remote = DATA["agent"], DATA["remote"]
    fields = {
        "fp": DATA["fingerprint"],
        "mode": "remote",
        "nonce": remote["nonce"],
        "origin": remote["origin"],
        "pairing_id": bot["id"],
    }
    text = PREFIX + b"agent-code\x00" + json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    assert b(remote["code_input"]) == text
    assert remote["agent_code"] == crockford(hashlib.sha256(text).digest(), 10)
    assert remote["agent_code_shown"] == remote["agent_code"][:5] + "-" + remote["agent_code"][5:]
    code = agent.agent_code(bot["id"], DATA["fingerprint"], b(remote["nonce"]), remote["origin"])
    assert code == remote["agent_code"]
    assert agent.crockford32(bytes([0xFF, 0x00])) == "ZW00"


def test_the_remote_handover_and_return_code_open_with_independent_code() -> None:
    bot, user, remote = DATA["agent"], DATA["user"], DATA["remote"]
    case = remote["handover"]
    info = PREFIX + b"agent-handover\x00" + ids(bot["id"], user["id"])
    assert b(case["info"]) == info
    plaintext = SUITE.decrypt(b(case["sealed"]), X25519PrivateKey.from_private_bytes(b(bot["kem_sk"])), info=info)
    assert plaintext.decode() == case["plaintext"]
    assert json.loads(plaintext) == {"k": case["root_key"], "r": DATA["secret"]["secret"]}
    kem = keys.x25519_private(b(bot["kem_sk"]))
    secret, root_key = agent.open_handover(kem, bot["id"], user["id"], case["sealed"])
    assert (b64u_encode(secret), root_key) == (DATA["secret"]["secret"], case["root_key"])
    resealed = agent.seal_handover(
        b(bot["kem"]), bot["id"], user["id"], secret, root_key, ephemeral=keys.x25519_private(b(case["ephemeral_sk"]))
    )
    assert resealed == case["sealed"]
    return_info = PREFIX + b"agent-return\x00" + ids(bot["id"], user["id"])
    assert b(remote["return_info"]) == return_info
    assert remote["return_code"] == crockford(hkdf(secret, return_info), 8)
    assert agent.return_code(secret, bot["id"], user["id"]) == remote["return_code"]
    assert agent.format_code(remote["return_code"]) == remote["return_code_shown"]
    assert agent.return_code_matches(secret, bot["id"], user["id"], remote["return_code_shown"].lower())
    assert not agent.return_code_matches(secret, bot["id"], user["id"], "0000-0000")
    assert not agent.return_code_matches(secret, bot["id"], user["id"], "not a code!")


@pytest.mark.parametrize("case", DATA["remote"]["typed"], ids=lambda case: case["text"])
def test_typed_codes_are_normalised(case: dict[str, Any]) -> None:
    assert agent.parse_code(case["text"], case["length"]) == case["code"]


def test_a_handover_with_a_bad_secret_or_root_key_is_refused() -> None:
    bot, user = DATA["agent"], DATA["user"]
    kem = keys.x25519_private(b(bot["kem_sk"]))
    info = agent.handover_info(bot["id"], user["id"])
    for body in ({"k": "x", "r": DATA["secret"]["secret"]}, {"k": DATA["remote"]["handover"]["root_key"], "r": "AA"}):
        sealed = b64u_encode(hpke.seal(b(bot["kem"]), info, json.dumps(body, separators=(",", ":")).encode()))
        with pytest.raises(CryptoError) as refused:
            agent.open_handover(kem, bot["id"], user["id"], sealed)
        assert refused.value.code == "header_invalid"
    with pytest.raises(CryptoError):
        agent.seal_handover(b(bot["kem"]), bot["id"], user["id"], bytes(16), DATA["remote"]["handover"]["root_key"])


def canonical_text(label: bytes, fields: dict[str, Any]) -> bytes:
    return PREFIX + label + json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()


def test_the_account_text_and_its_three_signatures_are_reproduced_with_independent_code() -> None:
    own = DATA["own_account"]
    account, bot, user = own["account"], DATA["agent"], DATA["user"]
    assert own["methods"] == ["agent-signature-v1"] == list(agent.PROOF_METHODS)
    text = canonical_text(
        b"agent-account\x00",
        {
            "agent": bot["id"],
            "agent_pub": {"kem": bot["kem"], "sig": bot["sig"]},
            "invite_id": account["invite_id"],
            "method": "agent-signature-v1",
            "pub": {"kem": user["kem"], "sig": user["sig"]},
            "user": user["id"],
        },
    )
    assert b(account["text"]) == text
    signing = Ed25519PrivateKey.from_private_bytes(hkdf(b(account["invite_code"]), PREFIX + b"invite-sign"))
    assert b64u_encode(signing.public_key().public_bytes_raw()) == account["pk_inv"]
    for signer, signature in ((account["pk_inv"], "inv_sig"), (user["sig"], "id_sig"), (bot["sig"], "agent_sig")):
        Ed25519PublicKey.from_public_bytes(b(signer)).verify(b(account[signature]), text)
        agent.verify_text(b(signer), account[signature], text)
    assert agent.sign_text(signing, text) == account["inv_sig"]


def test_the_recover_text_and_its_signatures_are_reproduced_with_independent_code() -> None:
    recover, bot, user = DATA["own_account"]["recover"], DATA["agent"], DATA["user"]
    text = canonical_text(
        b"agent-recover\x00",
        {
            "agent": bot["id"],
            "agent_pub": {"kem": bot["kem"], "sig": bot["sig"]},
            "challenge": recover["challenge"],
            "method": "agent-signature-v1",
            "user": user["id"],
        },
    )
    assert b(recover["text"]) == text
    assert len(b(recover["challenge"])) == agent.CHALLENGE_BYTES
    Ed25519PublicKey.from_public_bytes(b(user["sig"])).verify(b(recover["id_sig"]), text)
    Ed25519PublicKey.from_public_bytes(b(bot["sig"])).verify(b(recover["agent_sig"]), text)


def test_a_proof_method_outside_the_list_and_short_keys_are_refused() -> None:
    assert agent.check_method("agent-signature-v1") == "agent-signature-v1"
    for method in ("webauthn", "", None, 1):
        with pytest.raises(CryptoError) as refused:
            agent.check_method(method)
        assert refused.value.code == "header_invalid"
    with pytest.raises(CryptoError) as short:
        agent.recover_text(
            agent_id="a",
            agent_kem=bytes(31),
            agent_sig=bytes(32),
            challenge=bytes(32),
            method="agent-signature-v1",
            user_id="u",
        )
    assert short.value.code == "header_invalid"


def test_the_delegation_certificate_is_reproduced_with_independent_code() -> None:
    """ADR-082 sections 1 and 9.1: the user signs the agent's ID, keys, generation and version."""
    bot, user, case = DATA["agent"], DATA["user"], DATA["own_identity"]["cert"]
    pub = {"kem": bot["kem"], "sig": bot["sig"]}
    certified = {"agent": bot["id"], "gen": 1, "pub": pub, "user": user["id"], "v": 1}
    text = PREFIX + b"agent-cert\x00" + json.dumps(certified, sort_keys=True, separators=(",", ":")).encode()
    assert b(case["text"]) == text
    assert case["gen"] == 1
    assert agent.cert_text(bot["id"], case["pub"], user["id"], 1) == text
    Ed25519PublicKey.from_public_bytes(b(user["sig"])).verify(b(case["cert"]), text)
    alice = Ed25519PrivateKey.from_private_bytes(vectors.fixed("alice/sig"))
    assert agent.sign_cert(alice, bot["id"], case["pub"], user["id"], 1) == case["cert"]
    agent.verify_cert(b(user["sig"]), case["cert"], agent_id=bot["id"], pub=case["pub"], user_id=user["id"], gen=1)
    for gen in (0, True, "1", 2**31):
        with pytest.raises(CryptoError) as refused:
            agent.cert_text(bot["id"], case["pub"], user["id"], gen)
        assert refused.value.code == "header_invalid"
    bad: list[tuple[object, str]] = [({"kem": bot["kem"]}, bot["id"]), (case["pub"], "agent"), ([1, 2], bot["id"])]
    for wrong, agent_id in bad:
        with pytest.raises(CryptoError):
            agent.cert_text(agent_id, wrong, user["id"], 1)


def test_the_user_anchor_opens_with_independent_code_and_is_reproduced() -> None:
    """ADR-082 section 2: the user's public keys and the certificate, bound to `r` and sealed to the agent."""
    bot, user, case = DATA["agent"], DATA["user"], DATA["own_identity"]["user_anchor"]
    info = PREFIX + b"agent-user\x00" + ids(bot["id"], user["id"])
    assert b(case["info"]) == info
    inner = SUITE.decrypt(b(case["sealed"]), X25519PrivateKey.from_private_bytes(b(bot["kem_sk"])), info=info)
    nonce = inner[:12]
    assert nonce == b(case["nonce"])
    plaintext = AESGCM(b(DATA["secret"]["bundle_key"])).decrypt(nonce, inner[12:], info)
    assert plaintext.decode() == case["plaintext"]
    cert = DATA["own_identity"]["cert"]["cert"]
    expected = {"cert": cert, "kem": user["kem"], "sig": user["sig"]}
    assert plaintext == json.dumps(expected, sort_keys=True, separators=(",", ":")).encode()
    bot_kem, secret = keys.x25519_private(b(bot["kem_sk"])), b(DATA["secret"]["secret"])
    assert agent.open_user(bot_kem, secret, bot["id"], user["id"], case["sealed"]) == expected
    resealed = agent.seal_user(
        b(bot["kem"]),
        secret,
        bot["id"],
        user["id"],
        user_pub={"kem": user["kem"], "sig": user["sig"]},
        cert=cert,
        nonce=nonce,
        ephemeral=keys.x25519_private(b(case["ephemeral_sk"])),
    )
    assert resealed == case["sealed"]
    user_pub = {"kem": user["kem"], "sig": user["sig"]}
    fresh = agent.seal_user(b(bot["kem"]), secret, bot["id"], user["id"], user_pub=user_pub, cert=cert)
    assert fresh != case["sealed"]


def test_a_user_anchor_must_hold_keys_and_a_signature() -> None:
    first = agent.new_agent()
    user = keys.Identity.generate()
    secret = bytes(range(32))
    cert = b64u_encode(bytes(64))
    sealed = agent.seal_user(first.kem_public(), secret, "a", "u", user_pub=user.public(), cert=cert)
    assert agent.open_user(first.kem, secret, "a", "u", sealed) == {"cert": cert, **user.public()}
    for user_pub, signature in ((user.public(), b64u_encode(bytes(63))), ({"kem": "x", "sig": "y"}, cert)):
        with pytest.raises(CryptoError) as refused:
            agent.seal_user(first.kem_public(), secret, "a", "u", user_pub=user_pub, cert=signature)
        assert refused.value.code == "header_invalid"
    info = agent.user_info("a", "u")
    inner = aead.seal_blob(agent.bundle_key(secret, "a", "u"), b"[]", info)
    with pytest.raises(CryptoError) as refused:
        agent.open_user(first.kem, secret, "a", "u", b64u_encode(hpke.seal(first.kem_public(), info, inner)))
    assert refused.value.code == "header_invalid"


def test_the_orchestrator_texts_are_signed_by_the_device_and_by_the_owner() -> None:
    """ADR-082 sections 6.2 and 9.8: the device signs the fingerprint of the session's keys, the task owner the sealed
    handover; both texts rebuilt independently."""
    section = DATA["orchestrator"]
    session = section["session_id"]
    key = section["key"]
    assert key["fp"] == DATA["fingerprint"]
    assert key["signer"] == section["device"]["sig"]
    key_fields = {"fp": key["fp"], "pairing_id": key["pairing_id"], "session": session}
    key_text = PREFIX + b"orchestrator-key\x00" + json.dumps(key_fields, sort_keys=True, separators=(",", ":")).encode()
    assert b(key["text"]) == key_text == agent.orchestrator_key_text(key["fp"], key["pairing_id"], session)
    Ed25519PublicKey.from_public_bytes(b(key["signer"])).verify(b(key["signature"]), key_text)
    handover = section["handover"]
    assert handover["signer"] == DATA["user"]["sig"]
    assert handover["ct"] == DATA["remote"]["handover"]["sealed"]
    fields = {"ct": handover["ct"], "pairing": handover["pairing_id"], "session": session}
    text = PREFIX + b"orchestrator-handover\x00" + json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    assert (
        b(handover["text"]) == text == agent.orchestrator_handover_text(handover["ct"], handover["pairing_id"], session)
    )
    Ed25519PublicKey.from_public_bytes(b(handover["signer"])).verify(b(handover["signature"]), text)
