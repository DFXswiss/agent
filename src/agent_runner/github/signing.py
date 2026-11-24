"""Commit objects and SSH commit signatures (KOR-47, ADR-076 sections 4 and 5).

- `Person(name, email, time)`: an author or committer line as git writes it (`name <email> time +0000`; the runner
  always commits in UTC with whole seconds, the form GitHub stores for dates ending in `Z`).
- `noreply(user_id, login)`: the account's `<id>+<login>@users.noreply.github.com` address, which GitHub attributes to
  the account (every account has it; no address of the person is needed or stored).
- `commit_text(...)`: the commit object without signature, the exact bytes a signature covers (`tree`, `parent`s,
  `author`, `committer`, an empty line, the message as sent).
- `signed_commit_id(text, signature)`: the object ID git and GitHub compute for the commit with the signature in its
  `gpgsig` header (each further line of the signature indented by one space, before the empty line).
- `new_seed()`, `public_line(seed, comment)`: an Ed25519 signing key and its OpenSSH public key line.
- `sign(seed, data)`: the armoured SSH signature (`ssh-keygen -Y sign -n git`, PROTOCOL.sshsig: magic `SSHSIG`,
  version 1, namespace `git`, empty reserved field, SHA-512 of the data).

Pure functions without I/O; the GitHub double verifies the same format independently (`agent_doubles.github.sshsig`).
"""

from __future__ import annotations

import base64
import hashlib
import os
import re
import struct
from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

NAMESPACE = b"git"
HASH = b"sha512"
KEY_TYPE = b"ssh-ed25519"
MAGIC = b"SSHSIG"
SIG_VERSION = 1
SEED_BYTES = 32
ARMOR_BEGIN = "-----BEGIN SSH SIGNATURE-----"
ARMOR_END = "-----END SSH SIGNATURE-----"
LINE = 70
NOREPLY_DOMAIN = "users.noreply.github.com"
_UNSAFE = re.compile(r"[<>\n\r\x00]")


def _string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


@dataclass(frozen=True)
class Person:
    """An author or committer of a commit object."""

    name: str
    email: str
    time: int

    def line(self) -> str:
        if _UNSAFE.search(self.name) or _UNSAFE.search(self.email) or not self.name.strip():
            raise ValueError("person")
        return f"{self.name} <{self.email}> {self.time} +0000"

    def api(self) -> dict[str, str]:
        """The form of `POST /repos/{r}/git/commits` (`date` in UTC with whole seconds)."""
        date = datetime.fromtimestamp(self.time, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {"name": self.name, "email": self.email, "date": date}


def noreply(user_id: int, login: str) -> str:
    """The account's noreply address."""
    return f"{user_id}+{login}@{NOREPLY_DOMAIN}"


def commit_text(tree: str, parents: list[str], author: Person, committer: Person, message: str) -> bytes:
    """The commit object as git writes it, without signature."""
    lines = [f"tree {tree}", *(f"parent {parent}" for parent in parents)]
    lines += [f"author {author.line()}", f"committer {committer.line()}"]
    return ("\n".join(lines) + "\n\n" + message).encode()


def with_signature(text: bytes, signature: str) -> bytes:
    """The commit object with `signature` in its `gpgsig` header."""
    head, separator, message = text.partition(b"\n\n")
    if not separator:
        raise ValueError("commit")
    header = "gpgsig " + "\n ".join(signature.rstrip("\n").split("\n"))
    return head + b"\n" + header.encode() + b"\n\n" + message


def object_id(text: bytes) -> str:
    """The SHA-1 object ID of a commit object."""
    return hashlib.sha1(b"commit %d\0" % len(text) + text, usedforsecurity=False).hexdigest()


def signed_commit_id(text: bytes, signature: str) -> str:
    return object_id(with_signature(text, signature))


def new_seed() -> bytes:
    return os.urandom(SEED_BYTES)


def _key(seed: bytes) -> Ed25519PrivateKey:
    if len(seed) != SEED_BYTES:
        raise ValueError("seed")
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_blob(seed: bytes) -> bytes:
    raw = _key(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return _string(KEY_TYPE) + _string(raw)


def public_line(seed: bytes, comment: str) -> str:
    """`ssh-ed25519 <base64> <comment>`."""
    if _UNSAFE.search(comment):
        raise ValueError("comment")
    return f"ssh-ed25519 {base64.b64encode(public_blob(seed)).decode('ascii')} {comment}".rstrip()


def signed_data(data: bytes) -> bytes:
    """What the key signs (PROTOCOL.sshsig)."""
    digest = hashlib.sha512(data).digest()
    return MAGIC + _string(NAMESPACE) + _string(b"") + _string(HASH) + _string(digest)


def sign(seed: bytes, data: bytes) -> str:
    """The armoured SSH signature of `data`."""
    raw = _key(seed).sign(signed_data(data))
    blob = (
        MAGIC
        + struct.pack(">I", SIG_VERSION)
        + _string(public_blob(seed))
        + _string(NAMESPACE)
        + _string(b"")
        + _string(HASH)
        + _string(_string(KEY_TYPE) + _string(raw))
    )
    text = base64.b64encode(blob).decode("ascii")
    lines = [text[index : index + LINE] for index in range(0, len(text), LINE)]
    return "\n".join([ARMOR_BEGIN, *lines, ARMOR_END]) + "\n"
