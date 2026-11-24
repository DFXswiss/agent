"""Copied from agent-core tests/platform/hub/crypto/test_identity.py at 952bb156fc9c.

CRY-01 (ADR-060): identity proof, derived user ID and account code (agent_hub.crypto.identity) against
`contracts/vectors/identity.json`, and the edges the vectors leave out."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_runner.crypto import identity, invite
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.keys import Identity

VECTORS = Path(__file__).resolve().parents[4] / "contracts" / "vectors"
DATA: dict[str, Any] = json.loads((VECTORS / "identity.json").read_text(encoding="utf-8"))


def test_identity_vectors() -> None:
    secrets = invite.invite_secrets(b64u_decode(DATA["invite"]["code"]))
    assert secrets.invite_id == DATA["invite"]["invite_id"]
    assert secrets.pk_inv == DATA["invite"]["pk_inv"]
    user = identity.invited_user_id(secrets.invite_id, DATA["invite"]["ts"])
    assert user == DATA["user_id"]
    assert b64u_encode(identity.identity_text(user, DATA["pub"])) == DATA["text"]
    assert identity.sign_identity(secrets.signing, user, DATA["pub"]) == DATA["id_sig"]
    assert identity.identity_valid(secrets.pk_inv, user, DATA["pub"], DATA["id_sig"])
    assert (
        identity.identity_valid(secrets.pk_inv, user, DATA["other_pub"], DATA["id_sig"])
        is DATA["id_sig_other_keys_valid"]
    )
    assert identity.account_code(user, DATA["pub"]) == DATA["account_code"]
    assert identity.format_account_code(DATA["account_code"]) == DATA["account_code_text"]
    assert identity.account_code(user, DATA["other_pub"]) == DATA["account_code_other_keys"]
    assert DATA["invalid"] == []


@pytest.mark.parametrize("case", DATA["parse"], ids=[case["text"] for case in DATA["parse"]])
def test_account_code_parse_vectors(case: dict[str, str]) -> None:
    if "error" in case:
        with pytest.raises(CryptoError) as refused:
            identity.parse_account_code(case["text"])
        assert refused.value.code == case["error"]
    else:
        assert identity.parse_account_code(case["text"]) == case["code"]


@pytest.mark.parametrize("value", [None, 12, b"ABCDEFGH2345", "", "ABCD-EFGH-23é5"])
def test_account_code_that_is_no_text_or_malformed_is_invalid(value: object) -> None:
    with pytest.raises(CryptoError, match="account_code_invalid"):
        identity.parse_account_code(value)


def test_identity_proof_of_another_user_or_key_is_invalid() -> None:
    secrets = invite.invite_secrets(b64u_decode(DATA["invite"]["code"]))
    other = invite.invite_secrets(bytes(16))
    user, pub = DATA["user_id"], DATA["pub"]
    assert not identity.identity_valid(other.pk_inv, user, pub, DATA["id_sig"])
    assert not identity.identity_valid(secrets.pk_inv, "01a1fe1a-4495-70ca-90ff-34dadbdf67b5", pub, DATA["id_sig"])
    assert not identity.identity_valid(secrets.pk_inv, user, pub, "")
    assert not identity.identity_valid(secrets.pk_inv, user, pub, 42)


@pytest.mark.parametrize(
    ("user", "pub"),
    [
        ("not-a-uuid", DATA["pub"]),
        (DATA["user_id"], {"kem": DATA["pub"]["kem"]}),
        (DATA["user_id"], {**DATA["pub"], "extra": "x"}),
        (DATA["user_id"], {**DATA["pub"], "sig": "AAAA"}),
        (DATA["user_id"], "keys"),
    ],
)
def test_malformed_user_or_keys_are_refused(user: str, pub: Any) -> None:
    with pytest.raises(CryptoError, match="header_invalid"):
        identity.identity_text(user, pub)
    with pytest.raises(CryptoError, match="header_invalid"):
        identity.account_code(user, pub)


def test_account_codes_round_trip_through_the_parser() -> None:
    keys = Identity.generate().public()
    code = identity.account_code(DATA["user_id"], keys)
    assert len(code) == identity.ACCOUNT_CODE_LENGTH
    assert identity.parse_account_code(identity.format_account_code(code).lower()) == code


def test_invited_user_id_carries_the_milliseconds_of_the_invitation() -> None:
    found = identity.invited_user_id(DATA["invite"]["invite_id"], "1970-01-01T00:00:00.001Z")
    assert found.startswith("00000000-0001-7")
    with pytest.raises(CryptoError, match="header_invalid"):
        identity.invited_user_id("AAAA", DATA["invite"]["ts"])
