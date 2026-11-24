"""Copied from agent-core src/agent_hub/crypto/identity.py at 952bb156fc9c.

Identity proof and account code of a user (04-crypto.md sections 3 and 5.13, ADR-060).

Identity proof: `id_sig = Ed25519_{sk_inv}(P || "invite-identity\\0" || canonical({"pub": pub, "user": user}))`, made
by the registering browser from the code of the platform invitation it registers with. Together with the root-signed
`user_invite {pk_inv}` of the platform log it proves, without trusting the hub, which identity `pub = {kem, sig}` the
holder of the code registered for `user`. A user registered through a platform invitation has the ID **derived** from
that invitation (`invited_user_id`: UUIDv7 with the milliseconds of the root-signed `user_invite` entry and the
random bits `SHA-256(P || "user-id\\0" || invite_id)[:10]`), so a proof made with one code names exactly one account:
the holder of another code cannot sign for someone else's account.

Account code: `base32(SHA-256(P || "account-code\\0" || user || "\\0" || pub.sig || pub.kem))[:12]` (RFC 4648
alphabet, uppercase), shown as `XXXX-XXXX-XXXX`. Every user sees its own code in the settings and may pass it on; the
hub resolves a code to an account, and the admitting client recomputes it from the proven keys, so a typed code also
checks the keys. The parser strips `-` and spaces, uppercases and reads the look-alikes `0`, `1`, `8` as `O`, `I`,
`B`; a wrong length or a character outside the alphabet is `account_code_invalid`.
"""

from __future__ import annotations

import base64
import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from agent_runner.crypto import fields
from agent_runner.crypto.encoding import P, b64u_decode, b64u_encode, canonical, parse_time, require_keys
from agent_runner.crypto.errors import CryptoError, fail
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.kdf import LABEL_ACCOUNT_CODE, LABEL_INVITE_IDENTITY, LABEL_USER_ID
from agent_runner.crypto.keys import RAW_KEY_BYTES, ed25519_sign, ed25519_verify_b64u

ACCOUNT_CODE_LENGTH = 12
INVITE_ID_BYTES = 16
_PUB_KEYS = frozenset({"kem", "sig"})
_CODE_RE = re.compile(r"[A-Z2-7]{12}")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_LOOKALIKES = str.maketrans({"0": "O", "1": "I", "8": "B"})


def _pub(pub: object) -> dict[str, str]:
    found = require_keys(pub, _PUB_KEYS)
    fields.b64u_bytes(found["kem"], RAW_KEY_BYTES)
    fields.b64u_bytes(found["sig"], RAW_KEY_BYTES)
    return {"kem": str(found["kem"]), "sig": str(found["sig"])}


def identity_text(user: str, pub: Mapping[str, str]) -> bytes:
    """`P || "invite-identity\\0" || canonical({"pub": pub, "user": user})`."""
    return P + LABEL_INVITE_IDENTITY + canonical({"pub": _pub(pub), "user": fields.uuid(user)})


def sign_identity(signing: Ed25519PrivateKey, user: str, pub: Mapping[str, str]) -> str:
    """The b64u `id_sig` by `sk_inv` of the code."""
    return b64u_encode(ed25519_sign(signing, identity_text(user, pub)))


def identity_valid(pk_inv: str, user: str, pub: Mapping[str, str], id_sig: object) -> bool:
    """True if `id_sig` is a valid signature of the identity text under `pk_inv` (b64u)."""
    try:
        ed25519_verify_b64u(b64u_decode(pk_inv, RAW_KEY_BYTES), id_sig, identity_text(user, pub))
    except CryptoError:
        return False
    return True


def invited_user_id(invite_id: str, invite_ts: str) -> str:
    """The user ID of the account registered through the platform invitation `invite_id` whose `user_invite` entry
    has the time `invite_ts`."""
    ms = (parse_time(invite_ts) - _EPOCH) // timedelta(milliseconds=1)
    digest = hashlib.sha256(P + LABEL_USER_ID + b64u_decode(invite_id, INVITE_ID_BYTES)).digest()
    return uuid7(ms, digest[:10])


def account_code(user: str, pub: Mapping[str, str]) -> str:
    """The 12-character account code (without dashes)."""
    keys = _pub(pub)
    digest = hashlib.sha256(
        P
        + LABEL_ACCOUNT_CODE
        + fields.uuid(user).encode("ascii")
        + b"\x00"
        + b64u_decode(keys["sig"], RAW_KEY_BYTES)
        + b64u_decode(keys["kem"], RAW_KEY_BYTES)
    ).digest()
    return base64.b32encode(digest).decode("ascii")[:ACCOUNT_CODE_LENGTH]


def format_account_code(code: str) -> str:
    """`XXXX-XXXX-XXXX`."""
    return "-".join(code[i : i + 4] for i in range(0, ACCOUNT_CODE_LENGTH, 4))


def parse_account_code(text: object) -> str:
    """The 12 characters of a typed code (`account_code_invalid` otherwise)."""
    if not isinstance(text, str):
        fail("account_code_invalid")
    code = text.replace("-", "").replace(" ", "").upper().translate(_LOOKALIKES)
    if not _CODE_RE.fullmatch(code):
        fail("account_code_invalid")
    return code
