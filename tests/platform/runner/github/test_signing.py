"""U-RUN-github (KOR-47): commit objects and SSH commit signatures (`github.signing`, ADR-078 sections 4 and 5),
checked against the GitHub double's independent implementation (`agent_doubles.github.objects`, `.sshsig`)."""

from __future__ import annotations

import base64
import hashlib

import pytest

from agent_doubles.github import sshsig
from agent_doubles.github.objects import Commit, Signature
from agent_runner.github import signing

SEED = bytes(range(32))
TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
PARENTS = ["a" * 40, "b" * 40]
AUTHOR = signing.Person("octo-bot", "103+octo-bot@users.noreply.github.com", 1_800_000_000)
COMMITTER = signing.Person("octo-user", "101+octo-user@users.noreply.github.com", 1_800_000_001)
MESSAGE = "Change.\n\nCo-authored-by: other-user <102+other-user@users.noreply.github.com>\n"


def double(person: signing.Person) -> Signature:
    return Signature(person.name, person.email, person.time)


def test_the_commit_text_is_what_git_and_the_double_write() -> None:
    text = signing.commit_text(TREE, PARENTS, AUTHOR, COMMITTER, MESSAGE)
    made = Commit(TREE, tuple(PARENTS), double(AUTHOR), double(COMMITTER), MESSAGE)
    assert text == made.unsigned_payload()
    assert text.startswith(f"tree {TREE}\nparent {'a' * 40}\nparent {'b' * 40}\nauthor octo-bot".encode())
    assert signing.object_id(text) == hashlib.sha1(b"commit %d\0" % len(text) + text, usedforsecurity=False).hexdigest()


def test_the_signature_verifies_and_the_signed_id_is_the_git_object_id() -> None:
    text = signing.commit_text(TREE, PARENTS, AUTHOR, COMMITTER, MESSAGE)
    armoured = signing.sign(SEED, text)
    lines = armoured.splitlines()
    assert (lines[0], lines[-1]) == (signing.ARMOR_BEGIN, signing.ARMOR_END)
    assert all(len(line) <= signing.LINE for line in lines[1:-1])
    parsed = sshsig.classify(armoured)
    assert isinstance(parsed, sshsig.SshSignature)
    assert (parsed.namespace, parsed.hash) == (b"git", b"sha512")
    assert sshsig.verifies(parsed, text)
    assert not sshsig.verifies(parsed, text + b"x")
    assert parsed.public_key == sshsig.parse_public_key(signing.public_line(SEED, "agent"))
    signed = Commit(TREE, tuple(PARENTS), double(AUTHOR), double(COMMITTER), MESSAGE, armoured)
    payload = signed.payload()
    assert signing.with_signature(text, armoured) == payload
    assert signing.signed_commit_id(text, armoured) == signing.object_id(payload)


def test_persons_and_addresses() -> None:
    assert signing.noreply(101, "octo-user") == "101+octo-user@users.noreply.github.com"
    assert AUTHOR.line() == "octo-bot <103+octo-bot@users.noreply.github.com> 1800000000 +0000"
    assert AUTHOR.api() == {
        "name": "octo-bot",
        "email": "103+octo-bot@users.noreply.github.com",
        "date": "2027-01-15T08:00:00Z",
    }
    for bad in (
        signing.Person("a<b", "x@y", 1),
        signing.Person("a", "x>y", 1),
        signing.Person("a\nb", "x@y", 1),
        signing.Person("  ", "x@y", 1),
    ):
        with pytest.raises(ValueError, match="person"):
            bad.line()


def test_keys_and_their_public_lines() -> None:
    assert len(signing.new_seed()) == signing.SEED_BYTES
    line = signing.public_line(SEED, "agent")
    kind, blob, comment = line.split(" ")
    assert (kind, comment) == ("ssh-ed25519", "agent")
    assert base64.b64decode(blob) == signing.public_blob(SEED)
    assert signing.public_line(SEED, "") == f"ssh-ed25519 {blob}"
    with pytest.raises(ValueError, match="comment"):
        signing.public_line(SEED, "a\nb")
    with pytest.raises(ValueError, match="seed"):
        signing.sign(b"short", b"data")
    with pytest.raises(ValueError, match="commit"):
        signing.with_signature(b"tree x", "sig")
