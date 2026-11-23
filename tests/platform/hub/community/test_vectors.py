"""The community vectors are current and consistent with the reference crypto (12-community.md section 3)."""

from __future__ import annotations

import json

import pytest

from agent_hub.crypto import community as fmt
from agent_hub.crypto.encoding import b64u_decode, b64u_encode
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.keys import x25519_private
from tests.platform.hub.community import vectors

DATA = json.loads(vectors.PATH.read_text(encoding="ascii"))


def test_the_vector_file_is_current() -> None:
    assert vectors.PATH.read_text(encoding="ascii") == vectors.text()


def test_the_vectors_open_with_the_reference() -> None:
    key = b64u_decode(DATA["community_key"], 32)
    assert b64u_encode(fmt.key_check(key, 1)) == DATA["key_check"]
    author_pk = b64u_decode(DATA["author_sig_pk"], 32)
    request = DATA["request"]
    fmt.verify_request(
        author_pk, id=request["id"], kem_pub=request["kem_pub"], user=DATA["user"], signature=request["sig"]
    )
    wrapped = DATA["request_wrap"]
    opened = fmt.unwrap_community_key(
        x25519_private(b64u_decode(request["kem_sk"], 32)), epoch=1, recipient=request["id"], wrap_text=wrapped["wrap"]
    )
    assert opened == key
    fmt.verify_community_wrap(
        b64u_decode(wrapped["wrapper_sig_pk"], 32),
        epoch=1,
        recipient=request["id"],
        wrap_text=wrapped["wrap"],
        signature=wrapped["sig"],
    )
    own = DATA["self_wrap"]
    assert (
        fmt.unwrap_community_key(
            x25519_private(b64u_decode(DATA["author_kem_sk"], 32)),
            epoch=1,
            recipient=DATA["user"],
            wrap_text=own["wrap"],
        )
        == key
    )
    assert fmt.open_message(key, DATA["message"]["sealed"], author_pk) == DATA["message"]["body"]


@pytest.mark.parametrize("case", DATA["invalid_messages"], ids=lambda case: case["name"])
def test_invalid_messages(case: dict[str, object]) -> None:
    with pytest.raises(CryptoError, match=str(case["error"])):
        fmt.verify_message(case["sealed"], b64u_decode(DATA["author_sig_pk"], 32))


@pytest.mark.parametrize("case", DATA["invalid_bodies"], ids=lambda case: case["name"])
def test_invalid_bodies(case: dict[str, object]) -> None:
    with pytest.raises(CryptoError, match="header_invalid"):
        fmt.check_body(case["body"])
