"""U-HUB-crypto (attachments): the attachments of prompts of `agent_hub.crypto.attachment` (04-crypto.md section 4.14,
ADR-102) and their vectors `contracts/vectors/attachment.json`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_hub.contexts import vectors as context_vectors
from agent_hub.crypto import attachment, runlog
from agent_hub.crypto.encoding import b64u_decode, b64u_encode
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.vectors import render

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
VECTORS = json.loads((CONTRACTS / "vectors" / "attachment.json").read_text(encoding="utf-8"))
WK = bytes(range(32))
SALT = bytes(32)
OBJECT = "01930043-0000-7000-8000-000000000001"


def code(call: Any) -> str:
    with pytest.raises(CryptoError) as refused:
        call()
    return refused.value.code


def seal(data: bytes, **extra: Any) -> tuple[dict[str, object], list[bytes]]:
    return attachment.seal_attachment(
        WK,
        object_id=OBJECT,
        name=extra.get("name", "report.pdf"),
        media_type=extra.get("media_type", "application/pdf"),
        epoch=1,
        data=data,
        salt=SALT,
        nonces=extra.get("nonces"),
    )


def test_the_vector_file_is_current_and_its_negatives_fail_as_stated() -> None:
    section = next(item for item in context_vectors.SECTIONS if item.name == "attachment")
    assert render(section) == (CONTRACTS / "vectors" / "attachment.json").read_text(encoding="utf-8")
    for vector in VECTORS["invalid"]:
        assert code(lambda v=vector: context_vectors.ATTACHMENT_OPS[v["op"]](v["args"])) == vector["error"], vector[
            "name"
        ]


def test_the_vector_generator_refuses_a_wrong_or_missing_error() -> None:
    with pytest.raises(RuntimeError, match="instead of non_ascii"):
        context_vectors.attachment_negative("x", "check_attachment_ref", {"ref": {}}, "non_ascii")
    with pytest.raises(RuntimeError, match="no error"):
        context_vectors.attachment_negative("x", "check_attachment_ref", {"ref": VECTORS["ref"]}, "header_invalid")


def test_the_positive_vector_opens() -> None:
    wk = b64u_decode(VECTORS["workgroup_key"])
    chunks = [b64u_decode(chunk) for chunk in VECTORS["chunks"]]
    assert attachment.open_attachment(wk, VECTORS["ref"], chunks) == b64u_decode(VECTORS["file"])
    key = attachment.attachment_key(wk, b64u_decode(VECTORS["salt"]), VECTORS["ref"]["object"])
    assert b64u_encode(key) == VECTORS["k_att"]
    part = VECTORS["part"]
    assert attachment.chunk_aad(VECTORS["ref"]["object"], part["index"]).decode("ascii") == part["aad"]
    assert attachment.open_chunk(key, VECTORS["ref"]["object"], part["index"], b64u_decode(part["sealed"])) == (
        b64u_decode(part["plaintext"])
    )
    assert VECTORS["limits"]["chunk_ct_bytes"] == attachment.CHUNK_BYTES + 28


def test_a_file_of_several_chunks_round_trips() -> None:
    data = bytes(range(256)) * (attachment.CHUNK_BYTES // 256) + b"tail"
    ref, chunks = seal(data)
    assert len(chunks) == 2 == attachment.chunk_count(len(data))
    assert sum(map(len, chunks)) == attachment.sealed_size(len(data)) == len(data) + 56
    assert ref["bytes"] == len(data)
    assert ref["digest"] == attachment.digest(chunks)
    assert attachment.open_attachment(WK, ref, chunks) == data
    # swapped or missing chunks never open
    assert code(lambda: attachment.open_attachment(WK, ref, [chunks[1], chunks[0]])) == "header_invalid"
    assert code(lambda: attachment.open_attachment(WK, ref, chunks[:1])) == "header_invalid"


def test_sizes_of_chunks_and_files() -> None:
    assert attachment.chunk_count(1) == 1
    assert attachment.chunk_count(attachment.CHUNK_BYTES) == 1
    assert attachment.chunk_count(attachment.CHUNK_BYTES + 1) == 2
    assert attachment.MAX_CHUNKS == 32
    key = attachment.attachment_key(WK, SALT, OBJECT)
    assert code(lambda: attachment.seal_chunk(key, OBJECT, 0, b"")) == "size_exceeded"
    assert code(lambda: attachment.seal_chunk(key, OBJECT, 0, bytes(attachment.CHUNK_BYTES + 1))) == "size_exceeded"
    assert code(lambda: attachment.open_chunk(key, OBJECT, 0, bytes(attachment.CHUNK_CT_BYTES + 1))) == (
        "size_exceeded"
    )
    assert code(lambda: attachment.chunk_aad(OBJECT, attachment.MAX_CHUNKS)) == "header_invalid"
    assert code(lambda: attachment.split(b"")) == "size_exceeded"
    assert code(lambda: attachment.split(bytes(attachment.MAX_BYTES + 1))) == "size_exceeded"
    assert attachment.split(b"abc") == [b"abc"]
    ref, chunks = seal(b"abc")
    assert code(lambda: attachment.check_chunks(ref, [bytes(attachment.CHUNK_CT_BYTES + 1)])) == "size_exceeded"
    assert code(lambda: attachment.check_chunks(ref, [*chunks, chunks[0]])) == "header_invalid"
    assert code(lambda: attachment.check_chunks(ref, [chunks[0] + b"x"])) == "header_invalid"


def test_keys_need_their_lengths_and_an_object_id() -> None:
    assert code(lambda: attachment.attachment_key(WK[:31], SALT, OBJECT)) == "header_invalid"
    assert code(lambda: attachment.attachment_key(WK, SALT[:31], OBJECT)) == "header_invalid"
    assert code(lambda: attachment.attachment_key(WK, SALT, "not-an-id")) == "header_invalid"
    assert attachment.attachment_key(WK, SALT, OBJECT) != runlog.log_key(WK, SALT, OBJECT)


def test_fixed_nonces_must_match_the_pieces() -> None:
    assert code(lambda: seal(b"abc", nonces=[])) == "header_invalid"
    one, _ = seal(b"abc", nonces=[bytes(12)])
    two, _ = seal(b"abc", nonces=[bytes(12)])
    assert one == two


@pytest.mark.parametrize(
    "name",
    ["", "x" * 256, "a/b", "a\\b", "a\x00b", "tab\there", "del\x7f", ".", "..", 7, None],
)
def test_names_that_are_refused(name: object) -> None:
    assert code(lambda: attachment.check_name(name)) == "header_invalid"


@pytest.mark.parametrize("name", ["a", "x" * 255, "Bildschirmfoto 2026.png", "...", ".env", "日本語.txt"])
def test_names_that_are_kept(name: str) -> None:
    assert attachment.check_name(name) == name


@pytest.mark.parametrize(
    "media_type", ["", "image", "Image/png", "image/", "/png", "image/png; charset=x", "a/" + "b" * 126, 5]
)
def test_types_that_are_refused(media_type: object) -> None:
    assert code(lambda: attachment.check_type(media_type)) == "header_invalid"


@pytest.mark.parametrize(
    "media_type", ["image/png", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", "text/x-c++"]
)
def test_types_that_are_kept(media_type: str) -> None:
    assert attachment.check_type(media_type) == media_type


def test_references_and_lists_of_them() -> None:
    ref, _ = seal(b"abc")
    assert attachment.check_ref(ref) is ref
    assert attachment.check_refs([]) == []
    assert attachment.check_refs([ref]) == [ref]
    other = {**ref, "object": "01930043-0000-7000-8000-000000000002"}
    assert attachment.check_refs([ref, other]) == [ref, other]
    assert code(lambda: attachment.check_refs({"0": ref})) == "header_invalid"
    assert code(lambda: attachment.check_refs([ref] * 2)) == "header_invalid"
    assert code(lambda: attachment.check_refs([{**ref, "object": f"x{n}"} for n in range(11)])) == "header_invalid"
    for key, value in (
        ("object", "x"),
        ("bytes", True),
        ("epoch", 0),
        ("salt", "AAAA"),
        ("digest", b64u_encode(bytes(31))),
    ):
        assert code(lambda k=key, v=value: attachment.check_ref({**ref, k: v})) == "header_invalid", key
