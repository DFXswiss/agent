"""U-RUN-github (KOR-47): the identity of `github.commit` (ADR-076 sections 4 and 5; 03-api.md section 9) through the
operation pipeline against the GitHub double: author and committer of identities the acting human may use (with
their noreply addresses and one runner time), co-authors as trailers, the identity's own SSH signature over exactly the
commit object GitHub stores (verified by the double, commit ID equal to the git object ID), and every refusal."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from agent_runner.credentials.cache import _store
from agent_runner.crypto.encoding import b64u_encode
from agent_runner.crypto.ids import uuid7
from agent_runner.github import commit, signing
from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import store_scope_key

from ..credentials.identity_kit import ORG_A, IdentityHub, deliver, identity_body, operation_of
from ..credentials.kit import ALICE, BOB, ck, make_record, pid
from ..operations.kit import TASK
from .conftest import OTHER_TOKEN, REPO, TOKEN, GitHub
from .run10_kit import R10, refused, run10, world

SEED = bytes(range(32))
BOT_TOKEN = "double-bot-token"  # not a secret: the double only compares it
WHEN = 1_800_000_000
COMMIT = {"repo": REPO, "branch": "work", "message": "Change."}


def seeded() -> dict[str, Any]:
    """The RUN-10 world plus a company account `octo-bot` (103) and the signing key of `octo-user`."""
    data = copy.deepcopy(world())
    data["users"][0]["ssh_signing_keys"] = [signing.public_line(SEED, "agent")]
    data["users"].append({"login": "octo-bot", "id": 103, "name": None, "email": None, "tokens": [BOT_TOKEN]})
    return data


def signing_key(seed: bytes = SEED) -> dict[str, Any]:
    return {"alg": "ssh-ed25519", "seed": b64u_encode(seed), "public": signing.public_line(seed, "agent")}


class Kit:
    def __init__(self, r: R10) -> None:
        self.r = r
        self.env = r.ok.env
        self.hub = IdentityHub(self.env.hub).install()

    async def key(self, owner: str, epoch: int = 1) -> None:
        async with session(self.env.pool) as conn:
            await store_scope_key(
                conn, self.env.sync.storage_key, ("cred", owner, epoch), ck(owner, epoch), source="wrap"
            )

    async def legacy(self, owner: str, author: str, body: dict[str, Any]) -> None:
        async with session(self.env.pool) as conn:
            await _store(conn, owner, "github", 1, make_record(body, owner=owner, author=author))
        await self.key(owner)

    async def prompts(self, sid: str, *authors: str) -> None:
        for seq, author in enumerate(authors, start=1):
            await deliver(self.env.pool, author, seq, task_id=TASK, session_id=sid)

    async def checkout(self, sid: str) -> None:
        op = await self.r.run(sid, "github.checkout", {"repo": REPO})
        assert op.state == "succeeded", op.answer

    def change(self, sid: str) -> None:
        root = self.r.guest.work(sid) / "repos" / "octo-org" / "hello"
        (root / "README.md").write_text(f"changed {uuid7()}\n")


def double_commit(github: GitHub, sha: str) -> Any:
    found: Any = github.double.world.repo("octo-org", "hello")
    return found.objects.commit(sha)


def test_author_committer_and_two_co_authors(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=seeded())

    async def body(r: R10) -> None:
        kit = Kit(r)
        await kit.key(ORG_A)
        bot = kit.hub.add(identity_body(login="octo-bot", user_id=103, token=BOT_TOKEN), owner=ORG_A, kind="org")
        await kit.legacy(BOB, "bob", identity_body(login="other-user", user_id=102, token=OTHER_TOKEN))
        sid = await r.session()
        await kit.prompts(sid, BOB, ALICE)  # alice instructed last: she acts
        await kit.checkout(sid)
        kit.change(sid)
        params = {**COMMIT, "author": bot, "co_authors": [BOB, ALICE]}
        op = await r.run(sid, "github.commit", params)
        assert op.state == "succeeded", op.answer
        assert op.result is not None
        made = double_commit(github, op.result["commit_sha"])
        assert (made.author.name, made.author.email, made.author.time) == (
            "octo-bot",
            "103+octo-bot@users.noreply.github.com",
            WHEN,
        )
        assert (made.committer.name, made.committer.email, made.committer.time) == (
            "octo-user",
            "101+octo-user@users.noreply.github.com",
            WHEN,
        )
        assert made.message == (
            "Change.\n\n"
            "Co-authored-by: other-user <102+other-user@users.noreply.github.com>\n"
            "Co-authored-by: octo-user <101+octo-user@users.noreply.github.com>\n"
        )
        assert made.signature is None  # the acting identity holds no signing key
        registration = r.ok.hub.requests[-1]
        assert registration["triggered_by"] == ALICE
        assert registration["github_identity"] == op.checked["data"]["github_identity"]  # type: ignore[index]
        # the pre-check data keeps names and addresses, never a token or key
        assert TOKEN not in str(op.checked)
        assert BOT_TOKEN not in str(op.checked)

    run10(store_dsn, github, tmp_path, body)


def test_a_signing_key_signs_exactly_the_commit_github_stores(
    store_dsn: str, start_github: Any, tmp_path: Path
) -> None:
    github = start_github(world=seeded())

    async def body(r: R10) -> None:
        kit = Kit(r)
        await kit.key(ALICE)
        keyed = kit.hub.add(identity_body(signing_key=signing_key()), owner=ALICE)
        sid = await r.session()
        await kit.checkout(sid)
        kit.change(sid)
        op = await r.run(sid, "github.commit", {**COMMIT, "github_identity": keyed})
        assert op.state == "succeeded", op.answer
        assert op.result is not None
        sha = op.result["commit_sha"]
        made = double_commit(github, sha)
        assert made.signature is not None
        person = signing.Person("octo-user", "101+octo-user@users.noreply.github.com", WHEN)
        text = signing.commit_text(made.tree, list(made.parents), person, person, "Change.")
        assert signing.signed_commit_id(text, made.signature) == sha  # GitHub's ID is the git object ID
        verification = github.double.world.repo("octo-org", "hello").verification(sha)
        assert (verification.verified, verification.reason) == (True, "valid")
        # `none` leaves the commit unsigned although the identity has a key
        kit.change(sid)
        op = await r.run(sid, "github.commit", {**COMMIT, "github_identity": keyed, "signing": "none"})
        assert op.result is not None
        assert double_commit(github, op.result["commit_sha"]).signature is None

    run10(store_dsn, github, tmp_path, body)


def test_every_refusal_of_the_commit_identity(store_dsn: str, start_github: Any, tmp_path: Path) -> None:
    github = start_github(world=seeded())

    async def body(r: R10) -> None:
        kit = Kit(r)
        await kit.key(ORG_A)
        await kit.key(BOB)
        bobs = kit.hub.add(identity_body(login="other-user", user_id=102), owner=BOB, author="bob")
        no_login = kit.hub.add({"token": BOT_TOKEN}, owner=ORG_A, kind="org")
        no_key = kit.hub.add(identity_body(), owner=ORG_A, kind="org", epoch=7)
        by_member = kit.hub.add(identity_body(), owner=ORG_A, kind="org", author="bob")
        kit.hub.refusals[pid("hidden")] = (403, {"error": "forbidden"})
        sid = await r.session()
        await kit.prompts(sid, BOB, ALICE)
        await kit.checkout(sid)
        kit.change(sid)
        cases: list[tuple[dict[str, Any], str]] = [
            ({"author": bobs}, "identity_forbidden"),  # another person's personal account
            ({"committer": pid("hidden")}, "identity_forbidden"),
            ({"committer": pid("gone")}, "identity_unavailable"),
            ({"author": by_member}, "identity_unavailable"),
            ({"author": no_login}, "identity_unavailable"),
            ({"committer": no_key}, "identity_unavailable"),
            ({"co_authors": [pid("zed")]}, "identity_forbidden"),  # never instructed the task
            ({"co_authors": [BOB]}, "identity_unavailable"),  # instructed, but has no own primary identity here
            ({"signing": "key"}, "identity_unavailable"),  # the committer holds no key
        ]
        for extra, code in cases:
            op = await r.run(sid, "github.commit", {**COMMIT, **extra})
            assert refused(op) == code, extra
        # free text is refused by the formal checks; a trailer in the message too
        op = await r.run(sid, "github.commit", {**COMMIT, "author": "Octo <octo@example.com>"})
        assert refused(op) == "params_invalid"
        op = await r.run(sid, "github.commit", {**COMMIT, "message": "Fix.\n\nCo-authored-by: A Tool <t@example.com>"})
        assert refused(op) == "params_invalid"
        assert op.answer is not None
        assert op.answer["error"]["pointers"] == ["/params/message"]
        assert [path for path in github.paths() if "git/commits" in path and path.startswith("POST")] == []

    run10(store_dsn, github, tmp_path, body)


# --- the pieces -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Fix.", "Fix.\n\nCo-authored-by: a <1+a@x>\nCo-authored-by: b <2+b@x>\n"),
        ("Fix.\n", "Fix.\n\nCo-authored-by: a <1+a@x>\nCo-authored-by: b <2+b@x>\n"),
    ],
)
def test_full_message_puts_the_trailers_after_one_blank_line(message: str, expected: str) -> None:
    people = [{"name": "a", "email": "1+a@x"}, {"name": "b", "email": "2+b@x"}]
    assert commit.full_message(message, people) == expected
    assert commit.full_message(message, []) == message


def test_a_signing_key_that_is_gone_at_execution_is_identity_unavailable(
    store_dsn: str, start_github: Any, tmp_path: Path
) -> None:
    github = start_github(world=seeded())

    async def body(r: R10) -> None:
        kit = Kit(r)
        await kit.key(ALICE)
        plain = kit.hub.add(identity_body(), owner=ALICE)
        broken = kit.hub.add(identity_body(signing_key={**signing_key(), "seed": "short"}), owner=ALICE)
        for record_id in (plain, broken, pid("gone")):
            signer = {"github_identity": record_id, "identity_owner": ALICE, "identity_primary": False}
            nothing: Any = None
            ctx = OperationContext(r.ok.env.context, operation_of(ALICE, "github.commit"), nothing, {}, {}, 1, None)
            with pytest.raises(OperationFailed) as failed:
                await commit._seed(ctx, signer)
            assert failed.value.code == "identity_unavailable"

    run10(store_dsn, github, tmp_path, body)
