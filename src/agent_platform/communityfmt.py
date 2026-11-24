"""The community formats of the agent tool (14-community.md section 3, ADR-069): the twin of the hub's reference
`agent_hub.community.crypto` on the runner's copy of the reference crypto (`agent_runner.crypto`), because the tool
must not import the hub (00-overview.md section 5).

Everything below this docstring equals the reference with `agent_hub.crypto` read as `agent_runner.crypto`
(`tests/platform/tool/test_community.py` compares the two files and runs the community vectors against this one).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import unicodedata

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_runner.crypto import fields, signed, wrap
from agent_runner.crypto.encoding import P, b64u_encode, canonical, require_bytes, require_int
from agent_runner.crypto.errors import fail
from agent_runner.crypto.kdf import KEY_BYTES, SALT_BYTES, hkdf
from agent_runner.crypto.keys import ed25519_sign, ed25519_verify_b64u

VERSION = 1
ALG = "A256GCM-HKDF"
HEADER_KEYS = frozenset({"v", "alg", "id", "author", "epoch", "ts", "salt", "nonce"})
BODY_KEYS = frozenset({"name", "text", "v"})
MAX_CT_BYTES = 16 * 1024
MAX_TEXT_CHARS = 2000
MAX_NAME_CHARS = 64
SCOPE = "community"
SCOPE_ID = "00000000-0000-0000-0000-000000000000"
INFO = b"community"
LABEL_SIG = b"community-sig\x00"
LABEL_CHECK = b"community-check\x00"
LABEL_REQUEST = b"community-request\x00"
NAME_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})
"""Unicode categories a display name must not contain (control, format incl. bidi and zero-width, separators)."""
TEXT_FORBIDDEN = frozenset(
    [*range(0x202A, 0x202F), *range(0x2066, 0x206A), *range(0x200B, 0x2010), 0x2028, 0x2029, 0xFEFF]
)
"""Bidi controls, zero-width characters, line/paragraph separators and the BOM, refused in a text."""
TEXT_CONTROLS_ALLOWED = frozenset({0x0A, 0x09})


def key_check(community_key: bytes, epoch: int) -> bytes:
    """`HMAC-SHA256(CMK_e, P||"community-check\0"||canonical({"epoch":e}))`, 32 bytes."""
    require_bytes(community_key, KEY_BYTES)
    text = P + LABEL_CHECK + canonical({"epoch": require_int(epoch, 1)})
    return hmac.new(community_key, text, hashlib.sha256).digest()


def request_text(*, id: str, kem_pub: str, user: str) -> bytes:
    """`P||"community-request\0"||canonical({"id","kem_pub","user"})`, signed by the requesting user's identity."""
    fields.uuid(id)
    fields.uuid(user)
    fields.b64u_bytes(kem_pub, 32)
    return P + LABEL_REQUEST + canonical({"id": id, "kem_pub": kem_pub, "user": user})


def sign_request(identity: Ed25519PrivateKey, *, id: str, kem_pub: str, user: str) -> str:
    """The user's signature of a key request."""
    return b64u_encode(ed25519_sign(identity, request_text(id=id, kem_pub=kem_pub, user=user)))


def verify_request(identity_sig_public: bytes, *, id: str, kem_pub: str, user: str, signature: str) -> None:
    """Check the signature of a key request (`signature_invalid`, `header_invalid`)."""
    ed25519_verify_b64u(identity_sig_public, signature, request_text(id=id, kem_pub=kem_pub, user=user))


def message_key(community_key: bytes, salt: bytes) -> bytes:
    """`k_msg = HKDF(CMK_e, salt=header.salt, info=P||"community")`."""
    require_bytes(community_key, KEY_BYTES)
    return hkdf(community_key, P + INFO, salt=require_bytes(salt, SALT_BYTES))


def check_header(value: object) -> dict[str, object]:
    """Exact key set, `v`, `alg` and value formats (`header_invalid`)."""
    header = fields.header_dict(value, HEADER_KEYS)
    fields.literal(header["v"], VERSION)
    fields.literal(header["alg"], ALG)
    fields.uuid(header["id"])
    fields.uuid(header["author"])
    require_int(header["epoch"], 1)
    fields.time(header["ts"])
    fields.b64u_bytes(header["salt"], SALT_BYTES)
    fields.b64u_bytes(header["nonce"], 12)
    return header


def _max_ct(_header: dict[str, object]) -> int:
    return MAX_CT_BYTES


FORMAT = signed.Format(check_header=check_header, derive_key=message_key, sig_label=LABEL_SIG, max_ct=_max_ct)


def new_header(*, id: str, author: str, epoch: int, ts: str) -> dict[str, object]:
    """The checked header fields without `salt` and `nonce` (`seal_message` draws those)."""
    return signed.check_fields(FORMAT, {"v": VERSION, "alg": ALG, "id": id, "author": author, "epoch": epoch, "ts": ts})


def _trimmed(value: object, limit: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not 1 <= len(value) <= limit:
        fail("header_invalid")
    return value


def check_name(value: object) -> str:
    """A display name: 1-64 code points, trimmed, NFC, none of the categories `NAME_CATEGORIES`."""
    name = _trimmed(value, MAX_NAME_CHARS)
    if unicodedata.normalize("NFC", name) != name or any(unicodedata.category(c) in NAME_CATEGORIES for c in name):
        fail("header_invalid")
    return name


def check_text(value: object) -> str:
    """A message text: 1-2000 code points, trimmed, no control but line feed and tab, none of `TEXT_FORBIDDEN`."""
    text = _trimmed(value, MAX_TEXT_CHARS)
    for char in text:
        point = ord(char)
        if point in TEXT_FORBIDDEN or (unicodedata.category(char) == "Cc" and point not in TEXT_CONTROLS_ALLOWED):
            fail("header_invalid")
    return text


def check_body(value: object) -> dict[str, object]:
    """The body JSON of a message: exact keys, `v` 1, a valid `name` and `text`."""
    if not isinstance(value, dict) or set(value) != BODY_KEYS:
        fail("header_invalid")
    fields.literal(value["v"], VERSION)
    check_name(value["name"])
    check_text(value["text"])
    return dict(value)


def body_bytes(name: str, text: str) -> bytes:
    """Body JSON (sorted keys, no white space, UTF-8) of a checked body."""
    body = check_body({"name": name, "text": text, "v": VERSION})
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def seal_message(
    community_key: bytes,
    header_fields: object,
    body: bytes,
    author: Ed25519PrivateKey,
    *,
    fixed_salt_nonce: tuple[bytes, bytes] | None = None,
) -> dict[str, object]:
    """Encrypt the body under `CMK_e` and sign as the author; fresh salt and nonce."""
    return signed.seal(FORMAT, community_key, header_fields, body, author, fixed_salt_nonce=fixed_salt_nonce)


def verify_message(message: object, author_sig_public: bytes) -> dict[str, object]:
    """Structure, header, size and signature without any key; returns the checked header."""
    return signed.verify(FORMAT, message, author_sig_public)[0]


def open_message(community_key: bytes, message: object, author_sig_public: bytes) -> dict[str, object]:
    """`verify_message`, decrypt and check the body (`decrypt_failed`, `header_invalid`)."""
    plaintext = signed.open_(FORMAT, community_key, message, author_sig_public)
    try:
        body = json.loads(plaintext.decode("utf-8"))
    except ValueError:
        fail("header_invalid")
    return check_body(body)


def wrap_community_key(
    recipient_kem_public: bytes,
    *,
    epoch: int,
    recipient: str,
    key: bytes,
    ephemeral: X25519PrivateKey | None = None,
) -> str:
    """Wrap `CMK_e` for one user (`agent_runner.crypto.wrap` with the community scope)."""
    return wrap.wrap_key(
        recipient_kem_public,
        scope=SCOPE,
        scope_id=SCOPE_ID,
        epoch=epoch,
        recipient=recipient,
        key=key,
        ephemeral=ephemeral,
    )


def unwrap_community_key(recipient_kem: X25519PrivateKey, *, epoch: int, recipient: str, wrap_text: str) -> bytes:
    """Open a community key wrap."""
    return wrap.unwrap_key(
        recipient_kem, scope=SCOPE, scope_id=SCOPE_ID, epoch=epoch, recipient=recipient, wrap=wrap_text
    )


def sign_community_wrap(wrapper: Ed25519PrivateKey, *, epoch: int, recipient: str, wrap_text: str) -> str:
    """The wrapper's signature of a community key wrap."""
    return wrap.sign_wrap(wrapper, scope=SCOPE, scope_id=SCOPE_ID, epoch=epoch, recipient=recipient, wrap=wrap_text)


def verify_community_wrap(
    wrapper_sig_public: bytes, *, epoch: int, recipient: str, wrap_text: str, signature: str
) -> None:
    """Check the wrapper's signature (`signature_invalid`)."""
    wrap.verify_wrap(
        wrapper_sig_public,
        scope=SCOPE,
        scope_id=SCOPE_ID,
        epoch=epoch,
        recipient=recipient,
        wrap=wrap_text,
        signature=signature,
    )
