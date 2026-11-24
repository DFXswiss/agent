"""U-HUB-crypto (logs): the logs of test runs and work VM jobs of `agent_hub.crypto.runlog` (04-crypto.md section 4.12,
ADR-079 section 3) and their vectors `contracts/vectors/runlog.json`."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agent_hub.contexts import vectors as context_vectors
from agent_hub.crypto import context, runlog
from agent_hub.crypto.encoding import b64u_decode
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.keys import Identity
from agent_hub.crypto.vectors import render

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
VECTORS = json.loads((CONTRACTS / "vectors" / "runlog.json").read_text(encoding="utf-8"))
WK = bytes(range(32))
SALT = bytes(32)
TS = "2026-11-24T12:00:00.000Z"
REF = "tr-00000000000a"


def ids() -> dict[str, str]:
    return {
        "object": "01930043-0000-7000-8000-000000000001",
        "org": "01930043-0000-7000-8000-000000000002",
        "wg": "01930043-0000-7000-8000-000000000003",
        "task": "01930043-0000-7000-8000-000000000004",
        "session": "01930043-0000-7000-8000-000000000005",
        "author": "01930043-0000-7000-8000-000000000006",
    }


def code(call: Any) -> str:
    with pytest.raises(CryptoError) as refused:
        call()
    return refused.value.code


def test_the_vector_file_is_current_and_its_negatives_fail_as_stated() -> None:
    section = next(item for item in context_vectors.SECTIONS if item.name == "runlog")
    assert render(section) == (CONTRACTS / "vectors" / "runlog.json").read_text(encoding="utf-8")
    for vector in VECTORS["invalid"]:
        assert code(lambda v=vector: context_vectors.LOG_OPS[v["op"]](v["args"])) == vector["error"], vector["name"]


def test_the_vector_generator_refuses_a_wrong_or_missing_error() -> None:
    args = {"manifest": VECTORS["manifest"], "sig": VECTORS["sig"], "author_sig_pk": VECTORS["author_sig_pk"]}
    with pytest.raises(RuntimeError, match="instead of non_ascii"):
        context_vectors.log_negative("x", "verify_log_manifest", {**args, "sig": "AAAA"}, "non_ascii")
    with pytest.raises(RuntimeError, match="no error"):
        context_vectors.log_negative("x", "verify_log_manifest", args, "header_invalid")


def test_the_positive_vector_opens() -> None:
    chunks = [b64u_decode(item) for item in VECTORS["chunks"]]
    opened = runlog.open_log(
        b64u_decode(VECTORS["workgroup_key"]),
        VECTORS["manifest"],
        VECTORS["sig"],
        b64u_decode(VECTORS["author_sig_pk"]),
        chunks,
    )
    assert opened == b64u_decode(VECTORS["output"])
    part = VECTORS["part"]
    key = b64u_decode(VECTORS["k_log"])
    assert runlog.open_chunk(key, VECTORS["manifest"]["object"], part["index"], b64u_decode(part["sealed"])) == (
        b64u_decode(part["plaintext"])
    )


def test_a_long_output_is_split_sealed_and_joined_again() -> None:
    device = Identity.generate()
    output = os.urandom(2 * runlog.CHUNK_BYTES + 5)
    manifest, sig, sealed = runlog.seal_log(
        WK, ids(), ref=REF, epoch=1, ts=TS, output=output, total=10 * len(output), author=device.sig, salt=SALT
    )
    assert [len(item) for item in sealed] == [runlog.CHUNK_CT_BYTES, runlog.CHUNK_CT_BYTES, 5 + 28]
    assert (manifest["chunks"], manifest["stored"], manifest["bytes"]) == (3, len(output), 10 * len(output))
    assert runlog.open_log(WK, manifest, sig, device.sig_public(), sealed) == output
    assert code(lambda: runlog.open_log(WK, manifest, sig, device.sig_public(), sealed[::-1])) == "header_invalid"
    big = [*sealed[:2], sealed[2] + bytes(runlog.CHUNK_CT_BYTES)]
    assert code(lambda: runlog.check_chunks(manifest, big)) == "size_exceeded"


def test_sealing_refusals() -> None:
    device = Identity.generate()
    assert code(lambda: runlog.split(b"")) == "size_exceeded"
    assert (
        code(
            lambda: runlog.seal_log(
                WK, ids(), ref=REF, epoch=1, ts=TS, output=b"x", total=1, author=device.sig, salt=SALT, nonces=[]
            )
        )
        == "header_invalid"
    )
    key = runlog.log_key(WK, SALT, ids()["object"])
    assert code(lambda: runlog.seal_chunk(key, ids()["object"], 0, b"")) == "size_exceeded"
    assert code(lambda: runlog.seal_chunk(key, ids()["object"], 0, bytes(runlog.CHUNK_BYTES + 1))) == "size_exceeded"
    assert code(lambda: runlog.open_chunk(key, ids()["object"], 0, bytes(runlog.CHUNK_CT_BYTES + 1))) == (
        "size_exceeded"
    )
    assert code(lambda: runlog.chunk_aad(ids()["object"], runlog.MAX_CHUNKS)) == "header_invalid"
    assert code(lambda: runlog.log_key(WK[:31], SALT, ids()["object"])) == "header_invalid"


def test_manifest_signature_format_and_kind() -> None:
    device = Identity.generate()
    manifest, sig, sealed = runlog.seal_log(
        WK, ids(), ref="j-0123456789ab", epoch=1, ts=TS, output=b"abc", total=3, author=device.sig, salt=SALT
    )
    assert runlog.verify_manifest(manifest, sig, device.sig_public()) == manifest
    assert code(lambda: runlog.verify_manifest(manifest, sig, Identity.generate().sig_public())) == "signature_invalid"
    for change in ({"ref": 7}, {"ref": "tr-1"}, {"kind": "context"}, {"stored": 0}, {"bytes": 2}, {"epoch": 0}):
        assert code(lambda c=change: runlog.check_manifest({**manifest, **c})) == "header_invalid", change
    assert runlog.manifest_text(manifest).startswith(b"agent.dfx.swiss/v1/run-log\x00{")
    # a log never passes for a context checkpoint, nor its chunk for a checkpoint chunk
    assert code(lambda: context.verify_manifest(manifest, sig, device.sig_public())) == "header_invalid"
    context_key = context.context_key(WK, SALT, ids()["object"])
    assert code(lambda: context.open_chunk(context_key, ids()["object"], 0, sealed[0])) == "decrypt_failed"
