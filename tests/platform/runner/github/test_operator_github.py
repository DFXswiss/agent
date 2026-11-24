"""U-RUN-github (KOR-27, ADR-053): the operator's GitHub account. An approved user's `github.connect` stores the
operator's token at once (one response `done`, no device flow), every other user keeps the device flow; the token file
is checked at each read (missing, type, owner, mode, format, replaced); a refused token ends `failed`; the repository
list falls back to `GET /user/repos` for a token without the GitHub App; a whole run connect -> pull request works
with such a token. Real request frames, the hub kit and the GitHub double (personal tokens)."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from agent_guest.schema import SchemaSet
from agent_runner.credentials import cache
from agent_runner.github import access, operator
from agent_runner.github.executors import EXECUTORS
from agent_runner.github.op_prechecks import PRECHECKS
from agent_runner.operations.extensions import CheckFailed, OperationFailed
from agent_runner.requests.service import service_for

from ..credentials.kit import ALICE, BOB, Env, ck, github_body, kit_env, make_record, seal_request
from ..settle import settle as settle_tasks
from .conftest import REPO, GitHub, seed
from .run10_kit import R10, refused, run10
from .run10_kit import world as run10_world
from .test_executors import context_for, precheck_request

SCHEMAS = SchemaSet(Path(__file__).resolve().parents[4] / "contracts")
OPERATOR_TOKEN = "operator-double-token-1"  # not a secret: the double only compares it
OTHER_OPERATOR_TOKEN = "operator-double-token-2"
UNKNOWN_TOKEN = "operator-revoked-token-9"


def operator_world(extra_repos: int = 0) -> dict[str, Any]:
    """The conftest world plus the operator's account `op-account`: personal tokens, collaborator on `hello` (not on
    `second`), its own repository `op-account/tools` and `extra_repos` more of its own."""
    world = seed()
    world["users"] += [
        {
            "login": "op-account",
            "id": 501,
            "name": None,
            "email": None,
            "personal_tokens": [OPERATOR_TOKEN],
            "collaborator": [REPO, "octo-org/missing"],
        },
        {"login": "op-second", "id": 502, "name": None, "email": None, "personal_tokens": [OTHER_OPERATOR_TOKEN]},
    ]
    world["repositories"] += [{"full_name": "op-account/tools", "branches": [{"name": "main", "commits": [{}]}]}]
    world["repositories"] += [
        {"full_name": f"op-account/r{n:03d}", "private": False, "branches": [{"name": "main", "commits": [{}]}]}
        for n in range(extra_repos)
    ]
    return world


@pytest.fixture
def opgh(start_github: Callable[..., GitHub]) -> GitHub:
    return start_github(world=operator_world())


def write_token(path: Path, text: str | bytes, mode: int = 0o600) -> None:
    """Replace `path` atomically, as the runbook does."""
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(text.encode() if isinstance(text, str) else text)
    tmp.chmod(mode)
    os.replace(tmp, path)


async def settle(env: Env) -> None:
    service = service_for(env.context)
    assert service is not None
    await settle_tasks(lambda: [f.task for f in service.flows.values()], "the request flows")


async def connect(env: Env, *, user: str = "alice", signer: str | None = None) -> str:
    service = service_for(env.context)
    assert service is not None
    request_id = env.hub.add_request(seal_request("github.connect", {}, user=user, signer=signer))
    await service.process(request_id)
    await settle(env)
    return request_id


def run(
    dsn: str,
    github: GitHub,
    token_file: Path | None,
    users: frozenset[str],
    body: Callable[[Env], Awaitable[None]],
) -> None:
    async def main() -> None:
        async with kit_env(dsn, github_url=github.url, github_wall=github.clock) as env:
            env.context.config.operator_github_token_file = token_file
            env.context.config.operator_github_users = users
            await env.add_ck()
            await body(env)

    asyncio.run(main())
    assert github.failures() == []


def test_an_approved_user_is_connected_at_once_with_the_operators_token(
    store_dsn: str, opgh: GitHub, tmp_path: Path
) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN + "\n")

    async def body(env: Env) -> None:
        env.context.config.github_client_id = None  # as on the platform device: no GitHub App registered
        request_id = await connect(env)
        assert env.hub.bodies(request_id) == [
            {"step": "done", "login": "op-account", "scopes": ["repo"], "source": "token"}
        ]
        assert [h["final"] for h, _b in env.hub.responses(request_id)] == [True]
        for response in env.hub.bodies(request_id):
            assert SCHEMAS.errors("requests/github.connect.response.json", response) == []
        async with cache.opened(env.context, ALICE, "github") as stored:
            assert stored["token"] == OPERATOR_TOKEN
            assert (stored["login"], stored["user_id"], stored["scopes"]) == ("op-account", 501, ["repo"])
        assert env.hub.records[(ALICE, "github")]["header"]["author"] == env.context.identity.device.device_id
        assert opgh.paths() == ["GET /user"]
        assert f"github_connect done request_id={request_id} source=operator" in env.log()
        assert OPERATOR_TOKEN not in env.log()
        assert OPERATOR_TOKEN not in json.dumps(env.hub.bodies(request_id))
        state = operator.state_for(env.context)
        assert (state.connects, state.failures) == (1, 0)

    run(store_dsn, opgh, token_file, frozenset({ALICE}), body)


def test_a_user_who_is_not_approved_keeps_the_device_flow(store_dsn: str, opgh: GitHub, tmp_path: Path) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN)

    async def body(env: Env) -> None:
        def on_sleep(_seconds: float) -> None:
            if len(env.clock.sleeps) == 1:
                opgh.decide("deny")

        env.clock.on_sleep = on_sleep
        request_id = await connect(env)
        bodies = env.hub.bodies(request_id)
        assert bodies[0]["step"] == "device_code"
        assert bodies[-1] == {"step": "failed", "code": "upstream_error"}
        assert "GET /user" not in opgh.paths()
        assert opgh.paths()[0] == "POST /login/device/code"
        assert "source=operator" not in env.log()
        assert (ALICE, "github") not in env.hub.records
        assert operator.state_for(env.context).connects == 0

    run(store_dsn, opgh, token_file, frozenset({BOB}), body)


def test_an_org_device_never_uses_the_file_even_with_the_settings(store_dsn: str, opgh: GitHub, tmp_path: Path) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN)

    async def body(env: Env) -> None:
        identity = env.context.identity
        env.context.identity = replace(identity, device=replace(identity.device, kind="org"))
        assert not operator.approved(env.context, ALICE)
        env.context.identity = identity
        assert operator.approved(env.context, ALICE.upper())
        assert not operator.approved(env.context, BOB)

    run(store_dsn, opgh, token_file, frozenset({ALICE}), body)


def test_a_forged_request_for_an_approved_user_never_reads_the_file(
    store_dsn: str, opgh: GitHub, tmp_path: Path
) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN)

    async def body(env: Env) -> None:
        # the header names the approved alice, the signature is bob's: refused before any handler runs
        request_id = await connect(env, user="alice", signer="bob")
        assert env.hub.bodies(request_id) == []
        assert opgh.paths() == []
        assert (ALICE, "github") not in env.hub.records
        assert "github_connect" not in env.log()

    run(store_dsn, opgh, token_file, frozenset({ALICE}), body)


def test_a_missing_file_fails_logged_once_and_a_later_file_connects(
    store_dsn: str, opgh: GitHub, tmp_path: Path
) -> None:
    token_file = tmp_path / "github.token"

    async def body(env: Env) -> None:
        first = await connect(env)
        second = await connect(env)
        for request_id in (first, second):
            assert env.hub.bodies(request_id) == [{"step": "failed", "code": "credential_missing"}]
        assert env.log().count("operator_github file_unusable reason=missing") == 1
        assert (
            f"github_connect failed request_id={first} code=credential_missing source=operator "
            "step=operator_file reason=missing"
        ) in env.log()
        assert opgh.paths() == []
        write_token(token_file, OPERATOR_TOKEN)
        third = await connect(env)
        assert env.hub.bodies(third) == [{"step": "done", "login": "op-account", "scopes": ["repo"], "source": "token"}]
        token_file.unlink()
        await connect(env)
        assert env.log().count("operator_github file_unusable reason=missing") == 2
        state = operator.state_for(env.context)
        assert (state.connects, state.failures) == (1, 3)

    run(store_dsn, opgh, token_file, frozenset({ALICE}), body)


def test_a_file_replaced_between_connects_is_read_anew(store_dsn: str, opgh: GitHub, tmp_path: Path) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN)

    async def body(env: Env) -> None:
        assert env.hub.bodies(await connect(env)) == [
            {"step": "done", "login": "op-account", "scopes": ["repo"], "source": "token"}
        ]
        write_token(token_file, OTHER_OPERATOR_TOKEN + "\n")
        assert env.hub.bodies(await connect(env)) == [
            {"step": "done", "login": "op-second", "scopes": ["repo"], "source": "token"}
        ]
        async with cache.opened(env.context, ALICE, "github") as stored:
            assert (stored["token"], stored["login"]) == (OTHER_OPERATOR_TOKEN, "op-second")

    run(store_dsn, opgh, token_file, frozenset({ALICE}), body)


def test_a_refused_token_ends_failed_without_the_token_anywhere(store_dsn: str, opgh: GitHub, tmp_path: Path) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, UNKNOWN_TOKEN)

    async def body(env: Env) -> None:
        request_id = await connect(env)
        assert env.hub.bodies(request_id) == [{"step": "failed", "code": "upstream_error"}]
        assert (
            f"github_connect failed request_id={request_id} code=upstream_error source=operator "
            "step=user kind=unauthorized status=401"
        ) in env.log()
        assert UNKNOWN_TOKEN not in env.log()
        assert (ALICE, "github") not in env.hub.records
        assert operator.state_for(env.context).failures == 1

    run(store_dsn, opgh, token_file, frozenset({ALICE}), body)


def test_an_approved_user_without_a_ck_ends_credential_missing(store_dsn: str, opgh: GitHub, tmp_path: Path) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN)

    async def main() -> None:
        async with kit_env(store_dsn, github_url=opgh.url, github_wall=opgh.clock) as env:
            env.context.config.operator_github_token_file = token_file
            env.context.config.operator_github_users = frozenset({ALICE})
            request_id = await connect(env)
            assert env.hub.bodies(request_id) == [{"step": "failed", "code": "credential_missing"}]
            assert "github_connect store_failed reason=key_missing" in env.log()

    asyncio.run(main())


# --- the token file ---------------------------------------------------------------------------------------------


def lines() -> tuple[list[str], Callable[[str], None]]:
    seen: list[str] = []
    return seen, seen.append


@pytest.mark.parametrize(
    ("content", "mode", "reason"),
    [
        (OPERATOR_TOKEN, 0o644, "mode"),
        (OPERATOR_TOKEN, 0o640, "mode"),
        (OPERATOR_TOKEN + "\n" + OPERATOR_TOKEN, 0o600, "format"),
        (OPERATOR_TOKEN + "\n\n", 0o600, "format"),
        ("", 0o600, "format"),
        ("\n", 0o600, "format"),
        ("tok en", 0o600, "format"),
        ("x" * 4097, 0o600, "format"),
        ("tökenä".encode(), 0o600, "format"),
        (OPERATOR_TOKEN, 0o000, "unreadable"),
    ],
)
def test_a_file_that_does_not_fit_is_refused_with_its_reason(
    tmp_path: Path, content: str | bytes, mode: int, reason: str
) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, content, mode)
    seen, line = lines()
    with pytest.raises(operator.Unusable) as refused:
        operator.read_token(token_file, operator.OperatorGitHub(), line)
    assert refused.value.reason == reason
    assert seen == [f"operator_github file_unusable reason={reason}"]


def test_the_longest_token_and_a_line_feed_fit(tmp_path: Path) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, "x" * 4096 + "\n")
    assert operator.read_token(token_file, operator.OperatorGitHub(), lines()[1]) == "x" * 4096


def test_a_symlink_a_directory_and_another_owner_are_never_read(tmp_path: Path) -> None:
    real = tmp_path / "real.token"
    write_token(real, OPERATOR_TOKEN)
    link = tmp_path / "link.token"
    link.symlink_to(real)
    seen, line = lines()
    state = operator.OperatorGitHub()
    for path, reason in ((link, "not_regular"), (tmp_path, "not_regular")):
        with pytest.raises(operator.Unusable) as refused:
            operator.read_token(path, state, line)
        assert refused.value.reason == reason
    other = operator.OperatorGitHub(euid=lambda: os.geteuid() + 1)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(operator, "_check_directory", lambda _path, _euid: None)  # a directory the other uid owns
        with pytest.raises(operator.Unusable) as refused:
            operator.read_token(real, other, line)
    assert refused.value.reason == "owner"
    # the same reason twice in a row is logged once
    assert seen == ["operator_github file_unusable reason=not_regular", "operator_github file_unusable reason=owner"]


def test_the_directory_must_be_the_runner_accounts_and_closed_to_others(tmp_path: Path) -> None:
    directory = tmp_path / "operator-github"
    directory.mkdir(mode=0o700)
    token_file = directory / "github.token"
    write_token(token_file, OPERATOR_TOKEN)
    assert operator.read_token(token_file, operator.OperatorGitHub(), lines()[1]) == OPERATOR_TOKEN
    seen, line = lines()
    for mode in (0o770, 0o702):
        directory.chmod(mode)
        with pytest.raises(operator.Unusable) as refused:
            operator.read_token(token_file, operator.OperatorGitHub(), line)
        assert refused.value.reason == "directory"
    directory.chmod(0o755)  # readable by others, writable by nobody else: fine
    assert operator.read_token(token_file, operator.OperatorGitHub(), line) == OPERATOR_TOKEN
    other = operator.OperatorGitHub(euid=lambda: os.geteuid() + 1)
    with pytest.raises(operator.Unusable) as refused:
        operator.read_token(token_file, other, line)
    assert refused.value.reason == "directory"
    link = tmp_path / "linked-dir"
    link.symlink_to(directory)
    with pytest.raises(operator.Unusable) as refused:
        operator.read_token(link / "github.token", operator.OperatorGitHub(), line)
    assert refused.value.reason == "directory"
    with pytest.raises(operator.Unusable) as refused:
        operator.read_token(tmp_path / "gone" / "github.token", operator.OperatorGitHub(), line)
    assert refused.value.reason == "missing"
    directory.chmod(0o700)
    assert seen == ["operator_github file_unusable reason=directory"] * 4 + [
        "operator_github file_unusable reason=missing"
    ]


def test_a_file_replaced_by_another_between_check_and_open_is_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN)
    loose = tmp_path / "loose"
    write_token(loose, OPERATOR_TOKEN, 0o644)
    monkeypatch.setattr(os, "fstat", lambda _fd: loose.stat())
    with pytest.raises(operator.Unusable) as refused:
        operator.read_token(token_file, operator.OperatorGitHub(), lines()[1])
    assert refused.value.reason == "mode"


def test_a_read_error_is_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN)

    def broken(_fd: int, _size: int) -> bytes:
        raise OSError("io")

    monkeypatch.setattr(os, "read", broken)
    with pytest.raises(operator.Unusable) as refused:
        operator.read_token(token_file, operator.OperatorGitHub(), lines()[1])
    assert refused.value.reason == "unreadable"


def test_the_state_is_one_per_daemon() -> None:
    class Context:
        pass

    context = Context()
    assert operator.state_for(context) is operator.state_for(context)


def test_the_doctor_check_names_the_state_of_the_file(tmp_path: Path) -> None:
    class Config:
        operator_github_token_file: Path | None = None

    config = Config()
    assert operator.check(config) == ("ok", "off")
    config.operator_github_token_file = tmp_path / "github.token"
    assert operator.check(config) == ("warn", "missing")
    write_token(config.operator_github_token_file, OPERATOR_TOKEN)
    assert operator.check(config) == ("ok", "usable")
    assert operator.check(config, euid=lambda: os.geteuid() + 1) == ("warn", "directory")


# --- operations with a token without the GitHub App ------------------------------------------------------------


def test_the_repository_list_falls_back_to_the_accounts_repositories_with_pages(
    store_dsn: str, start_github: Callable[..., GitHub], tmp_path: Path
) -> None:
    github = start_github(world=operator_world(extra_repos=100))
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN)

    async def body(env: Env) -> None:
        assert env.hub.bodies(await connect(env)) == [
            {"step": "done", "login": "op-account", "scopes": ["repo"], "source": "token"}
        ]
        data = {"allowed": [REPO, "op-account/tools"]}
        result = await EXECUTORS["github.list_repositories"](context_for(env, {}, data=data))
        assert SCHEMAS.errors("ops/github.list_repositories.result.json", result) == []
        listed = result["repositories"]
        assert len(listed) == 102
        assert listed[0] == {"repo": REPO, "default_branch": "main", "private": True, "allowed": True}
        assert listed[1]["repo"] == "op-account/r000"
        assert listed[-1] == {"repo": "op-account/tools", "default_branch": "main", "private": True, "allowed": True}
        assert [entry["repo"] for entry in listed if entry["allowed"]] == [REPO, "op-account/tools"]
        journal = github.journal()
        assert journal[1:3] == ["GET /user/installations?per_page=100", "GET /user/repos?per_page=100"]
        assert journal[3].startswith("GET /user/repos?")
        assert "page=2" in journal[3]
        assert len(journal) == 4

    run(store_dsn, github, token_file, frozenset({ALICE}), body)


def test_other_failures_of_the_installation_list_are_not_a_fallback(
    store_dsn: str, opgh: GitHub, tmp_path: Path
) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN)

    async def body(env: Env) -> None:
        assert env.hub.bodies(await connect(env)) == [
            {"step": "done", "login": "op-account", "scopes": ["repo"], "source": "token"}
        ]
        opgh.faults({"route": "GET /user/installations", "kind": "error", "status": 503, "times": 1})
        with pytest.raises(OperationFailed) as failed:
            await EXECUTORS["github.list_repositories"](context_for(env, {}))
        assert (failed.value.code, failed.value.retryable) == ("upstream_error", True)
        assert "GET /user/repos" not in opgh.paths()

    run(store_dsn, opgh, token_file, frozenset({ALICE}), body)


def test_connect_then_read_and_write_a_pull_request_with_the_operators_token(
    store_dsn: str, opgh: GitHub, tmp_path: Path
) -> None:
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN)

    async def body(env: Env) -> None:
        assert env.hub.bodies(await connect(env)) == [
            {"step": "done", "login": "op-account", "scopes": ["repo"], "source": "token"}
        ]
        for op_type in ("github.get_pull_request", "github.create_branch", "github.comment"):
            await PRECHECKS[op_type](precheck_request(env, op_type, {"repo": REPO}))
        # the repository exists, but the account is no collaborator of it: not accessible, as for an App user token
        with pytest.raises(CheckFailed) as hidden:
            await PRECHECKS["github.comment"](precheck_request(env, "github.comment", {"repo": "octo-org/second"}))
        assert hidden.value.code == "repo_not_accessible"
        pull = await EXECUTORS["github.get_pull_request"](context_for(env, {"repo": REPO, "number": 1}))
        assert SCHEMAS.errors("ops/github.get_pull_request.result.json", pull) == []
        assert (pull["number"], pull["title"], pull["head"]["ref"]) == (1, "Feature", "feature")
        assert [(c["author"], c["body"]) for c in pull["comments"]] == [("other-user", "Looks good.")]
        branch = await EXECUTORS["github.create_branch"](
            context_for(env, {"repo": REPO, "branch": "agent-x", "from": "main"})
        )
        assert branch["branch"] == "agent-x"
        params = {"repo": REPO, "head": "topic", "base": "main", "title": "Work", "body": "Done.", "draft": True}
        created = await EXECUTORS["github.create_pull_request"](context_for(env, params))
        assert created["draft"] is True
        comment = await EXECUTORS["github.comment"](
            context_for(env, {"repo": REPO, "number": created["number"], "body": "Ready."})
        )
        assert comment["url"].startswith(f"https://github.example/{REPO}/pull/{created['number']}#issuecomment-")
        assert OPERATOR_TOKEN not in env.log()

    run(store_dsn, opgh, token_file, frozenset({ALICE}), body)


def test_checkout_commit_and_push_with_the_operators_token(
    store_dsn: str, start_github: Callable[..., GitHub], tmp_path: Path
) -> None:
    data = run10_world()
    data["users"] += operator_world()["users"][-2:]
    github = start_github(world=data)

    async def body(r: R10) -> None:
        sid = await r.session()
        assert (await r.run(sid, "github.checkout", {"repo": REPO})).state == "succeeded"
        root = r.guest.work(sid) / "repos" / "octo-org" / "hello"
        (root / "README.md").write_text("operator\n")
        made = await r.run(sid, "github.commit", {"repo": REPO, "branch": "work", "message": "M."})
        assert made.result is not None, made.answer
        sha = made.result["commit_sha"]
        pushed = await r.run(sid, "github.push", {"repo": REPO, "branch": "work", "commit_sha": sha})
        assert pushed.state == "succeeded", pushed.answer
        repo = github.double.world.repo("octo-org", "hello")
        assert repo is not None
        assert repo.refs["refs/heads/work"] == sha
        assert all(not line.startswith("GET /user/installations") for line in r.journal())
        # reachable for the account, but not on the workgroup's allow-list: refused before GitHub is asked
        start = len(r.journal())
        outside = await r.run(sid, "github.checkout", {"repo": "op-account/tools"})
        assert refused(outside) == "repo_not_allowed"
        assert r.journal()[start:] == []

    run10(store_dsn, github, tmp_path, body, token=OPERATOR_TOKEN)


@pytest.mark.parametrize("allowed", [None, ()], ids=["no-settings", "empty-list"])
def test_without_an_allow_list_every_repository_of_the_account_works(
    store_dsn: str, start_github: Callable[..., GitHub], tmp_path: Path, allowed: tuple[str, ...] | None
) -> None:
    """KOR-30: no list in the workgroup settings allows every repository the connection reaches - listed `allowed`,
    then checkout, commit, push, pull request and comment on the account's own repository, each registered at the hub
    with its type; a repository the account cannot reach is refused by GitHub (`repo_not_accessible`)."""
    data = run10_world()
    data["users"] += [user for user in operator_world()["users"] if user["login"].startswith("op-")]
    data["repositories"] += [{"full_name": "op-account/tools", "branches": [{"name": "main", "commits": [{}]}]}]
    github = start_github(world=data)
    tools = "op-account/tools"

    async def body(r: R10) -> None:
        sid = await r.session()
        listed = await r.run(sid, "github.list_repositories", {})
        assert listed.result is not None, listed.answer
        entries = {entry["repo"]: entry["allowed"] for entry in listed.result["repositories"]}
        assert entries[tools] is True
        assert all(entries.values())
        assert (await r.run(sid, "github.checkout", {"repo": tools})).state == "succeeded"
        root = r.guest.work(sid) / "repos" / "op-account" / "tools"
        (root / "NOTES.md").write_text("no allow-list\n")
        made = await r.run(sid, "github.commit", {"repo": tools, "branch": "work", "message": "M."})
        assert made.result is not None, made.answer
        sha = made.result["commit_sha"]
        pushed = await r.run(sid, "github.push", {"repo": tools, "branch": "work", "commit_sha": sha})
        assert pushed.state == "succeeded", pushed.answer
        params = {"repo": tools, "head": "work", "base": "main", "title": "Notes", "body": ""}
        created = await r.run(sid, "github.create_pull_request", params)
        assert created.result is not None, created.answer
        number = created.result["number"]
        commented = await r.run(sid, "github.comment", {"repo": tools, "number": number, "body": "Ready."})
        assert commented.state == "succeeded", commented.answer
        registered = [request["type"] for request in r.ok.hub.requests]
        assert registered == [
            "github.list_repositories",
            "github.checkout",
            "github.commit",
            "github.push",
            "github.create_pull_request",
            "github.comment",
        ]
        # each registration (the audit of the operation) names its repository
        assert [r.ok.hub.params_event(index)[1]["params"]["repo"] for index in range(1, 6)] == [tools] * 5
        # not reachable for the account: GitHub refuses it in the pre-check, the session gets a clear code
        outside = await r.run(sid, "github.checkout", {"repo": "octo-org/second"})
        assert refused(outside) == "repo_not_accessible"
        assert outside.answer is not None
        assert outside.answer["error"]["message"] == access.NOT_ACCESSIBLE
        rejected = r.ok.hub.requests[-1]
        assert (len(r.ok.hub.requests), rejected["type"], rejected["local_check"]) == (7, "github.checkout", "rejected")
        assert r.ok.hub.params_event()[1]["params"]["repo"] == "octo-org/second"

    run10(store_dsn, github, tmp_path, body, token=OPERATOR_TOKEN, allowed=allowed)


def test_a_copy_of_the_operators_token_held_by_another_user_is_never_used(
    store_dsn: str, opgh: GitHub, tmp_path: Path
) -> None:
    """HOT-14: alice is the operator's one user; bob still holds the copy stored while several users could be
    approved. Bob's operations never use it (no request reaches GitHub); alice's do."""
    token_file = tmp_path / "github.token"
    write_token(token_file, OPERATOR_TOKEN + "\n")

    async def body(env: Env) -> None:
        env.hub.records[(ALICE, "github")] = make_record(github_body(token=OPERATOR_TOKEN))
        env.hub.records[(BOB, "github")] = make_record(
            github_body(token=OPERATOR_TOKEN), owner=BOB, author="bob", key=ck(BOB)
        )
        await env.add_ck(BOB)
        with pytest.raises(OperationFailed) as failed:
            await EXECUTORS["github.list_repositories"](context_for(env, {}, owner=BOB))
        assert failed.value.code == "github_not_connected"
        assert await refused_check(env, BOB) == "github_not_connected"
        assert opgh.journal() == []
        listed = await EXECUTORS["github.list_repositories"](context_for(env, {}, data={"allowed": []}))
        assert "repositories" in listed
        # without the operator's file there is nothing to compare with (the runbook revokes the token at GitHub)
        assert not operator.foreign_copy(env.context, BOB, "another-token")
        token_file.unlink()
        assert not operator.foreign_copy(env.context, BOB, OPERATOR_TOKEN)
        env.context.config.operator_github_token_file = None
        assert not operator.foreign_copy(env.context, BOB, OPERATOR_TOKEN)

    run(store_dsn, opgh, token_file, frozenset({ALICE}), body)


async def refused_check(env: Env, user: str) -> str:
    with pytest.raises(CheckFailed) as failed:
        await PRECHECKS["github.list_repositories"](precheck_request(env, "github.list_repositories", {}, owner=user))
    return failed.value.code
