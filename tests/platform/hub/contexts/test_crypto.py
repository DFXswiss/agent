"""U-HUB-contexts (crypto): context checkpoints and pull-request tags of `agent_hub.crypto.context` (04-crypto.md
sections 3 and 4.11, ADR-072 sections 3 and 8) and their vectors `contracts/vectors/context.json`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_hub.contexts import vectors as context_vectors
from agent_hub.crypto import context
from agent_hub.crypto.encoding import b64u_decode
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.keys import Identity
from agent_hub.crypto.vectors import render

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
VECTORS = json.loads((CONTRACTS / "vectors" / "context.json").read_text(encoding="utf-8"))
WK = bytes(range(32))
SALT = bytes(32)
TS = "2026-11-23T12:00:00.000Z"


def ids(author: str = "01930033-0000-7000-8000-000000000006") -> dict[str, str]:
    return {
        "object": "01930033-0000-7000-8000-000000000001",
        "org": "01930033-0000-7000-8000-000000000002",
        "wg": "01930033-0000-7000-8000-000000000003",
        "task": "01930033-0000-7000-8000-000000000004",
        "session": "01930033-0000-7000-8000-000000000005",
        "author": author,
    }


def code(call: Any) -> str:
    with pytest.raises(CryptoError) as refused:
        call()
    return refused.value.code


def test_the_vector_file_is_current_and_its_negatives_fail_as_stated() -> None:
    section = context_vectors.SECTIONS[0]
    assert render(section) == (CONTRACTS / "vectors" / "context.json").read_text(encoding="utf-8")
    for vector in VECTORS["invalid"]:
        assert code(lambda v=vector: context_vectors.OPS[v["op"]](v["args"])) == vector["error"], vector["name"]


def test_the_vector_generator_refuses_a_wrong_or_missing_error() -> None:
    args = {"workgroup_key": VECTORS["workgroup_key"], "repo": "a/b", "number": 0}
    with pytest.raises(RuntimeError, match="instead of non_ascii"):
        context_vectors.negative("x", "pr_link_tag", args, "non_ascii")
    with pytest.raises(RuntimeError, match="no error"):
        context_vectors.negative("x", "pr_link_tag", {**args, "number": 1}, "header_invalid")


def test_the_positive_vector_opens() -> None:
    chunks = [b64u_decode(item) for item in VECTORS["chunks"]]
    opened = context.open_context(
        b64u_decode(VECTORS["workgroup_key"]),
        VECTORS["manifest"],
        VECTORS["sig"],
        b64u_decode(VECTORS["author_sig_pk"]),
        chunks,
    )
    assert opened == b64u_decode(VECTORS["compressed"])
    key = b64u_decode(VECTORS["k_ctx"])
    for part in VECTORS["parts"]["chunks"]:
        sealed = b64u_decode(part["sealed"])
        assert context.open_chunk(key, VECTORS["manifest"]["object"], part["index"], sealed) == b64u_decode(
            part["plaintext"]
        )
    assert context.digest([b64u_decode(p["sealed"]) for p in VECTORS["parts"]["chunks"]]) == VECTORS["parts"]["digest"]
    link_key = context.pr_link_key(b64u_decode(VECTORS["workgroup_key"]))
    first, lowered, _ = VECTORS["pr_links"]["links"]
    assert first["tag"] == lowered["tag"] == context.pr_link_tag(link_key, "EXAMPLE-ORG/example-repo", 17)


def test_a_large_archive_is_split_sealed_and_joined_again() -> None:
    device = Identity.generate()
    compressed = os.urandom(2 * context.CHUNK_BYTES + 5)
    manifest, sig, sealed = context.seal_context(
        WK, ids(), epoch=1, ts=TS, compressed=compressed, author=device.sig, salt=SALT
    )
    assert [len(item) for item in sealed] == [context.CHUNK_CT_BYTES, context.CHUNK_CT_BYTES, 5 + 28]
    assert manifest["chunks"] == 3
    assert manifest["size"] == sum(len(item) for item in sealed)
    assert context.open_context(WK, manifest, sig, device.sig_public(), sealed) == compressed
    # chunks in another order, one missing or one too many never open
    assert code(lambda: context.open_context(WK, manifest, sig, device.sig_public(), sealed[::-1])) == "header_invalid"
    assert code(lambda: context.open_context(WK, manifest, sig, device.sig_public(), sealed[:2])) == "header_invalid"
    big = [*sealed[:2], sealed[2] + bytes(context.CHUNK_CT_BYTES)]
    assert code(lambda: context.check_chunks(manifest, big)) == "size_exceeded"


def test_sealing_refusals() -> None:
    device = Identity.generate()
    assert code(lambda: context.split(b"")) == "size_exceeded"
    assert (
        code(
            lambda: context.seal_context(
                WK, ids(), epoch=1, ts=TS, compressed=b"x", author=device.sig, salt=SALT, nonces=[]
            )
        )
        == "header_invalid"
    )
    key = context.context_key(WK, SALT, ids()["object"])
    assert code(lambda: context.seal_chunk(key, ids()["object"], 0, b"")) == "size_exceeded"
    assert code(lambda: context.seal_chunk(key, ids()["object"], 0, bytes(context.CHUNK_BYTES + 1))) == "size_exceeded"
    assert code(lambda: context.open_chunk(key, ids()["object"], 0, bytes(context.CHUNK_CT_BYTES + 1))) == (
        "size_exceeded"
    )
    assert code(lambda: context.chunk_aad(ids()["object"], context.MAX_CHUNKS)) == "header_invalid"
    assert code(lambda: context.context_key(WK, SALT, "not-an-id")) == "header_invalid"
    assert code(lambda: context.context_key(WK[:31], SALT, ids()["object"])) == "header_invalid"


def test_manifest_signature_and_format() -> None:
    device = Identity.generate()
    manifest, sig, _ = context.seal_context(WK, ids(), epoch=1, ts=TS, compressed=b"abc", author=device.sig, salt=SALT)
    assert context.verify_manifest(manifest, sig, device.sig_public()) == manifest
    assert code(lambda: context.verify_manifest(manifest, sig, Identity.generate().sig_public())) == (
        "signature_invalid"
    )
    assert code(lambda: context.verify_manifest(manifest, "not b64u!", device.sig_public())) == "signature_invalid"
    for change in ({"size": 2 * context.CHUNK_CT_BYTES}, {"epoch": 0}, {"salt": "AAAA"}, {"ts": "x"}):
        assert code(lambda c=change: context.check_manifest({**manifest, **c})) == "header_invalid", change
    assert code(lambda: context.sign_manifest(device.sig, {**manifest, "v": 2})) == "header_invalid"
    assert context.manifest_text(manifest).startswith(b"agent.dfx.swiss/v1/context\x00{")


def test_pull_request_tags() -> None:
    link_key = context.pr_link_key(WK)
    tag = context.pr_link_tag(link_key, "Org/Repo", 3)
    assert tag == context.pr_link_tag(link_key, "org/repo", 3)
    assert tag != context.pr_link_tag(link_key, "org/repo", 4)
    assert tag != context.pr_link_tag(context.pr_link_key(WK[::-1]), "org/repo", 3)
    assert context.pr_link_text("A/B", 1) == b'{"number":1,"repo":"a/b"}'
    assert code(lambda: context.pr_link_text(None, 1)) == "header_invalid"
    assert code(lambda: context.pr_link_text("a/b", True)) == "header_invalid"
    assert code(lambda: context.pr_link_text("a/b/c", 1)) == "header_invalid"
    assert code(lambda: context.pr_link_key(WK[:16])) == "header_invalid"
