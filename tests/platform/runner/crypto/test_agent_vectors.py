"""U-RUN-crypto (agent part, KOR-24): the copy `agent_runner.crypto.agent` against `contracts/vectors/agent.json`.

Every positive value of the vector file is recomputed with the copy (and the sealed values are opened with
independent pyca code), every negative vector fails through the copy with its code, and the copied section
regenerates the file byte for byte.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hpke as pyca_hpke
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from agent_runner.crypto import agent, keys
from agent_runner.crypto.agents import vectors as agent_vectors
from agent_runner.crypto.bundle import parse_bundle_plaintext
from agent_runner.crypto.encoding import P, b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.kdf import hkdf

VECTORS = Path(__file__).resolve().parents[4] / "contracts" / "vectors"
DATA: dict[str, Any] = json.loads((VECTORS / "agent.json").read_text(encoding="utf-8"))
SUITE = pyca_hpke.Suite(pyca_hpke.KEM.X25519, pyca_hpke.KDF.HKDF_SHA256, pyca_hpke.AEAD.AES_256_GCM)


def _agent() -> keys.Identity:
    data = DATA["agent"]
    return keys.Identity(
        keys.x25519_private(b64u_decode(data["kem_sk"])), keys.ed25519_private(b64u_decode(data["sig_sk"]))
    )


def test_section_regenerates_the_file() -> None:
    (section,) = agent_vectors.SECTIONS
    rendered = json.dumps(section.build(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    assert rendered == (VECTORS / "agent.json").read_text(encoding="utf-8")


def test_fingerprint_and_proof_of_possession() -> None:
    bot = _agent()
    kem_pub, sig_pub = bot.kem_public(), bot.sig_public()
    assert b64u_encode(kem_pub) == DATA["agent"]["kem"]
    assert agent.fingerprint(kem_pub, sig_pub) == DATA["fingerprint"]
    digest = hashlib.sha256(P + b"agent-key\x00" + kem_pub + sig_pub).digest()[:16]
    assert b64u_encode(digest) == DATA["fingerprint"]
    agent_id = DATA["agent"]["id"]
    assert b64u_encode(agent.pop_text(agent_id, kem_pub, sig_pub)) == DATA["pop"]["text"]
    assert agent.sign_pop(bot, agent_id) == DATA["pop"]["signature"]
    agent.verify_pop(agent_id, kem_pub, sig_pub, DATA["pop"]["signature"])


def test_secret_derivations() -> None:
    secret = b64u_decode(DATA["secret"]["secret"])
    agent_id, user_id = DATA["agent"]["id"], DATA["user"]["id"]
    ids = json.dumps({"agent": agent_id, "user": user_id}, sort_keys=True, separators=(",", ":")).encode()
    assert b64u_encode(agent.bundle_key(secret, agent_id, user_id)) == DATA["secret"]["bundle_key"]
    assert b64u_encode(hkdf(secret, P + b"agent-bundle-key\x00" + ids)) == DATA["secret"]["bundle_key"]
    claim = agent.claim(secret, agent_id, user_id)
    assert b64u_encode(claim) == DATA["secret"]["claim"]
    assert b64u_encode(agent.claim_hash(claim)) == DATA["secret"]["claim_hash"]
    assert b64u_encode(hashlib.sha256(P + b"agent-claim-hash\x00" + claim).digest()) == DATA["secret"]["claim_hash"]


def test_identity_opens_with_the_copy_and_with_independent_code() -> None:
    bot = _agent()
    secret = b64u_decode(DATA["secret"]["secret"])
    agent_id, user_id = DATA["agent"]["id"], DATA["user"]["id"]
    sealed = DATA["identity"]["sealed"]
    opened = agent.open_identity(bot.kem, secret, agent_id, user_id, sealed)
    assert b64u_encode(opened.kem_public()) == DATA["user"]["kem"]
    assert b64u_encode(opened.sig_public()) == DATA["user"]["sig"]
    info = b64u_decode(DATA["identity"]["info"])
    assert info == agent.identity_info(agent_id, user_id)
    inner = SUITE.decrypt(b64u_decode(sealed), bot.kem, info)
    key = b64u_decode(DATA["secret"]["bundle_key"])
    plaintext = AESGCM(key).decrypt(inner[:12], inner[12:], info)
    assert plaintext.decode() == DATA["identity"]["plaintext"]
    assert parse_bundle_plaintext(plaintext).sig_public() == opened.sig_public()
    resealed = agent.seal_identity(
        bot.kem_public(),
        secret,
        agent_id,
        user_id,
        opened,
        nonce=b64u_decode(DATA["identity"]["nonce"]),
        ephemeral=keys.x25519_private(b64u_decode(DATA["identity"]["ephemeral_sk"])),
    )
    assert resealed == sealed


def test_label_opens_and_reseals() -> None:
    user_kem = keys.x25519_private(b64u_decode(DATA["user"]["kem_sk"]))
    agent_id, user_id = DATA["agent"]["id"], DATA["user"]["id"]
    sealed = DATA["label"]["sealed"]
    assert agent.open_label(user_kem, agent_id, user_id, sealed) == DATA["label"]["value"]
    assert b64u_decode(DATA["label"]["info"]) == agent.label_info(agent_id, user_id)
    plaintext = SUITE.decrypt(b64u_decode(sealed), user_kem, b64u_decode(DATA["label"]["info"]))
    assert json.loads(plaintext) == DATA["label"]["value"]
    eph = keys.x25519_private(b64u_decode(DATA["label"]["ephemeral_sk"]))
    user_pub = keys.x25519_public_raw(user_kem)
    assert agent.seal_label(user_pub, agent_id, user_id, DATA["label"]["value"], eph) == sealed


def test_label_with_non_canonical_body_is_refused() -> None:
    user_kem = keys.x25519_private(b64u_decode(DATA["user"]["kem_sk"]))
    agent_id, user_id = DATA["agent"]["id"], DATA["user"]["id"]
    info = agent.label_info(agent_id, user_id)
    enc_ct = SUITE.encrypt(b'{"name":"n", "host":"h"}', user_kem.public_key(), info)
    with pytest.raises(CryptoError) as refused:
        agent.open_label(user_kem, agent_id, user_id, b64u_encode(enc_ct))
    assert refused.value.code == "header_invalid"


def test_new_agent_draws_fresh_keys() -> None:
    one, two = agent.new_agent(), agent.new_agent()
    assert one.sig_public() != two.sig_public()
    agent.verify_pop("p", one.kem_public(), one.sig_public(), agent.sign_pop(one, "p"))


@pytest.mark.parametrize("case", DATA["invalid"], ids=lambda case: f"{case['op']}:{case['name']}")
def test_negative_vector(case: dict[str, Any]) -> None:
    with pytest.raises(CryptoError) as refused:
        agent_vectors.OPS[case["op"]](case["args"])
    assert refused.value.code == case["error"]


def test_every_operation_has_a_negative_vector() -> None:
    assert {case["op"] for case in DATA["invalid"]} == set(agent_vectors.OPS)


def test_negative_refuses_a_wrong_expectation() -> None:
    args = {"label": {"host": "h", "name": "n"}}
    with pytest.raises(RuntimeError, match="no error instead of header_invalid"):
        agent_vectors.negative("x", "check_label", args, "header_invalid")
    with pytest.raises(RuntimeError, match="header_invalid instead of decrypt_failed"):
        agent_vectors.negative("x", "check_label", {"label": {}}, "decrypt_failed")


def test_remote_agent_code_and_typed_forms() -> None:
    remote = DATA["remote"]
    agent_id, nonce = DATA["agent"]["id"], b64u_decode(remote["nonce"])
    fp = DATA["fingerprint"]
    text = agent.code_input(agent_id, fp, nonce, remote["origin"])
    assert b64u_encode(text) == remote["code_input"]
    fields = {"fp": fp, "mode": "remote", "nonce": remote["nonce"], "origin": remote["origin"], "pairing_id": agent_id}
    assert text == P + b"agent-code\x00" + json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    assert agent.agent_code(agent_id, fp, nonce, remote["origin"]) == remote["agent_code"]
    assert agent.crockford32(hashlib.sha256(text).digest())[:10] == remote["agent_code"]
    assert agent.format_code(remote["agent_code"]) == remote["agent_code_shown"]
    for case in remote["typed"]:
        assert agent.parse_code(case["text"], case["length"]) == case["code"]


def test_remote_handover_opens_and_the_return_code_matches() -> None:
    remote, bot = DATA["remote"], _agent()
    agent_id, user_id = DATA["agent"]["id"], DATA["user"]["id"]
    secret = b64u_decode(DATA["secret"]["secret"])
    case = remote["handover"]
    info = agent.handover_info(agent_id, user_id)
    assert b64u_encode(info) == case["info"]
    assert SUITE.decrypt(b64u_decode(case["sealed"]), bot.kem, info).decode() == case["plaintext"]
    assert agent.open_handover(bot.kem, agent_id, user_id, case["sealed"]) == (secret, case["root_key"])
    eph = keys.x25519_private(b64u_decode(case["ephemeral_sk"]))
    resealed = agent.seal_handover(bot.kem_public(), agent_id, user_id, secret, case["root_key"], ephemeral=eph)
    assert resealed == case["sealed"]
    back = remote["return_code"]
    assert b64u_encode(agent.return_info(agent_id, user_id)) == remote["return_info"]
    assert agent.crockford32(hkdf(secret, b64u_decode(remote["return_info"])))[:8] == back
    assert agent.return_code(secret, agent_id, user_id) == back
    assert agent.return_code_matches(secret, agent_id, user_id, remote["return_code_shown"].lower())
    assert not agent.return_code_matches(secret, agent_id, user_id, "0000-0000")
    assert not agent.return_code_matches(secret, agent_id, user_id, "UUUU-UUUU")
