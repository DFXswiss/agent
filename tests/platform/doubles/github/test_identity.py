"""U-DBL-github: commit identities of the GitHub double (KOR-47, ADR-078 "Pre-study").

Author, committer and ``signature`` of ``POST …/git/commits``, the ``verification`` of commit answers, the account
attribution by email and the SSH signing keys of ``/user/ssh_signing_keys``. The signatures are made here with
`cryptography` in the format of PROTOCOL.sshsig (independently of the runner's signer and of the double's checker).
"""

from __future__ import annotations

import base64
import hashlib
import struct
from collections.abc import Callable
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from agent_doubles.github import sshsig
from agent_doubles.github.objects import EMPTY_TREE, Commit, Signature, object_id
from agent_doubles.github.repos import SeedError, World, iso, parse_iso

REPO = "/repos/octo-org/hello"
KEYS = "/user/ssh_signing_keys"
OCTO_NOREPLY = "101+octo-user@users.noreply.github.com"
DATE = "2026-03-01T12:00:00Z"
ALL = "read:ssh_signing_key, write:ssh_signing_key, admin:ssh_signing_key"
SSH_BEGIN = "-----BEGIN SSH SIGNATURE-----"
SSH_END = "-----END SSH SIGNATURE-----"


def string(data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + data


def key_blob(key: Ed25519PrivateKey) -> bytes:
    return string(b"ssh-ed25519") + string(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def key_line(key: Ed25519PrivateKey, comment: str = "") -> str:
    return f"ssh-ed25519 {base64.b64encode(key_blob(key)).decode()} {comment}".rstrip()


def armour(blob: bytes) -> str:
    text = base64.b64encode(blob).decode()
    return "\n".join([SSH_BEGIN, *(text[i : i + 70] for i in range(0, len(text), 70)), SSH_END]) + "\n"


def sign(
    key: Ed25519PrivateKey,
    data: bytes,
    *,
    namespace: bytes = b"git",
    algorithm: bytes = b"sha512",
    sig_type: bytes = b"ssh-ed25519",
    public: bytes | None = None,
) -> str:
    """An armoured SSH signature of `data` (PROTOCOL.sshsig), with fields that can be set wrong on purpose."""
    digest = hashlib.new(algorithm.decode(), data).digest()
    signed = b"SSHSIG" + string(namespace) + string(b"") + string(algorithm) + string(digest)
    blob = (
        b"SSHSIG"
        + struct.pack(">I", 1)
        + string(key_blob(key) if public is None else public)
        + string(namespace)
        + string(b"")
        + string(algorithm)
        + string(string(sig_type) + string(key.sign(signed)))
    )
    return armour(blob)


def commit_text(tree: str, parents: list[str], author: str, committer: str, message: str) -> bytes:
    """The commit object as git writes it (without signature); `author`/`committer` are ``name <email>``."""
    stamp = parse_iso(DATE)
    lines = [f"tree {tree}", *(f"parent {p}" for p in parents)]
    lines += [f"author {author} {stamp} +0000", f"committer {committer} {stamp} +0000"]
    return ("\n".join(lines) + "\n\n" + message).encode()


def with_gpgsig(text: bytes, signature: str) -> bytes:
    head, _, message = text.partition(b"\n\n")
    return head + b"\ngpgsig " + "\n ".join(signature.rstrip("\n").split("\n")).encode() + b"\n\n" + message


def person(name: str, email: str) -> dict[str, str]:
    return {"name": name, "email": email, "date": DATE}


def identity_seed(seed: Callable[[], Any], other_key: str) -> Any:
    """The base seed with classic tokens of every signing-key scope and a seeded key of ``other-user``."""
    data = seed()
    octo, other = data["users"]
    octo["tokens"] += [
        {"token": "sign-admin", "kind": "classic", "scopes": ["repo", "admin:ssh_signing_key"]},
        {"token": "sign-write", "kind": "classic", "scopes": ["repo", "write:ssh_signing_key"]},
        {"token": "sign-read", "kind": "classic", "scopes": ["read:ssh_signing_key"]},
        {"token": "repo-only", "kind": "classic", "scopes": ["repo"]},
        {"token": "fine", "kind": "fine_grained"},
        {"token": "oauth-write", "kind": "oauth", "scopes": ["repo", "write:ssh_signing_key"]},
        {"token": "oauth-repo", "kind": "oauth", "scopes": ["repo"]},
    ]
    other["tokens"] += [{"token": "other-admin", "kind": "classic", "scopes": ["repo", "admin:ssh_signing_key"]}]
    other["ssh_signing_keys"] = [other_key]
    return data


@pytest.fixture
def keys() -> dict[str, Ed25519PrivateKey]:
    return {name: Ed25519PrivateKey.generate() for name in ("octo", "other", "stranger")}


@pytest.fixture
def gh(start_github: Callable[..., Any], seed: Callable[[], Any], keys: dict[str, Ed25519PrivateKey]) -> Any:
    return start_github(identity_seed(seed, key_line(keys["other"], "seeded")))


def head(gh: Any) -> str:
    sha: str = gh.get(f"{REPO}/git/ref/heads/main")["object"]["sha"]
    return sha


def register(gh: Any, key: Ed25519PrivateKey, token: str = "sign-admin") -> dict[str, Any]:
    created = gh.api("POST", KEYS, {"title": "probe", "key": key_line(key, "a comment")}, token=token)
    assert created.status == 201, created.body
    answer: dict[str, Any] = created.json()
    return answer


# --- the signature module -------------------------------------------------------------------------------


def test_public_keys(keys: dict[str, Ed25519PrivateKey]) -> None:
    blob = sshsig.parse_public_key(key_line(keys["octo"], "comment"))
    assert blob == key_blob(keys["octo"])
    assert sshsig.key_line(blob) == key_line(keys["octo"])
    raw = keys["octo"].public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    for line in (
        "",
        "ssh-ed25519",
        f"ssh-rsa {base64.b64encode(key_blob(keys['octo'])).decode()}",
        "ssh-ed25519 not*base64",
        f"ssh-ed25519 {base64.b64encode(string(b'ssh-rsa') + string(raw)).decode()}",
        f"ssh-ed25519 {base64.b64encode(string(b'ssh-ed25519') + string(raw[:31])).decode()}",
        f"ssh-ed25519 {base64.b64encode(key_blob(keys['octo']) + b'x').decode()}",
        f"ssh-ed25519 {base64.b64encode(b'\0\0').decode()}",
    ):
        with pytest.raises(sshsig.Malformed):
            sshsig.parse_public_key(line)


def test_signatures_parse_and_verify(keys: dict[str, Ed25519PrivateKey]) -> None:
    key = keys["octo"]
    good = sshsig.parse(sign(key, b"data"))
    assert (good.public_key, good.namespace, good.reserved, good.hash, good.sig_type) == (
        key_blob(key),
        b"git",
        b"",
        b"sha512",
        b"ssh-ed25519",
    )
    assert sshsig.verifies(good, b"data")
    assert not sshsig.verifies(good, b"other data")
    assert sshsig.verifies(sshsig.parse(sign(key, b"data", algorithm=b"sha256")), b"data")
    assert not sshsig.verifies(sshsig.parse(sign(key, b"data", namespace=b"file")), b"data")
    assert not sshsig.verifies(sshsig.parse(sign(key, b"data", sig_type=b"ssh-rsa")), b"data")
    rsa = string(b"ssh-rsa") + string(b"\1" * 32)
    assert not sshsig.verifies(sshsig.parse(sign(key, b"data", public=rsa)), b"data")
    short = string(b"ssh-ed25519") + string(b"\1" * 31)
    assert not sshsig.verifies(sshsig.parse(sign(key, b"data", public=short)), b"data")
    assert sshsig.classify(sign(key, b"data")) == good
    assert sshsig.classify("-----BEGIN PGP SIGNATURE-----\n\nabc\n-----END PGP SIGNATURE-----\n") == (
        "gpgverify_unavailable"
    )
    assert sshsig.classify("") == "unknown_signature_type"
    assert sshsig.classify("signed by me") == "unknown_signature_type"
    assert sshsig.classify(f"{SSH_BEGIN}\n!!\n{SSH_END}") == "malformed_signature"


def test_ed25519_verification_agrees_with_cryptography(keys: dict[str, Ed25519PrivateKey]) -> None:
    key = keys["octo"]
    public = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    signature = key.sign(b"message")
    assert sshsig.ed25519_verify(public, b"message", signature)
    assert not sshsig.ed25519_verify(public, b"messagE", signature)
    assert not sshsig.ed25519_verify(public[:31], b"message", signature)
    assert not sshsig.ed25519_verify(public, b"message", signature[:63])
    no_point = (2).to_bytes(32, "little")  # y = 2 is on no point of the curve
    assert not sshsig.ed25519_verify(no_point, b"message", signature)
    assert not sshsig.ed25519_verify(public, b"message", no_point + signature[32:])
    too_big = (sshsig._L + int.from_bytes(signature[32:], "little")).to_bytes(32, "little")
    assert not sshsig.ed25519_verify(public, b"message", signature[:32] + too_big)


def test_ed25519_point_decoding() -> None:
    """Every case of recovering x from y (RFC 8032 section 5.1.3), with fixed values."""
    p = sshsig._P
    assert sshsig._recover_x(p, 0) is None  # y not below the prime
    assert sshsig._recover_x(1, 0) == 0  # the neutral point
    assert sshsig._recover_x(1, 1) is None  # x = 0 has no odd form
    assert sshsig._recover_x(2, 0) is None  # no square root
    for y in (3, 9):  # 3 needs the square root of -1, 9 does not
        even, odd = sshsig._recover_x(y, 0), sshsig._recover_x(y, 1)
        assert even is not None
        assert odd is not None
        assert (even % 2, odd % 2, (even + odd) % p) == (0, 1, 0)
        assert (even * even - (y * y - 1) * pow(sshsig._D * y * y + 1, p - 2, p)) % p == 0


def _blob(*, magic: bytes = b"SSHSIG", version: int = 1, fields: int = 5, extra: bytes = b"") -> bytes:
    inner = string(string(b"ssh-ed25519") + string(b"s"))
    parts = [string(b"k"), string(b"git"), string(b""), string(b"sha512"), inner][:fields]
    return magic + struct.pack(">I", version) + b"".join(parts) + extra


@pytest.mark.parametrize(
    "text",
    [
        SSH_BEGIN,
        f"{SSH_BEGIN}\n{base64.b64encode(_blob()).decode()}\n-----END PGP SIGNATURE-----",
        armour(_blob(magic=b"SSHSIX")),
        armour(_blob(version=2)),
        armour(_blob(fields=4)),
        armour(_blob(extra=b"x")),
        armour(_blob().replace(b"sha512", b"sha384")),
        armour(_blob()[:-1]),
        armour(b"SSHSIG\0\0\0\1" + string(b"k") + string(b"git") + string(b"") + string(b"sha512") + string(b"\0\0")),
        armour(
            b"SSHSIG\0\0\0\1"
            + string(b"k")
            + string(b"git")
            + string(b"")
            + string(b"sha512")
            + string(string(b"ssh-ed25519") + string(b"s") + b"x")
        ),
    ],
)
def test_malformed_signatures(text: str) -> None:
    with pytest.raises(sshsig.Malformed):
        sshsig.parse(text)
    assert sshsig.classify(text) == "malformed_signature"


# --- commits: author, committer, signature, verification, attribution -----------------------------------


def test_signed_commit_is_verified_and_stays_verified(gh: Any, keys: dict[str, Ed25519PrivateKey]) -> None:
    key = register(gh, keys["octo"])
    parent = head(gh)
    tree = gh.get(f"{REPO}/git/commits/{parent}")["tree"]["sha"]
    me = f"Octo User <{OCTO_NOREPLY}>"
    message = "Signed.\n\nCo-authored-by: other-user <102+other-user@users.noreply.github.com>\n"
    text = commit_text(tree, [parent], me, me, message)
    signature = sign(keys["octo"], text)
    body = {
        "message": message,
        "tree": tree,
        "parents": [parent],
        "author": person("Octo User", OCTO_NOREPLY),
        "committer": person("Octo User", OCTO_NOREPLY),
        "signature": signature,
    }
    created = gh.api("POST", f"{REPO}/git/commits", body, token="sign-admin")
    assert created.status == 201, created.body
    commit = created.json()
    assert commit["sha"] == object_id("commit", with_gpgsig(text, signature))
    verification = commit["verification"]
    assert verification == {
        "verified": True,
        "reason": "valid",
        "signature": signature,
        "payload": text.decode(),
        "verified_at": verification["verified_at"],
    }
    assert parse_iso(verification["verified_at"]) > 0
    assert commit["author"] == commit["committer"] == {"name": "Octo User", "email": OCTO_NOREPLY, "date": DATE}
    assert gh.get(f"{REPO}/git/commits/{commit['sha']}") == commit
    rest = gh.get(f"{REPO}/commits/{commit['sha']}")
    assert rest["commit"]["verification"] == verification
    assert (rest["author"]["login"], rest["committer"]["login"]) == ("octo-user", "octo-user")
    listed = gh.get(f"{REPO}/compare/{parent}...{commit['sha']}")["commits"]
    assert [c["commit"]["verification"] for c in listed] == [verification]
    # GitHub keeps a verification once decided: removing the key or creating the same commit again changes nothing.
    assert gh.api("DELETE", f"{KEYS}/{key['id']}", token="sign-admin").status == 204
    assert gh.get(f"{REPO}/git/commits/{commit['sha']}")["verification"] == verification
    again = gh.api("POST", f"{REPO}/git/commits", body, token="sign-admin").json()
    assert (again["sha"], again["verification"]) == (commit["sha"], verification)
    # The same content signed with the now unknown key is a different commit and not verified.
    text2 = commit_text(tree, [parent], me, me, "Signed again.\n")
    late = {**body, "message": "Signed again.\n", "signature": sign(keys["octo"], text2)}
    assert gh.api("POST", f"{REPO}/git/commits", late, token="sign-admin").json()["verification"]["reason"] == (
        "unknown_key"
    )


def test_verification_reasons(gh: Any, keys: dict[str, Ed25519PrivateKey]) -> None:
    register(gh, keys["octo"])
    parent = head(gh)
    tree = gh.get(f"{REPO}/git/commits/{parent}")["tree"]["sha"]

    def commit(signature: Callable[[bytes], str], email: str = OCTO_NOREPLY, message: str = "m\n") -> Any:
        text = commit_text(tree, [parent], f"Octo <{email}>", f"Octo <{email}>", message)
        body = {
            "message": message,
            "tree": tree,
            "parents": [parent],
            "author": person("Octo", email),
            "committer": person("Octo", email),
            "signature": signature(text),
        }
        created = gh.api("POST", f"{REPO}/git/commits", body, token="sign-admin")
        assert created.status == 201, created.body
        assert created.json()["verification"]["payload"] == text.decode()
        return created.json()["verification"]

    octo, other, stranger = keys["octo"], keys["other"], keys["stranger"]
    reasons = {
        "valid": commit(lambda t: sign(octo, t)),
        "valid_upper_case_email": commit(lambda t: sign(octo, t), email=OCTO_NOREPLY.upper()),
        "unknown_key": commit(lambda t: sign(stranger, t)),
        "invalid": commit(lambda t: sign(octo, t + b"x")),
        "invalid_namespace": commit(lambda t: sign(octo, t, namespace=b"file")),
        "bad_email_other_account": commit(lambda t: sign(octo, t), email="other@example.invalid"),
        "bad_email_no_account": commit(lambda t: sign(octo, t), email="nobody@example.invalid"),
        "other_valid": commit(lambda t: sign(other, t), email="102+other-user@users.noreply.github.com"),
        "other_by_email": commit(lambda t: sign(other, t), email="other@example.invalid", message="n\n"),
        "malformed": commit(lambda t: f"{SSH_BEGIN}\n!!\n{SSH_END}\n"),
        "pgp": commit(lambda t: "-----BEGIN PGP SIGNATURE-----\n\nabc\n-----END PGP SIGNATURE-----\n"),
        "other_type": commit(lambda t: "signature"),
    }
    assert {name: (v["verified"], v["reason"]) for name, v in reasons.items()} == {
        "valid": (True, "valid"),
        "valid_upper_case_email": (True, "valid"),
        "unknown_key": (False, "unknown_key"),
        "invalid": (False, "invalid"),
        "invalid_namespace": (False, "invalid"),
        "bad_email_other_account": (False, "bad_email"),
        "bad_email_no_account": (False, "bad_email"),
        "other_valid": (True, "valid"),
        "other_by_email": (True, "valid"),
        "malformed": (False, "malformed_signature"),
        "pgp": (False, "gpgverify_unavailable"),
        "other_type": (False, "unknown_signature_type"),
    }
    assert all((v["verified_at"] is None) == (not v["verified"]) for v in reasons.values())
    assert all(isinstance(v["signature"], str) for v in reasons.values())


def test_unsigned_commits_and_attribution(gh: Any) -> None:
    parent = head(gh)
    plain = gh.api("POST", f"{REPO}/git/commits", {"message": "m", "tree": EMPTY_TREE, "parents": [parent]})
    assert plain.json()["verification"] == {
        "verified": False,
        "reason": "unsigned",
        "signature": None,
        "payload": None,
        "verified_at": None,
    }
    body = {
        "message": "Mixed.\n",
        "tree": EMPTY_TREE,
        "parents": [parent],
        "author": person("Other", "102+OTHER-USER@users.noreply.github.com"),
        "committer": person("Octo", OCTO_NOREPLY),
    }
    mixed = gh.api("POST", f"{REPO}/git/commits", body, token="sign-admin").json()
    assert mixed["author"]["date"] == mixed["committer"]["date"] == DATE
    rest = gh.get(f"{REPO}/commits/{mixed['sha']}")
    assert (rest["author"]["login"], rest["committer"]["login"]) == ("other-user", "octo-user")
    # The committer defaults to the author; a null date takes the double's time.
    body = {
        "message": "m",
        "tree": EMPTY_TREE,
        "author": {"name": "Nobody", "email": "x@example.invalid", "date": None},
    }
    alone = gh.api("POST", f"{REPO}/git/commits", body).json()
    assert alone["committer"] == alone["author"]
    assert alone["author"]["date"] != DATE
    rest = gh.get(f"{REPO}/commits/{alone['sha']}")
    assert (rest["author"], rest["committer"]) == (None, None)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"signature": 5}, "Invalid request.\n\nFor 'properties/signature', 5 is not a string."),
        ({"signature": ["a"]}, "Invalid request.\n\nFor 'properties/signature', [\"a\"] is not a string."),
        ({"author": "me"}, "Invalid request.\n\nFor 'properties/author', \"me\" is not an object."),
        ({"committer": [1]}, "Invalid request.\n\nFor 'properties/committer', [1] is not an object."),
        ({"author": {"name": 5, "email": "e"}}, "Invalid request.\n\nFor 'properties/name', 5 is not a string."),
        ({"author": {"name": "n", "email": True}}, "Invalid request.\n\nFor 'properties/email', true is not a string."),
        (
            {"committer": {"name": "n", "email": "e", "date": 5}},
            "Invalid request.\n\nFor 'properties/date', 5 is not a string.",
        ),
        ({"committer": {"email": "e"}}, "Invalid request.\n\nInvalid committer: name and email are required."),
        ({"committer": {"name": "n", "email": "e", "date": "soon"}}, "Invalid request.\n\nInvalid committer date."),
    ],
)
def test_commit_type_errors(gh: Any, extra: dict[str, Any], message: str) -> None:
    response = gh.api("POST", f"{REPO}/git/commits", {"message": "m", "tree": EMPTY_TREE, **extra})
    assert response.status == 422
    assert response.json()["message"] == message


# --- SSH signing keys -----------------------------------------------------------------------------------


def test_signing_key_lifecycle(gh: Any, keys: dict[str, Ed25519PrivateKey]) -> None:
    created = gh.api("POST", KEYS, {"title": "laptop", "key": key_line(keys["octo"], "me@host")}, token="sign-write")
    assert created.status == 201
    key = created.json()
    assert key == {"key": key_line(keys["octo"]), "id": key["id"], "title": "laptop", "created_at": key["created_at"]}
    assert created.headers["location"].endswith(f"{KEYS}/{key['id']}")
    assert iso(parse_iso(key["created_at"])) == key["created_at"]
    untitled = gh.api("POST", KEYS, {"key": key_line(keys["stranger"]), "title": None}, token="sign-admin").json()
    assert untitled["title"] == ""
    assert gh.get(KEYS, token="sign-read") == [key, untitled]
    page = gh.api("GET", f"{KEYS}?per_page=1", token="sign-read")
    assert (page.json(), 'rel="next"' in page.headers["link"]) == ([key], True)
    assert gh.get(f"{KEYS}/{key['id']}", token="sign-write") == key
    removed = gh.api("DELETE", f"{KEYS}/{key['id']}", token="sign-admin")
    assert (removed.status, removed.body) == (204, b"")
    assert gh.api("GET", f"{KEYS}/{key['id']}", token="sign-admin").status == 404
    assert gh.api("DELETE", f"{KEYS}/{key['id']}", token="sign-admin").status == 404
    assert gh.get(KEYS, token="sign-admin") == [untitled]


def test_keys_of_other_accounts_are_not_found(gh: Any, keys: dict[str, Ed25519PrivateKey]) -> None:
    (seeded,) = gh.get(KEYS, token="other-admin")
    assert (seeded["key"], seeded["title"]) == (key_line(keys["other"]), "")
    assert gh.get(KEYS, token="sign-admin") == []
    assert gh.api("GET", f"{KEYS}/{seeded['id']}", token="sign-admin").status == 404
    assert gh.api("DELETE", f"{KEYS}/{seeded['id']}", token="sign-admin").status == 404
    assert gh.api("GET", f"{KEYS}/999999", token="sign-admin").status == 404
    assert gh.get(KEYS, token="other-admin") == [seeded]


def test_signing_key_validation(gh: Any, keys: dict[str, Ed25519PrivateKey]) -> None:
    def post(body: Any) -> Any:
        response = gh.api("POST", KEYS, body, token="sign-admin")
        assert response.status == 422
        return response.json()

    invalid = post({"key": "ssh-rsa AAAA"})
    assert (invalid["message"], invalid["errors"]) == (
        "Validation Failed",
        [
            {
                "resource": "SshSigningKey",
                "code": "custom",
                "field": "key",
                "message": "key is invalid. You must supply a key in OpenSSH public key format",
            }
        ],
    )
    assert post({"key": key_line(keys["other"], "again")})["errors"][0]["message"] == "key is already in use"
    assert post({"title": "t"})["message"] == 'Invalid request.\n\n"key" wasn\'t supplied.'
    assert post({"key": 5})["message"] == "Invalid request.\n\nFor 'properties/key', 5 is not a string."
    assert post({"key": "k", "title": 5})["message"] == "Invalid request.\n\nFor 'properties/title', 5 is not a string."
    assert gh.api("POST", KEYS, [1], token="sign-admin").status == 400
    assert gh.get(KEYS, token="sign-admin") == []


def test_signing_key_rights(gh: Any, keys: dict[str, Ed25519PrivateKey]) -> None:
    def refused(method: str, path: str, token: str, body: Any = None) -> tuple[int, str, str | None]:
        response = gh.api(method, path, body, token=token)
        return response.status, response.json()["message"], response.headers.get("x-accepted-oauth-scopes")

    seeded = gh.get(KEYS, token="other-admin")[0]["id"]
    new = {"key": key_line(keys["octo"])}
    assert refused("GET", KEYS, "repo-only") == (404, "Not Found", ALL)
    assert refused("GET", f"{KEYS}/{seeded}", "repo-only") == (404, "Not Found", ALL)
    assert refused("POST", KEYS, "repo-only", new) == (404, "Not Found", "write:ssh_signing_key, admin:ssh_signing_key")
    assert refused("POST", KEYS, "sign-read", new) == (404, "Not Found", "write:ssh_signing_key, admin:ssh_signing_key")
    assert refused("DELETE", f"{KEYS}/{seeded}", "sign-write") == (404, "Not Found", "admin:ssh_signing_key")
    app = (403, "Resource not accessible by integration", None)
    assert refused("GET", KEYS, "double-user-token") == app
    assert refused("POST", KEYS, "double-user-token", new) == app
    assert refused("DELETE", f"{KEYS}/{seeded}", "double-user-token") == app
    assert refused("POST", KEYS, "fine", new) == (403, "Resource not accessible by personal access token", None)
    # an OAuth App token follows its scopes like a classic token (KOR-65)
    assert refused("POST", KEYS, "oauth-repo", new) == (
        404,
        "Not Found",
        "write:ssh_signing_key, admin:ssh_signing_key",
    )
    assert gh.api("POST", KEYS, new, token="oauth-write").status == 201
    assert gh.api("GET", KEYS, token="unknown").status == 401
    assert gh.get(KEYS, token="other-admin") != []
    assert gh.state()["failures"] == []


# --- seeding --------------------------------------------------------------------------------------------


def test_seeded_signing_keys(keys: dict[str, Ed25519PrivateKey]) -> None:
    world = World()
    world.seed(
        {
            "users": [
                {
                    "login": "u",
                    "ssh_signing_keys": [key_line(keys["octo"]), {"key": key_line(keys["other"]), "title": "t"}],
                }
            ]
        }
    )
    listed = world.user_signing_keys("u")
    assert [(k.title, k.line, k.login) for k in listed] == [
        ("", key_line(keys["octo"]), "u"),
        ("t", key_line(keys["other"]), "u"),
    ]
    assert world.user_signing_keys("v") == []


@pytest.mark.parametrize(
    ("keys_seed", "message"),
    [
        (["ssh-ed25519 AAAA"], "key is invalid"),
        ([5], "ssh signing key must be an object"),
        ([{"title": "t"}], r"ssh signing key\.key must be a string"),
        ([{"key": "k", "x": 1}], "ssh signing key: unknown keys"),
        ("k", r"user\.ssh_signing_keys must be a list"),
        (["DUPLICATE", "DUPLICATE"], "key is already in use"),
    ],
)
def test_seeded_signing_key_refusals_leave_nothing_behind(
    keys: dict[str, Ed25519PrivateKey], keys_seed: Any, message: str
) -> None:
    line = key_line(keys["octo"])
    if isinstance(keys_seed, list):
        keys_seed = [line if item == "DUPLICATE" else item for item in keys_seed]
    world = World()
    world.seed({"users": [{"login": "a", "ssh_signing_keys": [key_line(keys["other"])]}]})
    with pytest.raises(SeedError, match=message):
        world.seed({"users": [{"login": "u", "ssh_signing_keys": keys_seed}]})
    assert sorted(world.users) == ["a"]
    assert [key.login for key in world.signing_keys.values()] == ["a"]


def test_verification_of_an_unsigned_commit() -> None:
    """`World.verify` of an unsigned commit (the server only asks for signed ones)."""
    sig = Signature("n", "e@example.invalid", 1)
    assert World().verify(Commit(EMPTY_TREE, (), sig, sig, "m")).reason == "unsigned"
