"""Strict base64url without padding ("b64u", docs/platform/spec/04-crypto.md section 1)."""

from __future__ import annotations

import base64
import re

_B64U_RE = re.compile(r"^[A-Za-z0-9_-]*$")


def b64u_encode(data: bytes) -> str:
    """Encode bytes as base64url without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64u_decode(text: str) -> bytes:
    """Decode canonical base64url without padding; padding and non-canonical encodings raise ValueError."""
    if not _B64U_RE.fullmatch(text) or len(text) % 4 == 1:
        raise ValueError("not base64url without padding")
    # The alphabet and length are checked above, so decoding cannot fail.
    data = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    if b64u_encode(data) != text:
        raise ValueError("non-canonical base64url")
    return data
