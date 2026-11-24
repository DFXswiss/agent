"""The community test vectors `contracts/community/vectors.json` (14-community.md section 3, ADR-069).

The community is a browser/hub format: runners and the VMs never take part (ADR-069 decision 9), so the vectors live
beside the community contracts and not in `contracts/vectors/` (which every runner section must regenerate, CON-02).
`test_vectors.py` keeps the file current; the web core checks its TypeScript implementation against it.

Regenerate: `.venv/bin/python -m tests.platform.hub.community.vectors`.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_hub.community import crypto as fmt
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.crypto.keys import ed25519_private, ed25519_public_raw, x25519_private, x25519_public_raw

PATH = Path(__file__).resolve().parents[4] / "contracts" / "community" / "vectors.json"
USER = "01a1ee56-5e00-77be-8cf3-1f27bb0e92a2"
OTHER = "01a1ee56-5e00-7d3c-9a1b-2c3d4e5f6a7b"
REQUEST = "01a1ee56-5e00-7547-b84c-d0d997b866f3"
MESSAGE = "01a1ee56-5e00-7e11-8f00-0123456789ab"
TS = "2026-11-23T12:00:00.000Z"


def _seed(label: str) -> bytes:
    return hashlib.sha256(b"agent.dfx.swiss/vectors/community/" + label.encode("ascii")).digest()


def build() -> dict[str, Any]:
    """Every value of the file, deterministically."""
    key = _seed("community-key")
    author = ed25519_private(_seed("author-sig"))
    author_kem = x25519_private(_seed("author-kem"))
    wrapper = ed25519_private(_seed("wrapper-sig"))
    request_kem = x25519_private(_seed("request-kem"))
    ephemeral = x25519_private(_seed("ephemeral"))
    request_kem_pub = b64u_encode(x25519_public_raw(request_kem))
    request_sig = fmt.sign_request(author, id=REQUEST, kem_pub=request_kem_pub, user=USER)
    request_wrap = fmt.wrap_community_key(
        x25519_public_raw(request_kem), epoch=1, recipient=REQUEST, key=key, ephemeral=ephemeral
    )
    self_wrap = fmt.wrap_community_key(
        x25519_public_raw(author_kem), epoch=1, recipient=USER, key=key, ephemeral=ephemeral
    )
    name, text = "Zoë Müller", "Grüezi mitenand!\nSiehe https://example.org/a?b=1 - danke."
    body = fmt.body_bytes(name, text)
    header = fmt.new_header(id=MESSAGE, author=USER, epoch=1, ts=TS)
    message: dict[str, Any] = fmt.seal_message(
        key, header, body, author, fixed_salt_nonce=(_seed("salt"), _seed("nonce")[:12])
    )
    tampered = {**message, "ct": message["ct"][:-2] + ("AA" if message["ct"][-2:] != "AA" else "BA")}
    other_author = {**message, "header": {**message["header"], "author": OTHER}}
    return {
        "about": "Community key check, key request, wraps and one message (14-community.md section 3, ADR-069).",
        "community_key": b64u_encode(key),
        "epoch": 1,
        "key_check": b64u_encode(fmt.key_check(key, 1)),
        "user": USER,
        "author_sig_pk": b64u_encode(ed25519_public_raw(author)),
        "author_kem_sk": b64u_encode(_seed("author-kem")),
        "request": {
            "id": REQUEST,
            "kem_sk": b64u_encode(_seed("request-kem")),
            "kem_pub": request_kem_pub,
            "sig": request_sig,
            "text": b64u_encode(fmt.request_text(id=REQUEST, kem_pub=request_kem_pub, user=USER)),
        },
        "request_wrap": {
            "recipient": REQUEST,
            "wrap": request_wrap,
            "wrapper_sig_pk": b64u_encode(ed25519_public_raw(wrapper)),
            "sig": fmt.sign_community_wrap(wrapper, epoch=1, recipient=REQUEST, wrap_text=request_wrap),
        },
        "self_wrap": {
            "recipient": USER,
            "wrap": self_wrap,
            "sig": fmt.sign_community_wrap(author, epoch=1, recipient=USER, wrap_text=self_wrap),
        },
        "message": {
            "body_json": body.decode("utf-8"),
            "body": {"name": name, "text": text, "v": 1},
            "sealed": message,
        },
        "invalid_messages": [
            {"name": "tampered_ct", "sealed": tampered, "error": "signature_invalid"},
            {"name": "other_author", "sealed": other_author, "error": "signature_invalid"},
        ],
        "invalid_bodies": [
            {"name": "bidi_in_name", "body": {"name": "Anna\u202e", "text": "x", "v": 1}},
            {"name": "zero_width_in_name", "body": {"name": "An\u200bna", "text": "x", "v": 1}},
            {"name": "not_nfc", "body": {"name": "Zoe\u0308", "text": "x", "v": 1}},
            {"name": "control_in_text", "body": {"name": "Anna", "text": "a\u0007b", "v": 1}},
            {"name": "bidi_in_text", "body": {"name": "Anna", "text": "a\u2066b", "v": 1}},
            {"name": "padded_name", "body": {"name": "Anna ", "text": "x", "v": 1}},
            {"name": "empty_text", "body": {"name": "Anna", "text": "", "v": 1}},
            {"name": "long_text", "body": {"name": "Anna", "text": "x" * 2001, "v": 1}},
            {"name": "long_name", "body": {"name": "A" * 65, "text": "x", "v": 1}},
            {"name": "extra_key", "body": {"name": "Anna", "text": "x", "v": 1, "html": "<b>"}},
            {"name": "version", "body": {"name": "Anna", "text": "x", "v": 2}},
        ],
    }


def text() -> str:
    """The file content."""
    return json.dumps(build(), indent=2, sort_keys=True, ensure_ascii=True) + "\n"


def main() -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    PATH.write_text(text(), encoding="ascii")


if __name__ == "__main__":
    main()
