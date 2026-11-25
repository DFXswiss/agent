"""Copied from src/agent_hub/crypto/browserframe.py.

Live frames of a platform browser session (04-crypto.md section 4.13, ADR-085 section 6).

A live frame is one JPEG picture of the page a session's platform browser shows. It is sent only while someone
watches and never stored beyond the latest frame per session. It is sealed under a key of the session's workgroup and
carries no signature (only the session's device can post one, and the hub cannot make or change one without `WK_e`):

- `k_frame = HKDF(ikm=WK_e, salt=salt (32 B random per frame), info=P || "browser-frame\\0" || browser_id)`,
- `ct = b64u(nonce(12) || AES-256-GCM(k_frame, jpeg, AAD))`, `AAD = P || "browser-frame\\0" ||
  canonical({"browser", "epoch", "seq", "session", "ts", "wg"})`,
- frame (JSON, clear except `ct`) with exactly the keys `FRAME_KEYS`; `ct` is `FRAME_CT_MIN` to `FRAME_CT_MAX` bytes
  decoded.

The label is its own, so a frame never passes for an event, a log chunk or a checkpoint chunk. Check order
(`check_frame` for the hub, which has no key; `open_frame` for a viewer): key set and canonical JSON (`header_invalid`,
`non_ascii`), the formats including a `ct` shorter than `FRAME_CT_MIN` (`header_invalid`), that `session` and `wg` are
the expected ones (`header_invalid`), the size of `ct` (`size_exceeded`), decryption (`decrypt_failed`). A viewer also
drops a frame whose `seq` is not above the last one it showed of that browser session; that is state, not format, and
stays with the viewer.
"""

from __future__ import annotations

import os
import re
from typing import cast

from agent_runner.crypto import aead, fields
from agent_runner.crypto.encoding import P, b64u_decode, b64u_encode, canonical, require_bytes, require_int
from agent_runner.crypto.errors import fail
from agent_runner.crypto.kdf import KEY_BYTES, SALT_BYTES, hkdf

VERSION = 1
LABEL = b"browser-frame\x00"
"""Info label of `k_frame` and AAD label of the ciphertext (without the prefix P)."""
FRAME_BYTES = 512 * 1024
"""At most this many JPEG bytes per frame."""
FRAME_CT_MAX = FRAME_BYTES + aead.NONCE_BYTES + aead.TAG_BYTES
FRAME_CT_MIN = aead.NONCE_BYTES + aead.TAG_BYTES + 1
MAX_EPOCH = 2**31 - 1
MAX_SEQ = 2**53 - 1
FRAME_KEYS = frozenset({"v", "browser", "session", "wg", "epoch", "seq", "ts", "salt", "ct"})
BROWSER_RE = re.compile(r"br-[0-9a-f]{12}")


def browser_id(value: object) -> str:
    """A browser session ID `br-<12 hex>` (`header_invalid` otherwise)."""
    if not isinstance(value, str) or BROWSER_RE.fullmatch(value) is None:
        fail("header_invalid")
    return value


def frame_key(workgroup_key: bytes, salt: bytes, browser: str) -> bytes:
    """`k_frame = HKDF(WK_e, salt, info=P || "browser-frame\\0" || browser_id)`."""
    require_bytes(workgroup_key, KEY_BYTES)
    info = P + LABEL + browser_id(browser).encode("ascii")
    return hkdf(workgroup_key, info, salt=require_bytes(salt, SALT_BYTES))


def frame_aad(*, browser: str, session: str, wg: str, epoch: int, seq: int, ts: str) -> bytes:
    """`P || "browser-frame\\0" || canonical({"browser", "epoch", "seq", "session", "ts", "wg"})`."""
    return (
        P + LABEL + canonical({"browser": browser, "epoch": epoch, "seq": seq, "session": session, "ts": ts, "wg": wg})
    )


def check_frame(frame: object, *, session: str | None = None, wg: str | None = None) -> dict[str, object]:
    """Key set, canonical JSON, formats, the expected `session` and `wg` when given, and the size of `ct`; returns
    the frame. Needs no key: the hub checks a posted frame with it."""
    checked = fields.header_dict(frame, FRAME_KEYS)
    fields.literal(checked["v"], VERSION)
    browser_id(checked["browser"])
    fields.uuid(checked["session"])
    fields.uuid(checked["wg"])
    require_int(checked["epoch"], 1, MAX_EPOCH)
    require_int(checked["seq"], 0, MAX_SEQ)
    fields.time(checked["ts"])
    fields.b64u_bytes(checked["salt"], SALT_BYTES)
    size = len(b64u_decode(checked["ct"]))
    if size < FRAME_CT_MIN:
        fail("header_invalid")
    if (session is not None and checked["session"] != session) or (wg is not None and checked["wg"] != wg):
        fail("header_invalid")
    if size > FRAME_CT_MAX:
        fail("size_exceeded")
    return checked


def seal_frame(
    workgroup_key: bytes,
    *,
    browser: str,
    session: str,
    wg: str,
    epoch: int,
    seq: int,
    ts: str,
    jpeg: bytes,
    salt: bytes | None = None,
    nonce: bytes | None = None,
) -> dict[str, object]:
    """The frame of one picture; an empty or too large picture is `size_exceeded`, a malformed field
    `header_invalid`. `salt` (32 B) and `nonce` (12 B) are random unless given (test vectors only)."""
    if not 1 <= len(jpeg) <= FRAME_BYTES:
        fail("size_exceeded")
    used_salt = os.urandom(SALT_BYTES) if salt is None else salt
    key = frame_key(workgroup_key, used_salt, browser)
    aad = frame_aad(browser=browser, session=session, wg=wg, epoch=epoch, seq=seq, ts=ts)
    frame = {
        "v": VERSION,
        "browser": browser,
        "session": session,
        "wg": wg,
        "epoch": epoch,
        "seq": seq,
        "ts": ts,
        "salt": b64u_encode(used_salt),
        "ct": b64u_encode(aead.seal_blob(key, jpeg, aad, nonce)),
    }
    return check_frame(frame)


def open_frame(workgroup_key: bytes, frame: object, *, session: str, wg: str) -> bytes:
    """The JPEG bytes of a frame of the watched `session` in workgroup `wg` (check order in the module docstring);
    `workgroup_key` is `WK_e` of the frame's epoch."""
    checked = check_frame(frame, session=session, wg=wg)
    browser = str(checked["browser"])
    key = frame_key(workgroup_key, b64u_decode(checked["salt"]), browser)
    aad = frame_aad(
        browser=browser,
        session=session,
        wg=wg,
        epoch=cast(int, checked["epoch"]),
        seq=cast(int, checked["seq"]),
        ts=cast(str, checked["ts"]),
    )
    return aead.open_blob(key, b64u_decode(checked["ct"]), aad)
