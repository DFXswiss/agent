"""Copied from src/agent_hub/crypto/credential.py.

Credential records (04-crypto.md section 4.4, new).

Header key set exactly `HEADER_KEYS`; `ct = AES-GCM(k_cred, body, AAD = canonical(header))` with
`k_cred = HKDF(CK_e, salt = header.salt, info = P || "credential")`; `sig = Ed25519_author(P || "credential-sig\\0" ||
canonical(header) || ct_raw)`. The body schemas live in `contracts/credentials/`; this module treats the body as
opaque body JSON.
"""

from __future__ import annotations

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.crypto import fields, signed
from agent_runner.crypto.encoding import require_int
from agent_runner.crypto.errors import fail
from agent_runner.crypto.kdf import LABEL_CREDENTIAL_SIG, SALT_BYTES, credential_key

VERSION = 1
ALG = "A256GCM-HKDF"
HEADER_KEYS = frozenset({"v", "alg", "id", "owner", "kind", "version", "epoch", "author", "ts", "salt", "nonce"})
KINDS = frozenset({"github", "anthropic_api_key", "claude_subscription", "mailbox"})
MAX_CT_BYTES = 1024 * 1024
"""No own limit in the specification; bounded by the signed request body limit of 03-api.md section 1."""


def check_header(value: object) -> dict[str, object]:
    """Exact key set, `v`, `alg`, `kind` from the closed list and value formats."""
    header = fields.header_dict(value, HEADER_KEYS)
    fields.literal(header["v"], VERSION)
    fields.literal(header["alg"], ALG)
    fields.uuid(header["id"])
    fields.uuid(header["owner"])
    if header["kind"] not in KINDS:
        fail("header_invalid")
    require_int(header["version"], 1)
    require_int(header["epoch"], 1)
    fields.uuid(header["author"])
    fields.time(header["ts"])
    fields.b64u_bytes(header["salt"], SALT_BYTES)
    fields.b64u_bytes(header["nonce"], 12)
    return header


def _max_ct(_header: dict[str, object]) -> int:
    return MAX_CT_BYTES


FORMAT = signed.Format(
    check_header=check_header, derive_key=credential_key, sig_label=LABEL_CREDENTIAL_SIG, max_ct=_max_ct
)


def new_header(
    *,
    id: str,
    owner: str,
    kind: str,
    version: int,
    epoch: int,
    author: str,
    ts: str,
) -> dict[str, object]:
    """The checked header fields without `salt` and `nonce` (`seal_credential` draws those)."""
    fields: dict[str, object] = {
        "v": VERSION,
        "alg": ALG,
        "id": id,
        "owner": owner,
        "kind": kind,
        "version": version,
        "epoch": epoch,
        "author": author,
        "ts": ts,
    }
    return signed.check_fields(FORMAT, fields)


def seal_credential(
    cred_key: bytes,
    fields: object,
    body: bytes,
    author: Ed25519PrivateKey,
    *,
    fixed_salt_nonce: tuple[bytes, bytes] | None = None,
) -> dict[str, object]:
    """Encrypt the body JSON under `CK_e` and sign as the author (owner or acting device); fresh salt and nonce."""
    return signed.seal(FORMAT, cred_key, fields, body, author, fixed_salt_nonce=fixed_salt_nonce)


def verify_credential(record: object, author_sig_public: bytes) -> dict[str, object]:
    """Structure, header, size and signature without any key; returns the checked header."""
    return signed.verify(FORMAT, record, author_sig_public)[0]


def open_credential(cred_key: bytes, record: object, author_sig_public: bytes) -> bytes:
    """`verify_credential`, then decrypt."""
    return signed.open_(FORMAT, cred_key, record, author_sig_public)
