"""U-RUN-contexts: the runner's copy of the checkpoint crypto (`agent_runner.crypto.context`) against the shared
vectors `contracts/vectors/context.json` (ADR-027 section 2, ADR-072 sections 3 and 8)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_runner.crypto import context as c
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError

VECTORS: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[4] / "contracts" / "vectors" / "context.json").read_text(encoding="utf-8")
)


def test_the_positive_vector_is_reproduced() -> None:
    v = VECTORS
    wk, salt = b64u_decode(v["workgroup_key"]), b64u_decode(v["salt"])
    manifest = v["manifest"]
    key = c.context_key(wk, salt, manifest["object"])
    assert b64u_encode(key) == v["k_ctx"]
    chunks = [b64u_decode(chunk) for chunk in v["chunks"]]
    for index, part in enumerate(v["parts"]["chunks"]):
        assert c.chunk_aad(manifest["object"], index).decode() == part["aad"]
        sealed = c.seal_chunk(
            key, manifest["object"], index, b64u_decode(part["plaintext"]), b64u_decode(part["nonce"])
        )
        assert b64u_encode(sealed) == part["sealed"]
    assert c.manifest_text(manifest).decode() == v["manifest_text"]
    public = b64u_decode(v["author_sig_pk"])
    assert c.verify_manifest(manifest, v["sig"], public) == manifest
    assert c.digest(chunks) == manifest["digest"]
    assert c.open_context(wk, manifest, v["sig"], public, chunks) == b64u_decode(v["compressed"])
    assert (v["limits"]["chunk_bytes"], v["limits"]["chunk_ct_bytes"]) == (c.CHUNK_BYTES, c.CHUNK_CT_BYTES)
    link_key = c.pr_link_key(wk)
    assert b64u_encode(link_key) == v["pr_links"]["link_key"]
    for link in v["pr_links"]["links"]:
        assert c.pr_link_text(link["repo"], link["number"]).decode() == link["text"]
        assert c.pr_link_tag(link_key, link["repo"], link["number"]) == link["tag"]
    body_key = c.pr_link_body_key(wk)
    assert b64u_encode(body_key) == v["pr_link_bodies"]["body_key"]
    for body in v["pr_link_bodies"]["bodies"]:
        binding = {key: body[key] for key in ("wg_id", "epoch", "tag", "task_id", "origin")}
        assert c.pr_link_body_aad(**binding).decode() == body["aad"]
        body_sealed = c.seal_pr_link(
            body_key, **binding, repo=body["repo"], number=body["number"], nonce=b64u_decode(body["nonce"])
        )
        assert body_sealed == body["sealed"]
        assert c.open_pr_link(body_key, link_key, **binding, sealed=body_sealed) == (body["repo"], body["number"])


def _run(case: dict[str, Any]) -> None:
    args = case["args"]
    if case["op"] == "open_pr_link":
        wk = b64u_decode(args["workgroup_key"])
        binding = {key: args[key] for key in ("wg_id", "epoch", "tag", "task_id", "origin")}
        c.open_pr_link(c.pr_link_body_key(wk), c.pr_link_key(wk), **binding, sealed=args["sealed"])
    elif "sealed" in args:
        c.open_chunk(b64u_decode(args["key"]), args["object"], args["index"], b64u_decode(args["sealed"]))
    elif "repo" in args:
        c.pr_link_tag(c.pr_link_key(b64u_decode(args["workgroup_key"])), args["repo"], args["number"])
    elif "chunks" in args:
        chunks = [b64u_decode(chunk) for chunk in args["chunks"]]
        c.open_context(
            b64u_decode(args["workgroup_key"]),
            args["manifest"],
            args["sig"],
            b64u_decode(args["author_sig_pk"]),
            chunks,
        )
    else:
        c.verify_manifest(args["manifest"], args["sig"], b64u_decode(args["author_sig_pk"]))


@pytest.mark.parametrize("case", VECTORS["invalid"], ids=lambda case: case["name"])
def test_negative_vectors_are_refused_with_their_code(case: dict[str, Any]) -> None:
    with pytest.raises(CryptoError) as refused:
        _run(case)
    assert refused.value.code == case["error"]


def test_size_and_shape_refusals_of_the_copy() -> None:
    key = bytes(32)
    oid = VECTORS["manifest"]["object"]
    for chunk in (b"", bytes(c.CHUNK_BYTES + 1)):
        with pytest.raises(CryptoError, match="size_exceeded"):
            c.seal_chunk(key, oid, 0, chunk)
    with pytest.raises(CryptoError, match="size_exceeded"):
        c.open_chunk(key, oid, 0, bytes(c.CHUNK_CT_BYTES + 1))
    with pytest.raises(CryptoError, match="size_exceeded"):
        c.check_chunks(VECTORS["manifest"], [bytes(c.CHUNK_CT_BYTES + 1)])
    with pytest.raises(CryptoError, match="size_exceeded"):
        c.split(b"")
    assert c.split(b"x" * (c.CHUNK_BYTES + 1)) == [b"x" * c.CHUNK_BYTES, b"x"]
    with pytest.raises(CryptoError, match="header_invalid"):
        c.seal_context(
            key,
            {},
            epoch=1,
            ts="2026-11-23T12:00:00.000Z",
            compressed=b"x",
            author=None,  # type: ignore[arg-type]
            salt=bytes(32),
            nonces=[],
        )
    with pytest.raises(CryptoError, match="header_invalid"):
        c.pr_link_text(5, 1)
    with pytest.raises(CryptoError, match="header_invalid"):
        c.pr_link_origin(7)


def test_the_vector_generator_refuses_a_wrong_or_missing_error() -> None:
    from agent_runner.crypto.contexts import vectors  # noqa: PLC0415

    args = next(case["args"] for case in VECTORS["invalid"] if case["name"] == "pr_repo_non_ascii")
    with pytest.raises(RuntimeError, match="instead of"):
        vectors.negative("x", "pr_link_tag", args, "signature_invalid")
    good = {**args, "repo": "owner/repo", "number": 1}
    with pytest.raises(RuntimeError, match="no error"):
        vectors.negative("x", "pr_link_tag", good, "header_invalid")
