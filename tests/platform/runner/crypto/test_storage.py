"""U-RUN-crypto (storage part): the runner storage key and local sealing (agent_runner.crypto.storage).

The storage key derivation is part of the shared vectors (`contracts/vectors/kdf.json`, `storage_key`); the local
sealing format is runner-only, so its vectors live in `data/storage.json` (decisions/CRY-03.md) and are generated
here from fixed inputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from agent_runner.crypto import aead, storage
from agent_runner.crypto.encoding import P, b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.vectors import fixed

REPO_ROOT = Path(__file__).resolve().parents[4]
KDF_VECTORS = REPO_ROOT / "contracts" / "vectors" / "kdf.json"
STORAGE_VECTORS = Path(__file__).with_name("data") / "storage.json"
PLAINTEXTS = {
    "outbox": '{"body":"Grüße, ✓","item_kind":"event"}'.encode(),
    "scope_key": fixed("storage/scope-key"),
    "credential": b'{"header":{"kind":"github"},"ct":"AAEC","sig":"AwQF"}',
}


def contexts() -> dict[str, str]:
    return {
        "outbox": storage.outbox_context(42, "event"),
        "scope_key": storage.scope_key_context("wg", "0196a1b2-0000-7000-8000-000000000001", 3),
        "credential": storage.credential_context("0196a1b2-0000-7000-8000-000000000002", "github", 2),
    }


def build_storage_vectors() -> dict[str, Any]:
    """The runner-only vectors: key from the shared seed, one sealed blob per purpose, negative cases."""
    seed = b64u_decode(json.loads(KDF_VECTORS.read_text(encoding="utf-8"))["storage_key"]["device_sig_seed"])
    key = storage.storage_key(seed)
    valid = []
    for purpose, context in contexts().items():
        nonce = fixed(f"storage/nonce/{purpose}", aead.NONCE_BYTES)
        aad = storage.local_aad(purpose, context)
        blob = aead.seal_blob(key, PLAINTEXTS[purpose], aad, nonce)
        valid.append(
            {
                "aad": b64u_encode(aad),
                "blob": b64u_encode(blob),
                "context": context,
                "nonce": b64u_encode(nonce),
                "plaintext": b64u_encode(PLAINTEXTS[purpose]),
                "purpose": purpose,
            }
        )
    outbox = valid[0]
    tampered = bytearray(b64u_decode(outbox["blob"]))
    tampered[-1] ^= 1
    invalid = [
        {"name": "other purpose", "purpose": "credential", "context": outbox["context"], "blob": outbox["blob"]},
        {"name": "other row", "purpose": "outbox", "context": "43:event", "blob": outbox["blob"]},
        {"name": "other item kind", "purpose": "outbox", "context": "42:audit", "blob": outbox["blob"]},
        {"name": "tampered tag", "purpose": "outbox", "context": "42:event", "blob": b64u_encode(bytes(tampered))},
        {"name": "short", "purpose": "outbox", "context": "42:event", "blob": b64u_encode(bytes(27))},
    ]
    for case in invalid:
        case["error"] = "decrypt_failed"
    return {
        "about": "Runner-only local sealing (agent_runner.crypto.storage, decisions/CRY-03.md); the key comes from "
        "the storage_key vector of contracts/vectors/kdf.json.",
        "device_sig_seed": b64u_encode(seed),
        "invalid": invalid,
        "key": b64u_encode(key),
        "valid": valid,
    }


def render() -> str:
    return json.dumps(build_storage_vectors(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def vectors() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(STORAGE_VECTORS.read_text(encoding="utf-8"))
    return result


def test_storage_vectors_file_is_current() -> None:
    assert STORAGE_VECTORS.read_text(encoding="utf-8") == render(), "regenerate data/storage.json from render()"


def test_storage_key_matches_the_shared_vector() -> None:
    shared = json.loads(KDF_VECTORS.read_text(encoding="utf-8"))["storage_key"]
    key = storage.storage_key(b64u_decode(shared["device_sig_seed"]))
    assert b64u_encode(key) == shared["key"]
    assert vectors()["key"] == shared["key"]


def test_storage_key_refuses_a_wrong_seed_length() -> None:
    with pytest.raises(CryptoError, match="header_invalid"):
        storage.storage_key(bytes(31))


@pytest.mark.parametrize("case", vectors()["valid"], ids=lambda case: case["purpose"])
def test_open_local_opens_the_vectors(case: dict[str, Any]) -> None:
    key = b64u_decode(vectors()["key"])
    assert b64u_decode(case["aad"]) == P + b"runner-storage\0" + case["purpose"].encode() + b"\0" + (
        case["context"].encode()
    )
    plaintext = storage.open_local(key, case["purpose"], case["context"], b64u_decode(case["blob"]))
    assert plaintext == b64u_decode(case["plaintext"])
    assert b64u_decode(case["blob"])[: aead.NONCE_BYTES] == b64u_decode(case["nonce"])


@pytest.mark.parametrize("case", vectors()["invalid"], ids=lambda case: case["name"])
def test_open_local_refuses_the_negative_vectors(case: dict[str, Any]) -> None:
    key = b64u_decode(vectors()["key"])
    with pytest.raises(CryptoError, match=case["error"]) as caught:
        storage.open_local(key, case["purpose"], case["context"], b64u_decode(case["blob"]))
    assert caught.value.__context__ is None


def test_seal_local_round_trip_with_fresh_nonces() -> None:
    key = storage.storage_key(fixed("storage/other-seed"))
    first = storage.seal_local(key, "outbox", "7", b"body")
    second = storage.seal_local(key, "outbox", "7", b"body")
    assert first[: aead.NONCE_BYTES] != second[: aead.NONCE_BYTES]
    assert len(first) == aead.NONCE_BYTES + len(b"body") + aead.TAG_BYTES
    assert storage.open_local(key, "outbox", "7", first) == b"body"
    assert storage.open_local(key, "outbox", "7", second) == b"body"
    with pytest.raises(CryptoError, match="decrypt_failed"):
        storage.open_local(storage.storage_key(fixed("storage/third-seed")), "outbox", "7", first)


def test_wrong_key_length_is_refused() -> None:
    with pytest.raises(CryptoError, match="header_invalid"):
        storage.seal_local(bytes(16), "outbox", "1", b"x")
    with pytest.raises(CryptoError, match="header_invalid"):
        storage.open_local(bytes(16), "outbox", "1", bytes(40))


@pytest.mark.parametrize(
    ("purpose", "context"),
    [("journal", "1"), ("outbox", ""), ("outbox", "a\0b"), ("outbox", "ä"), ("outbox", "x" * 1025)],
)
def test_local_aad_refuses_unknown_purposes_and_bad_contexts(purpose: str, context: str) -> None:
    with pytest.raises(ValueError, match=r"purpose|context"):
        storage.local_aad(purpose, context)


def test_contexts() -> None:
    assert storage.outbox_context(0, "audit") == "0:audit"
    assert storage.outbox_context(2**63 - 1, "event") == "9223372036854775807:event"
    assert storage.scope_key_context("org", "o", 0) == "org:o:0"
    assert storage.credential_context("u", "openrouter", 7) == "u:openrouter:7"
    for bad in (-1, True, 2**63, cast(int, "1")):
        with pytest.raises(ValueError, match="origin_seq"):
            storage.outbox_context(bad, "event")
    with pytest.raises(ValueError, match="item_kind"):
        storage.outbox_context(1, "")
    with pytest.raises(ValueError, match="version"):
        storage.credential_context("u", "github", -1)
    with pytest.raises(ValueError, match="scope"):
        storage.scope_key_context("team", "o", 0)
    with pytest.raises(ValueError, match="epoch"):
        storage.scope_key_context("wg", "w", -1)
    with pytest.raises(ValueError, match="epoch"):
        storage.scope_key_context("wg", "w", False)
    for scope_id in ("", "a:b"):
        with pytest.raises(ValueError, match="scope_id"):
            storage.scope_key_context("wg", scope_id, 0)
    for owner, kind in (("", "github"), ("u", ""), ("u:v", "github"), ("u", "a:b")):
        with pytest.raises(ValueError, match=r"owner_id|kind"):
            storage.credential_context(owner, kind, 0)
