"""Copied from src/agent_hub/contexts/vectors.py.

Vectors of the context checkpoints and pull-request links (`contracts/vectors/context.json`, 04-crypto.md
sections 3 and 4.11, ADR-072) and of the logs (`contracts/vectors/runlog.json`, section 4.12, ADR-079, below),
discovered by the generator of `agent_hub.crypto.vectors` through `SECTIONS`.

Positive part of `context`: one checkpoint of one chunk (workgroup key, salt, `k_ctx`, the compressed archive, the
sealed chunk, manifest and signature), three standalone chunks of one object at the indexes 0-2 with their AAD and the
digest over them, and pull-request tags. Negative vectors name the operations of `OPS`; they are executed while
generating and must fail with exactly the stated code, as in `agent_hub.crypto.vectors.negative`.
"""

from __future__ import annotations

import gzip
from collections.abc import Callable
from typing import Any

from agent_runner.crypto import context, runlog
from agent_runner.crypto.encoding import b64u_decode, b64u_encode
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.keys import Identity
from agent_runner.crypto.vectors import Section, fixed, fixed_id

Json = dict[str, Any]
TS = "2026-11-23T12:00:00.000Z"
IDS = {
    "object": fixed_id("context/object"),
    "org": fixed_id("context/org"),
    "wg": fixed_id("context/wg"),
    "task": fixed_id("context/task"),
    "session": fixed_id("context/session"),
    "author": fixed_id("context/device"),
}
ARCHIVE = b"context.json\x00" + "Kontext der Sitzung: früher gesagte Dinge.".encode()


def _b(value: object) -> bytes:
    return b64u_decode(value)


OPS: dict[str, Callable[[Json], object]] = {
    "verify_context_manifest": lambda a: context.verify_manifest(a["manifest"], a["sig"], _b(a["author_sig_pk"])),
    "open_context": lambda a: context.open_context(
        _b(a["workgroup_key"]), a["manifest"], a["sig"], _b(a["author_sig_pk"]), [_b(c) for c in a["chunks"]]
    ),
    "open_context_chunk": lambda a: context.open_chunk(_b(a["key"]), a["object"], a["index"], _b(a["sealed"])),
    "pr_link_tag": lambda a: context.pr_link_tag(context.pr_link_key(_b(a["workgroup_key"])), a["repo"], a["number"]),
}


def negative(name: str, op: str, args: Json, error: str) -> Json:
    """A negative vector; `op(args)` must fail with exactly `error`."""
    try:
        OPS[op](args)
    except CryptoError as refused:
        if refused.code == error:
            return {"name": name, "op": op, "args": args, "error": error}
        raise RuntimeError(f"vector {name}: {refused.code} instead of {error}") from None
    raise RuntimeError(f"vector {name}: no error instead of {error}")


def _device() -> Identity:
    return Identity.from_raw(fixed("context/device/kem"), fixed("context/device/sig"))


def _changed(manifest: Json, **changes: object) -> Json:
    return {**manifest, **changes}


def build() -> Json:
    """The section `context` (module docstring)."""
    wk, salt, device = fixed("context/wg-key"), fixed("context/salt"), _device()
    pk = b64u_encode(device.sig_public())
    compressed = gzip.compress(ARCHIVE, compresslevel=6, mtime=0)
    nonce = fixed("context/nonce/0", 12)
    manifest, sig, sealed = context.seal_context(
        wk, IDS, epoch=2, ts=TS, compressed=compressed, author=device.sig, salt=salt, nonces=[nonce]
    )
    key = context.context_key(wk, salt, IDS["object"])
    parts = [b"erster Teil \xc3\xa4", b"second part", b"\x00\x01\x02"]
    chunks = [
        {
            "index": index,
            "plaintext": b64u_encode(part),
            "nonce": b64u_encode(fixed(f"context/nonce/part/{index}", 12)),
            "aad": context.chunk_aad(IDS["object"], index).decode("ascii"),
            "sealed": b64u_encode(
                context.seal_chunk(key, IDS["object"], index, part, fixed(f"context/nonce/part/{index}", 12))
            ),
        }
        for index, part in enumerate(parts)
    ]
    chunk_bytes = [_b(item["sealed"]) for item in chunks]
    link_key = context.pr_link_key(wk)
    links = [
        {"repo": repo, "number": number, "text": context.pr_link_text(repo, number).decode("ascii")}
        for repo, number in (("Example-Org/Example-Repo", 17), ("example-org/example-repo", 17), ("a/b.c", 1))
    ]
    for link in links:
        link["tag"] = context.pr_link_tag(link_key, link["repo"], link["number"])
    one = b64u_encode(sealed[0])
    base = {"manifest": manifest, "sig": sig, "author_sig_pk": pk}
    return {
        "about": "Context checkpoints and pull-request links of 04-crypto.md section 4.11 (ADR-072): k_ctx from WK "
        "and the manifest salt, sealed chunks, the signed manifest, the digest and pull-request tags.",
        "workgroup_key": b64u_encode(wk),
        "author_sig_pk": pk,
        "salt": b64u_encode(salt),
        "k_ctx": b64u_encode(key),
        "compressed": b64u_encode(compressed),
        "nonces": [b64u_encode(nonce)],
        "manifest": manifest,
        "manifest_text": context.manifest_text(manifest).decode("ascii"),
        "sig": sig,
        "chunks": [one],
        "parts": {"chunks": chunks, "digest": context.digest(chunk_bytes)},
        "pr_links": {"link_key": b64u_encode(link_key), "links": links},
        "limits": {"chunk_bytes": context.CHUNK_BYTES, "chunk_ct_bytes": context.CHUNK_CT_BYTES},
        "invalid": [
            negative(
                "manifest_extra_key",
                "verify_context_manifest",
                {**base, "manifest": _changed(manifest, lh=1)},
                "header_invalid",
            ),
            negative(
                "manifest_non_ascii",
                "verify_context_manifest",
                {**base, "manifest": _changed(manifest, ts="2026-11-23T12:00:00.000Zé")},
                "non_ascii",
            ),
            negative(
                "manifest_version",
                "verify_context_manifest",
                {**base, "manifest": _changed(manifest, v=2)},
                "header_invalid",
            ),
            negative(
                "manifest_no_chunks",
                "verify_context_manifest",
                {**base, "manifest": _changed(manifest, chunks=0)},
                "header_invalid",
            ),
            negative(
                "manifest_size_below_chunks",
                "verify_context_manifest",
                {**base, "manifest": _changed(manifest, size=28)},
                "header_invalid",
            ),
            negative(
                "manifest_other_task",
                "verify_context_manifest",
                {**base, "manifest": _changed(manifest, task=IDS["session"])},
                "signature_invalid",
            ),
            negative(
                "manifest_other_author_key",
                "verify_context_manifest",
                {**base, "author_sig_pk": b64u_encode(fixed("context/other"))},
                "signature_invalid",
            ),
            negative(
                "chunk_missing",
                "open_context",
                {**base, "workgroup_key": b64u_encode(wk), "chunks": []},
                "header_invalid",
            ),
            negative(
                "chunk_swapped",
                "open_context",
                {**base, "workgroup_key": b64u_encode(wk), "chunks": [chunks[0]["sealed"]]},
                "header_invalid",
            ),
            negative(
                "other_workgroup_key",
                "open_context",
                {**base, "workgroup_key": b64u_encode(fixed("context/wg-key-2")), "chunks": [one]},
                "decrypt_failed",
            ),
            negative(
                "chunk_other_index",
                "open_context_chunk",
                {"key": b64u_encode(key), "object": IDS["object"], "index": 1, "sealed": chunks[0]["sealed"]},
                "decrypt_failed",
            ),
            negative(
                "chunk_other_object",
                "open_context_chunk",
                {"key": b64u_encode(key), "object": IDS["task"], "index": 0, "sealed": chunks[0]["sealed"]},
                "decrypt_failed",
            ),
            negative(
                "chunk_too_short",
                "open_context_chunk",
                {"key": b64u_encode(key), "object": IDS["object"], "index": 0, "sealed": b64u_encode(bytes(27))},
                "decrypt_failed",
            ),
            negative(
                "pr_number_zero",
                "pr_link_tag",
                {"workgroup_key": b64u_encode(wk), "repo": "a/b", "number": 0},
                "header_invalid",
            ),
            negative(
                "pr_repo_without_owner",
                "pr_link_tag",
                {"workgroup_key": b64u_encode(wk), "repo": "example-repo", "number": 1},
                "header_invalid",
            ),
            negative(
                "pr_repo_non_ascii",
                "pr_link_tag",
                {"workgroup_key": b64u_encode(wk), "repo": "örg/repo", "number": 1},
                "non_ascii",
            ),
        ],
    }


# -- logs of test runs and work VM jobs (`contracts/vectors/runlog.json`, 04-crypto.md section 4.12, ADR-079) ---------
#
# Positive part: one log of two chunks (the first `CHUNK_BYTES` long) with workgroup key, salt, `k_log`, the kept
# output, the sealed chunks, manifest, signature and digest; one standalone chunk with its AAD. Negative vectors include
# a context manifest offered as a log and a log chunk opened with the context's labels (the kinds never pass for each
# other).

LOG_TS = "2026-11-24T12:00:00.000Z"
LOG_IDS = {
    "object": fixed_id("runlog/object"),
    "org": fixed_id("runlog/org"),
    "wg": fixed_id("runlog/wg"),
    "task": fixed_id("runlog/task"),
    "session": fixed_id("runlog/session"),
    "author": fixed_id("runlog/device"),
}
LOG_REF = "tr-0123456789ab"


LOG_OPS: dict[str, Callable[[Json], object]] = {
    "verify_log_manifest": lambda a: runlog.verify_manifest(a["manifest"], a["sig"], _b(a["author_sig_pk"])),
    "open_log": lambda a: runlog.open_log(
        _b(a["workgroup_key"]), a["manifest"], a["sig"], _b(a["author_sig_pk"]), [_b(c) for c in a["chunks"]]
    ),
    "open_log_chunk": lambda a: runlog.open_chunk(_b(a["key"]), a["object"], a["index"], _b(a["sealed"])),
    "open_context_chunk": lambda a: context.open_chunk(_b(a["key"]), a["object"], a["index"], _b(a["sealed"])),
}


def log_negative(name: str, op: str, args: Json, error: str) -> Json:
    """A negative vector; `op(args)` must fail with exactly `error`."""
    try:
        LOG_OPS[op](args)
    except CryptoError as refused:
        if refused.code == error:
            return {"name": name, "op": op, "args": args, "error": error}
        raise RuntimeError(f"vector {name}: {refused.code} instead of {error}") from None
    raise RuntimeError(f"vector {name}: no error instead of {error}")


def log_output() -> bytes:
    """A deterministic output one byte longer than a chunk: `CHUNK_BYTES` of text lines plus one byte."""
    line = "PASSED tests/test_calc.py::test_add - ok \u2713\n".encode()
    body = (line * (runlog.CHUNK_BYTES // len(line) + 1))[: runlog.CHUNK_BYTES]
    return body + b"!"


def build_runlog() -> Json:
    """The section `runlog` (module docstring)."""
    wk, salt = fixed("runlog/wg-key"), fixed("runlog/salt")
    device = Identity.from_raw(fixed("runlog/device/kem"), fixed("runlog/device/sig"))
    pk = b64u_encode(device.sig_public())
    kept = log_output()
    nonces = [fixed("runlog/nonce/0", 12), fixed("runlog/nonce/1", 12)]
    manifest, sig, sealed = runlog.seal_log(
        wk,
        LOG_IDS,
        ref=LOG_REF,
        epoch=3,
        ts=LOG_TS,
        output=kept,
        total=len(kept) + 100,
        author=device.sig,
        salt=salt,
        nonces=nonces,
    )
    key = runlog.log_key(wk, salt, LOG_IDS["object"])
    part = b"Tests: 3 failed, 12 passed\n"
    part_nonce = fixed("runlog/nonce/part", 12)
    one = runlog.seal_chunk(key, LOG_IDS["object"], 5, part, part_nonce)
    base = {"manifest": manifest, "sig": sig, "author_sig_pk": pk}
    ctx_key = context.context_key(wk, salt, LOG_IDS["object"])
    return {
        "about": "Logs of test runs and work VM jobs of 04-crypto.md section 4.12 (ADR-079): k_log from WK and the "
        "manifest salt, uncompressed sealed chunks, the signed manifest with kind and ref, the digest.",
        "workgroup_key": b64u_encode(wk),
        "author_sig_pk": pk,
        "salt": b64u_encode(salt),
        "k_log": b64u_encode(key),
        "output": b64u_encode(kept),
        "nonces": [b64u_encode(nonce) for nonce in nonces],
        "manifest": manifest,
        "manifest_text": runlog.manifest_text(manifest).decode("ascii"),
        "sig": sig,
        "chunks": [b64u_encode(chunk) for chunk in sealed],
        "part": {
            "index": 5,
            "plaintext": b64u_encode(part),
            "nonce": b64u_encode(part_nonce),
            "aad": runlog.chunk_aad(LOG_IDS["object"], 5).decode("ascii"),
            "sealed": b64u_encode(one),
        },
        "limits": {"chunk_bytes": runlog.CHUNK_BYTES, "chunk_ct_bytes": runlog.CHUNK_CT_BYTES},
        "invalid": [
            log_negative(
                "manifest_without_kind",
                "verify_log_manifest",
                {**base, "manifest": {k: v for k, v in manifest.items() if k != "kind"}},
                "header_invalid",
            ),
            log_negative(
                "manifest_other_kind",
                "verify_log_manifest",
                {**base, "manifest": {**manifest, "kind": "context"}},
                "header_invalid",
            ),
            log_negative(
                "manifest_bad_ref",
                "verify_log_manifest",
                {**base, "manifest": {**manifest, "ref": "run-1"}},
                "header_invalid",
            ),
            log_negative(
                "manifest_other_ref",
                "verify_log_manifest",
                {**base, "manifest": {**manifest, "ref": "tr-ba9876543210"}},
                "signature_invalid",
            ),
            log_negative(
                "manifest_stored_above_chunks",
                "verify_log_manifest",
                {**base, "manifest": {**manifest, "stored": 2 * runlog.CHUNK_BYTES + 1}},
                "header_invalid",
            ),
            log_negative(
                "manifest_bytes_below_stored",
                "verify_log_manifest",
                {**base, "manifest": {**manifest, "bytes": len(kept) - 1}},
                "header_invalid",
            ),
            log_negative(
                "context_manifest_as_log",
                "verify_log_manifest",
                {
                    **base,
                    "manifest": {k: v for k, v in manifest.items() if k not in ("kind", "ref", "bytes", "stored")},
                },
                "header_invalid",
            ),
            log_negative(
                "chunks_swapped",
                "open_log",
                {**base, "workgroup_key": b64u_encode(wk), "chunks": [b64u_encode(sealed[1]), b64u_encode(sealed[0])]},
                "header_invalid",
            ),
            log_negative(
                "other_workgroup_key",
                "open_log",
                {
                    **base,
                    "workgroup_key": b64u_encode(fixed("runlog/wg-key-2")),
                    "chunks": [b64u_encode(c) for c in sealed],
                },
                "decrypt_failed",
            ),
            log_negative(
                "chunk_other_index",
                "open_log_chunk",
                {"key": b64u_encode(key), "object": LOG_IDS["object"], "index": 4, "sealed": b64u_encode(one)},
                "decrypt_failed",
            ),
            log_negative(
                "log_chunk_as_context_chunk",
                "open_context_chunk",
                {"key": b64u_encode(ctx_key), "object": LOG_IDS["object"], "index": 5, "sealed": b64u_encode(one)},
                "decrypt_failed",
            ),
        ],
    }


SECTIONS = (Section("context", build), Section("runlog", build_runlog))
