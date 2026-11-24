"""Vectors of the context checkpoints and pull-request links (`contracts/vectors/context.json`, 04-crypto.md
sections 3 and 4.11, ADR-059), discovered by the generator of `agent_hub.crypto.vectors` through `SECTIONS`.

Positive part: one checkpoint of one chunk (workgroup key, salt, `k_ctx`, the compressed archive, the sealed chunk,
manifest and signature), three standalone chunks of one object at the indexes 0-2 with their AAD and the digest over
them, and pull-request tags. Negative vectors name the operations of `OPS`; they are executed while generating and
must fail with exactly the stated code, as in `agent_hub.crypto.vectors.negative`.
"""

from __future__ import annotations

import gzip
from collections.abc import Callable
from typing import Any

from agent_hub.crypto import context
from agent_hub.crypto.encoding import b64u_decode, b64u_encode
from agent_hub.crypto.errors import CryptoError
from agent_hub.crypto.keys import Identity
from agent_hub.crypto.vectors import Section, fixed, fixed_id

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
        "about": "Context checkpoints and pull-request links of 04-crypto.md section 4.11 (ADR-059): k_ctx from WK "
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


SECTIONS = (Section("context", build),)
