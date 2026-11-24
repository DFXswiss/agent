"""SSH signing keys and SSH commit signatures as GitHub checks them, verified independently of the runner's signer.

Public keys (``POST /user/ssh_signing_keys``): an OpenSSH public key line ``ssh-ed25519 <base64> [comment]``. The
double accepts Ed25519 keys only (GitHub also takes RSA, ECDSA and security-key types; the platform creates Ed25519
keys only, ADR-076 section 5): the base64 must decode to ``string("ssh-ed25519") string(32-byte key)`` with nothing
after it. `parse_public_key` returns that blob; `key_line` the normalised line without comment (GitHub stores no
comment).

Signatures (the ``signature`` of ``POST …/git/commits``, git's ``gpgsig`` header), `classify`:

- an armoured SSH signature (``-----BEGIN SSH SIGNATURE-----`` … ``-----END SSH SIGNATURE-----``, base64 in between)
  in the format of PROTOCOL.sshsig: magic ``SSHSIG``, ``uint32`` version 1, ``string`` public key, ``string``
  namespace, ``string`` reserved, ``string`` hash algorithm (``sha256`` or ``sha512``), ``string`` signature
  (``string`` signature type, ``string`` signature bytes), nothing after it. Anything that does not parse is
  ``malformed_signature``. The signed data is ``"SSHSIG" ‖ string(namespace) ‖ string(reserved) ‖ string(hash) ‖
  string(H(payload))`` with the payload the commit object without its ``gpgsig`` header; `verifies` checks it
  (Ed25519 only) and requires the namespace ``git`` and the signature type ``ssh-ed25519``;
- a PGP armour (``-----BEGIN PGP SIGNATURE-----``): ``gpgverify_unavailable``. The double models no GPG keys and
  cannot check OpenPGP; GitHub's documented reason for "the signature could not be checked" is the closest one;
- anything else: ``unknown_signature_type``.

The doubles import only the standard library (07-testing.md section 1), so `ed25519_verify` is the verification of
RFC 8032 section 5.1.7 in plain integer arithmetic (cofactorless, ``S < L`` and canonical point encodings required, like
OpenSSH and `cryptography`). It is a test oracle, not constant-time code.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import struct
from dataclasses import dataclass

KEY_TYPE = b"ssh-ed25519"
KEY_BYTES = 32
MAGIC = b"SSHSIG"
VERSION = 1
NAMESPACE = b"git"
HASHES = {b"sha256": hashlib.sha256, b"sha512": hashlib.sha512}
SSH_BEGIN = "-----BEGIN SSH SIGNATURE-----"
SSH_END = "-----END SSH SIGNATURE-----"
PGP_BEGIN = "-----BEGIN PGP SIGNATURE-----"


# Ed25519 (RFC 8032 section 5.1): the field prime, the curve constant, the group order, a square root of -1.
_P = 2**255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P
_L = 2**252 + 27742317777372353535851937790883648493
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)
_Point = tuple[int, int, int, int]
"""A point in extended coordinates ``(X, Y, Z, T)``."""


def _recover_x(y: int, sign: int) -> int | None:
    """The x coordinate of the point with `y` whose lowest bit is `sign`, None when there is none."""
    if y >= _P:
        return None
    x2 = (y * y - 1) * pow(_D * y * y + 1, _P - 2, _P) % _P
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P or (x == 0 and sign):
        return None
    return _P - x if x & 1 != sign else x


def _decompress(data: bytes) -> _Point | None:
    number = int.from_bytes(data, "little")
    y = number & ((1 << 255) - 1)
    x = _recover_x(y, number >> 255)
    return None if x is None else (x, y, 1, x * y % _P)


def _add(a: _Point, b: _Point) -> _Point:
    first = (a[1] - a[0]) * (b[1] - b[0]) % _P
    second = (a[1] + a[0]) * (b[1] + b[0]) % _P
    c, d = 2 * a[3] * b[3] * _D % _P, 2 * a[2] * b[2] % _P
    e, f, g, h = second - first, d - c, d + c, second + first
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _multiply(scalar: int, point: _Point) -> _Point:
    result: _Point = (0, 1, 1, 0)
    while scalar:
        if scalar & 1:
            result = _add(result, point)
        point, scalar = _add(point, point), scalar >> 1
    return result


def _same(a: _Point, b: _Point) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


_GY = 4 * pow(5, _P - 2, _P) % _P
_GX = _recover_x(_GY, 0)
assert _GX is not None
_BASE: _Point = (_GX, _GY, 1, _GX * _GY % _P)


def ed25519_verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """Whether `signature` (64 bytes) is a valid Ed25519 signature of `message` by the 32-byte key `public`."""
    if len(public) != KEY_BYTES or len(signature) != 2 * KEY_BYTES:
        return False
    key, point_r = _decompress(public), _decompress(signature[:KEY_BYTES])
    s = int.from_bytes(signature[KEY_BYTES:], "little")
    if key is None or point_r is None or s >= _L:
        return False
    digest = hashlib.sha512(signature[:KEY_BYTES] + public + message).digest()
    h = int.from_bytes(digest, "little") % _L
    return _same(_multiply(s, _BASE), _add(point_r, _multiply(h, key)))


class Malformed(ValueError):
    """A key or signature that cannot be parsed."""


@dataclass(frozen=True)
class SshSignature:
    """A parsed SSH signature (the fields of PROTOCOL.sshsig)."""

    public_key: bytes
    namespace: bytes
    reserved: bytes
    hash: bytes
    sig_type: bytes
    sig: bytes


class _Reader:
    """Reads the SSH wire format (``uint32`` and length-prefixed ``string``)."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.at = 0

    def take(self, size: int) -> bytes:
        if self.at + size > len(self.data):
            raise Malformed("truncated")
        part = self.data[self.at : self.at + size]
        self.at += size
        return part

    def uint32(self) -> int:
        value: int = struct.unpack(">I", self.take(4))[0]
        return value

    def string(self) -> bytes:
        return self.take(self.uint32())

    def end(self) -> None:
        if self.at != len(self.data):
            raise Malformed("trailing data")


def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def _decode(text: str) -> bytes:
    try:
        return base64.b64decode(text, validate=True)
    except binascii.Error as exc:
        raise Malformed("base64") from exc


def _ed25519_key(blob: bytes) -> bytes:
    """The raw 32-byte key of an Ed25519 public key blob."""
    reader = _Reader(blob)
    if reader.string() != KEY_TYPE:
        raise Malformed("key type")
    raw = reader.string()
    reader.end()
    if len(raw) != KEY_BYTES:
        raise Malformed("key length")
    return raw


def parse_public_key(line: str) -> bytes:
    """The public key blob of an OpenSSH ``ssh-ed25519`` key line (`Malformed` otherwise)."""
    parts = line.split()
    if len(parts) < 2 or parts[0] != KEY_TYPE.decode():
        raise Malformed("key line")
    blob = _decode(parts[1])
    _ed25519_key(blob)
    return blob


def key_line(blob: bytes) -> str:
    """``ssh-ed25519 <base64>`` of a key blob."""
    return f"{KEY_TYPE.decode()} {base64.b64encode(blob).decode('ascii')}"


def parse(text: str) -> SshSignature:
    """An armoured SSH signature (`Malformed` when it does not follow PROTOCOL.sshsig)."""
    lines = text.strip().splitlines()
    if len(lines) < 2 or lines[0].strip() != SSH_BEGIN or lines[-1].strip() != SSH_END:
        raise Malformed("armour")
    reader = _Reader(_decode("".join(line.strip() for line in lines[1:-1])))
    if reader.take(len(MAGIC)) != MAGIC or reader.uint32() != VERSION:
        raise Malformed("magic or version")
    public_key, namespace, reserved, digest, inner = (reader.string() for _ in range(5))
    reader.end()
    if digest not in HASHES:
        raise Malformed("hash algorithm")
    sig_reader = _Reader(inner)
    sig_type, sig = sig_reader.string(), sig_reader.string()
    sig_reader.end()
    return SshSignature(public_key, namespace, reserved, digest, sig_type, sig)


def classify(text: str) -> SshSignature | str:
    """The parsed SSH signature, or the verification reason of a signature that is none
    (``malformed_signature``, ``gpgverify_unavailable``, ``unknown_signature_type``)."""
    first = text.strip().split("\n", 1)[0].strip()
    if first == PGP_BEGIN:
        return "gpgverify_unavailable"
    if first != SSH_BEGIN:
        return "unknown_signature_type"
    try:
        return parse(text)
    except Malformed:
        return "malformed_signature"


def signed_data(signature: SshSignature, payload: bytes) -> bytes:
    """What the key signed for `payload` (PROTOCOL.sshsig)."""
    digest = HASHES[signature.hash](payload).digest()
    return (
        MAGIC + _string(signature.namespace) + _string(signature.reserved) + _string(signature.hash) + _string(digest)
    )


def verifies(signature: SshSignature, payload: bytes) -> bool:
    """Whether `signature` is a valid Ed25519 signature of `payload` in the namespace ``git``."""
    if signature.namespace != NAMESPACE or signature.sig_type != KEY_TYPE:
        return False
    try:
        key = _ed25519_key(signature.public_key)
    except Malformed:
        return False
    return ed25519_verify(key, signed_data(signature, payload), signature.sig)
