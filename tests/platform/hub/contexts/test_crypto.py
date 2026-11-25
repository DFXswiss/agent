"""U-HUB-contexts (crypto): context checkpoints and pull-request tags of `agent_hub.crypto.context` (04-crypto.md
sections 3 and 4.11, ADR-072 sections 3 and 8) and their vectors `contracts/vectors/context.json`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_hub.contexts import vectors as context_vectors
from agent_hub.crypto import aead, context
from agent_hub.crypto.encoding import b64u_decode, b64u_encode
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


def _binding(**changes: object) -> dict[str, Any]:
    tag = context.pr_link_tag(context.pr_link_key(WK), "Org/Repo", 3)
    binding: dict[str, Any] = {
        "wg_id": ids()["wg"],
        "epoch": 4,
        "tag": tag,
        "task_id": ids()["task"],
        "origin": f"user:{ids()['author']}",
    }
    return {**binding, **changes}


def test_the_sealed_link_body_vectors_open_and_are_reproduced() -> None:
    wk = b64u_decode(VECTORS["workgroup_key"])
    body_key, link_key = context.pr_link_body_key(wk), context.pr_link_key(wk)
    data = VECTORS["pr_link_bodies"]
    assert b64u_encode(body_key) == data["body_key"]
    assert data["body_bytes"] == context.PR_LINK_BODY_BYTES
    for body in data["bodies"]:
        binding = {key: body[key] for key in ("wg_id", "epoch", "tag", "task_id", "origin")}
        assert context.pr_link_body_aad(**binding).decode("ascii") == body["aad"]
        assert context.pr_link_body_text(body["repo"], body["number"]).decode("ascii") == body["plaintext"]
        nonce = b64u_decode(body["nonce"])
        sealed = context.seal_pr_link(body_key, **binding, repo=body["repo"], number=body["number"], nonce=nonce)
        assert sealed == body["sealed"]
        assert len(b64u_decode(sealed)) == context.PR_LINK_SEALED_BYTES
        opened = context.open_pr_link(body_key, link_key, **binding, sealed=sealed)
        assert opened == (body["repo"], body["number"])


def test_a_sealed_link_body_round_trips_with_a_random_nonce_and_keeps_the_spelling() -> None:
    body_key, link_key = context.pr_link_body_key(WK), context.pr_link_key(WK)
    sealed = context.seal_pr_link(body_key, **_binding(), repo="Org/Repo", number=3)
    assert sealed != context.seal_pr_link(body_key, **_binding(), repo="Org/Repo", number=3)
    assert context.open_pr_link(body_key, link_key, **_binding(), sealed=sealed) == ("Org/Repo", 3)
    device = _binding(origin=f"device:{ids()['session']}")
    assert context.open_pr_link(
        body_key, link_key, **device, sealed=context.seal_pr_link(body_key, **device, repo="Org/Repo", number=3)
    ) == ("Org/Repo", 3)
    assert body_key != context.pr_link_key(WK)


def test_sealed_link_body_refusals() -> None:
    body_key, link_key = context.pr_link_body_key(WK), context.pr_link_key(WK)
    sealed = context.seal_pr_link(body_key, **_binding(), repo="Org/Repo", number=3)
    # the binding of the AAD
    for change in (
        {"task_id": ids()["object"]},
        {"wg_id": ids()["org"]},
        {"epoch": 5},
        {"origin": f"device:{ids()['author']}"},
        {"origin": f"user:{ids()['session']}"},
    ):
        assert code(lambda c=change: context.open_pr_link(body_key, link_key, **_binding(**c), sealed=sealed)) == (
            "decrypt_failed"
        ), change
    # the formats of the AAD parts
    for change in (
        {"origin": 7},
        {"origin": "robot:" + ids()["author"]},
        {"origin": "user:not-an-id"},
        {"epoch": 0},
        {"tag": "AAAA"},
        {"wg_id": "x"},
    ):
        assert code(lambda c=change: context.pr_link_body_aad(**_binding(**c))) == "header_invalid", change
    # the sealed text, the keys and the plaintext
    assert code(lambda: context.open_pr_link(body_key, link_key, **_binding(), sealed=sealed[:-2])) == "header_invalid"
    assert code(lambda: context.open_pr_link(body_key, link_key, **_binding(), sealed=None)) == "header_invalid"
    assert code(lambda: context.open_pr_link(WK[:5], link_key, **_binding(), sealed=sealed)) == "header_invalid"
    assert code(lambda: context.seal_pr_link(WK[:5], **_binding(), repo="a/b", number=1)) == "header_invalid"
    assert code(lambda: context.seal_pr_link(body_key, **_binding(), repo="a", number=1)) == "header_invalid"
    assert code(lambda: context.seal_pr_link(body_key, **_binding(), repo="a/b", number=0)) == "header_invalid"
    assert code(lambda: context.pr_link_body_key(WK[:31])) == "header_invalid"
    # a body that names another pull request than the tag, under the right binding
    other = context.seal_pr_link(body_key, **_binding(), repo="org/repo", number=4)
    assert code(lambda: context.open_pr_link(body_key, link_key, **_binding(), sealed=other)) == "header_invalid"
    # a link key of another workgroup recomputes another tag
    foreign = context.pr_link_key(WK[::-1])
    assert code(lambda: context.open_pr_link(body_key, foreign, **_binding(), sealed=sealed)) == "header_invalid"
    # a body with another key set does not open as a link
    aad = context.pr_link_body_aad(**_binding())
    text = b'{"number":3,"repo":"Org/Repo","x":1}'
    blob = b64u_encode(aead.seal_blob(body_key, text + bytes(context.PR_LINK_BODY_BYTES - len(text)), aad))
    assert code(lambda: context.open_pr_link(body_key, link_key, **_binding(), sealed=blob)) == "header_invalid"
