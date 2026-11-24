"""Copied from agent-core src/agent_hub/crypto/event.py at 3d02f01a237c.

Events (04-crypto.md section 4.3, checks E1, E2, E4 and E8 of section 6.4).

Header key set exactly `HEADER_KEYS` **(changed** against Spike D: `seq` and `device` removed, `id`, `scope_id`, `ref`
and `lh` added). `ct = AES-GCM(k_event, plaintext, AAD = canonical(header))` with
`k_event = HKDF(scope_key, salt = header.salt, info = P || "event")`; `sig = Ed25519_author(P || "event-sig\\0" ||
canonical(header) || ct_raw)`. Kind/scope/author rules, rights and epochs (E3, E5-E7, E9) are in `agent_hub.log`.
"""

from __future__ import annotations

import re

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.crypto import fields, signed
from agent_runner.crypto.encoding import require_int
from agent_runner.crypto.errors import fail
from agent_runner.crypto.kdf import LABEL_EVENT_SIG, SALT_BYTES, event_key

VERSION = 1
ALG = "A256GCM-HKDF"
HEADER_KEYS = frozenset(
    {
        "v",
        "alg",
        "id",
        "org",
        "scope",
        "scope_id",
        "epoch",
        "wg",
        "task",
        "session",
        "author",
        "kind",
        "ref",
        "ts",
        "lh",
        "salt",
        "nonce",
    }
)
SCOPES = frozenset({"org", "wg"})
MAX_CT_BYTES = 256 * 1024
"""E8: `ct` at most 256 KiB decoded (ciphertext including the 16-byte tag)."""
MAX_TERMINAL_CT_BYTES = 64 * 1024
"""E8: terminal chunks (`session.terminal`) at most 64 KiB decoded."""
TERMINAL_KIND = "session.terminal"
WG_PROFILE_KIND = "wg.profile"
"""The only org-scope kind whose header may name a workgroup in `wg` (section 4.3)."""
_REF_RE = re.compile(r"[A-Za-z0-9._:/-]{1,200}")


def check_header(value: object) -> dict[str, object]:
    """E1/E2: exact key set, `v == 1`, `alg`, value formats and the scope/scope_id/wg relation (an org-scope header
    names a workgroup only for `wg.profile`)."""
    header = fields.header_dict(value, HEADER_KEYS)
    fields.literal(header["v"], VERSION)
    fields.literal(header["alg"], ALG)
    fields.uuid(header["id"])
    org = fields.uuid(header["org"])
    scope_id = fields.uuid(header["scope_id"])
    wg = fields.uuid_or_empty(header["wg"])
    fields.uuid_or_empty(header["task"])
    fields.uuid_or_empty(header["session"])
    fields.uuid(header["author"])
    fields.kind(header["kind"])
    if header["ref"] != "" and not (isinstance(header["ref"], str) and _REF_RE.fullmatch(header["ref"])):
        fail("header_invalid")
    fields.time(header["ts"])
    require_int(header["epoch"], 1)
    require_int(header["lh"], 0)
    fields.b64u_bytes(header["salt"], SALT_BYTES)
    fields.b64u_bytes(header["nonce"], 12)
    if header["scope"] not in SCOPES or scope_id != (wg if header["scope"] == "wg" else org):
        fail("header_invalid")
    if header["scope"] == "org" and wg != "" and header["kind"] != WG_PROFILE_KIND:
        fail("header_invalid")
    return header


def max_ct_bytes(header: dict[str, object]) -> int:
    """The E8 limit for this header's kind."""
    return MAX_TERMINAL_CT_BYTES if header["kind"] == TERMINAL_KIND else MAX_CT_BYTES


FORMAT = signed.Format(check_header=check_header, derive_key=event_key, sig_label=LABEL_EVENT_SIG, max_ct=max_ct_bytes)


def new_header(
    *,
    id: str,
    org: str,
    scope: str,
    scope_id: str,
    epoch: int,
    author: str,
    kind: str,
    ts: str,
    lh: int,
    wg: str = "",
    task: str = "",
    session: str = "",
    ref: str = "",
) -> dict[str, object]:
    """The checked header fields without `salt` and `nonce` (`seal_event` draws those)."""
    fields: dict[str, object] = {
        "v": VERSION,
        "alg": ALG,
        "id": id,
        "org": org,
        "scope": scope,
        "scope_id": scope_id,
        "epoch": epoch,
        "wg": wg,
        "task": task,
        "session": session,
        "author": author,
        "kind": kind,
        "ref": ref,
        "ts": ts,
        "lh": lh,
    }
    return signed.check_fields(FORMAT, fields)


def seal_event(
    scope_key: bytes,
    fields: object,
    plaintext: bytes,
    author: Ed25519PrivateKey,
    *,
    fixed_salt_nonce: tuple[bytes, bytes] | None = None,
) -> dict[str, object]:
    """Encrypt and sign an event (writer, section 5.12); the plaintext is the body JSON of the kind.

    `fields` is the header without `salt`/`nonce`; both are drawn fresh (fixed values only for test vectors).
    """
    return signed.seal(FORMAT, scope_key, fields, plaintext, author, fixed_salt_nonce=fixed_salt_nonce)


def verify_event(event: object, author_sig_public: bytes) -> dict[str, object]:
    """E1, E2, E4, E8 without any key (what the hub checks); returns the checked header."""
    return signed.verify(FORMAT, event, author_sig_public)[0]


def open_event(scope_key: bytes, event: object, author_sig_public: bytes) -> bytes:
    """`verify_event`, then decrypt with the scope key of the header's epoch."""
    return signed.open_(FORMAT, scope_key, event, author_sig_public)
