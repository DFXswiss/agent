"""U-RUN-github: a personal connection is never used on behalf of another person (HOT-14, ADR-070, KONZEPT A15 and
E-19; 05-runner.md section 12 step 3).

The class, not the single case:

- **One place** (`credentials/acting.py`): no module of the runner opens a personal credential kind for an operation
  anywhere else and no GitHub or operation module reads the session's owner (an AST scan of the source), and every
  registered `github.*` pre-check and executor refuses an operation without an acting user - while the task owner is
  connected - without a single GitHub request.
- **Against the GitHub double** (the scenario of the package): the task owner (alice, connected) owns the task; bob
  (no GitHub, write access) gives the instruction of the turn and asks for a GitHub operation -> held, the `waiting`
  card names bob, GitHub sees no request with alice's token; bob connects his own account -> the operation runs with
  bob's token; alice instructs the next turn -> alice's token. A turn whose acting user is unknown uses nobody's
  connection and is not held.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import agent_runner
from agent_runner.credentials import acting, cache
from agent_runner.crypto.ids import uuid7
from agent_runner.github.client import GitHubOptions
from agent_runner.github.executors import EXECUTORS
from agent_runner.github.op_prechecks import PRECHECKS
from agent_runner.operations.extensions import CheckFailed, OperationContext, OperationFailed, PrecheckRequest
from agent_runner.operations.pipeline import install
from agent_runner.operations.settings import DEFAULTS
from agent_runner.orgs.db import session
from agent_runner.orgs.keys import store_scope_key

from ..credentials.kit import ALICE, BOB, CLIENT_ID, Env, ck, github_body, make_record
from ..operations.kit import OKit, setup
from ..reconcile.rig import Rig, standard_world
from .conftest import OTHER_TOKEN, REPO, TOKEN, GitHub, seed
from .test_executors import with_env

SOURCE = Path(agent_runner.__file__).parent
ONE_PLACE = SOURCE / "credentials" / "acting.py"
OPENERS = frozenset({"opened", "cached", "fetch"})
"""Functions of `credentials.cache` that read a credential of an owner."""
OWNER_READERS = ("github", "operations")
"""Areas that act for an operation: they never read the session's owner (the acting user decides)."""
HARNESS = SOURCE / "sessions" / "harness.py"
"""The session's harness line names the login of the task owner's connection at the start (a fact for the agent, no
token is used and no operation reads it); the only reader of a personal kind outside the one place."""


def _kind_literals(call: ast.Call) -> set[str]:
    values = [*call.args, *(keyword.value for keyword in call.keywords)]
    return {value.value for value in values if isinstance(value, ast.Constant) and isinstance(value.value, str)}


def _called(call: ast.Call) -> str | None:
    """The called function's name; None for the one place itself (`acting.opened`)."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return None if isinstance(func.value, ast.Name) and func.value.id == "acting" else func.attr
    return func.id if isinstance(func, ast.Name) else None


def test_only_the_one_place_opens_a_personal_connection() -> None:
    assert frozenset({"github"}) == acting.PERSONAL_KINDS
    found: list[str] = []
    for path in sorted(SOURCE.rglob("*.py")):
        if path in (ONE_PLACE, HARNESS) or "crypto" in path.relative_to(SOURCE).parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            if isinstance(node, ast.Call) and _called(node) in OPENERS and _kind_literals(node) & acting.PERSONAL_KINDS:
                found.append(f"{path.relative_to(SOURCE)}:{node.lineno}")
    assert found == []
    harness = [
        node.lineno
        for node in ast.walk(ast.parse(HARNESS.read_text()))
        if isinstance(node, ast.Call) and _called(node) in OPENERS and _kind_literals(node) & acting.PERSONAL_KINDS
    ]
    assert len(harness) == 2  # cached + opened of the login for the harness line, nothing else


def test_no_github_or_operation_module_reads_the_session_owner() -> None:
    found: list[str] = []
    for area in OWNER_READERS:
        for path in sorted((SOURCE / area).rglob("*.py")):
            source = path.read_text()
            for node in ast.walk(ast.parse(source, str(path))):
                if isinstance(node, ast.Attribute) and node.attr == "owner_id":
                    found.append(f"{path.relative_to(SOURCE)}:{node.lineno}")
                if isinstance(node, ast.Name) and node.id == "owner_client":
                    found.append(f"{path.relative_to(SOURCE)}:{node.lineno}")
    assert found == []


def _operation(op_type: str, actor: str | None) -> Any:
    return SimpleNamespace(id=uuid7(), type=op_type, actor_id=actor, params={})


PARAMS: dict[str, Any] = {
    "repo": REPO,
    "number": 1,
    "branch": "work",
    "from": "main",
    "head": "work",
    "base": "main",
    "title": "T",
    "body": "B.",
    "message": "M.",
    "ref": "main",
    "commit_sha": "a" * 40,
    "state": "open",
}


def test_every_github_type_refuses_an_operation_without_acting_user(store_dsn: str, github: GitHub) -> None:
    """The owner is connected; the operation has no acting user: every pre-check and every executor refuses it with
    `github_not_connected` and GitHub sees no request (no token of the owner is ever sent)."""

    async def body(env: Env) -> None:
        settings = type(DEFAULTS)(repositories=frozenset({REPO}), protected_branches=(), pr_draft_default=True)
        assert set(EXECUTORS) == set(PRECHECKS)
        assert all(op_type.startswith("github.") and acting.needs(op_type) == "github" for op_type in EXECUTORS)
        owner_session = SimpleNamespace(id=uuid7(), owner_id=ALICE, vm_id=None, task_id=uuid7(), depth=0)
        for op_type in sorted(EXECUTORS):
            operation = _operation(op_type, None)
            request = PrecheckRequest(env.context, operation, owner_session, settings, PARAMS)  # type: ignore[arg-type]
            with pytest.raises(CheckFailed) as refused:
                await PRECHECKS[op_type](request)
            assert refused.value.code == "github_not_connected", op_type
            ctx = OperationContext(env.context, operation, owner_session, PARAMS, {}, 1, None)  # type: ignore[arg-type]
            with pytest.raises(OperationFailed) as failed:
                await EXECUTORS[op_type](ctx)
            assert failed.value.code == "github_not_connected", op_type
        assert github.journal() == []

    with_env(store_dsn, github, body)


def test_the_one_place_opens_only_the_acting_users_own_kind(store_dsn: str, github: GitHub) -> None:
    async def body(env: Env) -> None:
        with pytest.raises(ValueError, match="personal connection"):
            async with acting.opened(env.context, _operation("task.set_title", ALICE), "github"):
                pass
        with pytest.raises(ValueError, match="personal connection"):
            async with acting.opened(env.context, _operation("github.comment", ALICE), "claude"):
                pass
        with pytest.raises(cache.CredentialUnavailable) as missing:
            async with acting.opened(env.context, _operation("github.comment", None), "github"):
                pass
        assert missing.value.reason == "no_actor"
        async with acting.opened(env.context, _operation("github.comment", ALICE), "github") as found:
            assert found["token"] == TOKEN
        async with session(env.pool) as conn:
            assert await acting.cached_version(conn, _operation("github.comment", ALICE), "github") == 1
            assert await acting.cached_version(conn, _operation("github.comment", BOB), "github") is None
            assert await acting.cached_version(conn, _operation("github.comment", None), "github") is None
        assert acting.needs("task.set_title") is None

    with_env(store_dsn, github, body)


# -- the scenario against the GitHub double ---------------------------------------------------------------------------


def two_people(dsn: str, github: GitHub, body: Callable[[OKit, dict[str, Any]], Awaitable[None]]) -> None:
    """A pipeline in the daemon context, the `CK` of alice and bob on the device; the hub serves the `github` records
    put into the returned mapping (by owner, `404` while absent). Alice is the task owner."""

    async def main() -> None:
        async with Rig(dsn).env(standard_world()) as env:
            env.context.config.github_client_id = CLIENT_ID
            env.context.config.github_api_url = github.url
            env.context.config.github_web_url = github.url
            env.context.github_options = GitHubOptions(wall=github.clock)
            found = await setup(env, executors=EXECUTORS, prechecks=PRECHECKS)
            await found.settings([REPO])
            async with session(env.pool) as conn:
                for owner in (ALICE, BOB):
                    await store_scope_key(conn, env.sync.storage_key, ("cred", owner, 1), ck(owner), source="wrap")
            install(env.context, found.pipeline)
            served: dict[str, Any] = {}
            original = env.hub.route

            def route(method: str, target: str, data: Any) -> tuple[int, dict[str, Any]]:
                for owner in (ALICE, BOB):
                    if (method, target) == ("GET", f"/v1/device/credentials/{owner}/github"):
                        return (200, {"record": served[owner]}) if owner in served else (404, {"error": "not_found"})
                return original(method, target, data)

            env.hub.route = route  # type: ignore[method-assign,assignment]
            await body(found, served)

    asyncio.run(main())
    assert github.failures() == []


async def connect(kit: OKit, served: dict[str, Any], owner: str) -> None:
    """`owner` connected GitHub: the hub holds the record and the device fetches it (hint `credentials`)."""
    if owner == ALICE:
        served[owner] = make_record(github_body(token=TOKEN))
    else:
        served[owner] = make_record(
            github_body(token=OTHER_TOKEN, login="other-user"), owner=BOB, author="bob", key=ck(BOB)
        )
    await cache.fetch(kit.env.context, owner, "github")


def callers(github: GitHub) -> list[str]:
    """Whose token each GitHub request carried (the double's journal redacts tokens to their length)."""
    lengths = {f"Bearer <redacted {len(TOKEN)} chars>": "alice", f"Bearer <redacted {len(OTHER_TOKEN)} chars>": "bob"}
    assert len(TOKEN) != len(OTHER_TOKEN)
    found = []
    for record in github.double.journal.records():
        headers = {name.lower(): value for name, value in record["headers"].items()}
        found.append(lengths.get(headers.get("authorization", ""), "nobody"))
    return found


async def results(kit: OKit) -> list[tuple[str, str | None]]:
    """`(operation_id, connection_of)` of every `operation.result` event in the outbox."""
    bodies = [item["body"] for kind, item in await kit.outbox() if kind == "event"]
    return [(body["operation_id"], body.get("connection_of")) for body in bodies if "status" in body]


async def turn(kit: OKit, sid: str, actor: str | None) -> None:
    async with kit.env.pool.connection() as conn:
        await conn.execute("UPDATE runner.session SET turn_actor = %s, turn_open = true WHERE id = %s", (actor, sid))


def test_a_shared_task_uses_the_github_of_whoever_gave_the_instruction(
    store_dsn: str, start_github: Callable[..., GitHub]
) -> None:
    world = seed()
    world["installations"][0]["users"].append("other-user")  # bob's account reaches the repository too
    github = start_github(world=world)

    async def body(kit: OKit, served: dict[str, Any]) -> None:
        await connect(kit, served, ALICE)
        sid = await kit.session(actor=BOB)  # alice's task, bob wrote the prompt of this turn
        op_id = await kit.request(sid, "github.comment", {"repo": REPO, "number": 1, "body": "From bob."})
        await kit.pipeline.process(op_id)
        held = await kit.op(op_id)
        assert (held.state, held.held, held.actor_id) == ("received", True, BOB)
        (card,) = [item["body"] for kind, item in await kit.outbox() if kind == "event"]
        assert (card["waiting_for"], card["for_user"]) == ("github_connection", BOB)
        assert github.journal() == []  # never a request with alice's token
        # bob connects his own account: the held operation runs with bob's token
        await connect(kit, served, BOB)
        await kit.pipeline.process(op_id)
        done = await kit.op(op_id)
        assert done.state == "succeeded"
        assert callers(github) == ["bob", "bob"]
        comments = [c for c in github.repo()["comments"] if c["body"] == "From bob."]
        assert [c["user"] for c in comments] == ["other-user"]
        assert await results(kit) == [(op_id, BOB)]  # the task's record names whose connection was used
        # alice instructs the next turn: alice's token
        await turn(kit, sid, ALICE)
        mine = await kit.run(sid, "github.comment", {"repo": REPO, "number": 1, "body": "From alice."})
        assert (mine.state, mine.actor_id) == ("succeeded", ALICE)
        assert callers(github) == ["bob", "bob", "alice", "alice"]
        assert await results(kit) == [(op_id, BOB), (mine.id, ALICE)]
        comments = [c for c in github.repo()["comments"] if c["body"] == "From alice."]
        assert [c["user"] for c in comments] == ["octo-user"]
        assert any(f"held operation_id={op_id}" in line for line in kit.lines)

    two_people(store_dsn, github, body)


def test_a_turn_without_one_acting_user_uses_nobodys_connection(store_dsn: str, github: GitHub) -> None:
    async def body(kit: OKit, served: dict[str, Any]) -> None:
        await connect(kit, served, ALICE)
        await connect(kit, served, BOB)
        sid = await kit.session(actor=None)  # prompts of alice and bob in one turn, or no known author
        kit.hub.decisions.append({"state": "rejected", "error_code": "github_not_connected"})
        op = await kit.run(sid, "github.comment", {"repo": REPO, "number": 1, "body": "Whose?"})
        assert (op.state, op.held_until, op.actor_id) == ("rejected", None, None)
        assert op.answer is not None
        assert op.answer["error"] == {"code": "github_not_connected", "message": acting.NO_ACTOR}
        bodies = [item["body"] for kind, item in await kit.outbox() if kind == "event"]
        assert [body for body in bodies if body.get("state") == "waiting"] == []  # no card: nobody to ask
        assert {connection for _op, connection in await results(kit)} <= {None}  # nobody's connection used
        assert github.journal() == []
        assert any(f"no_acting_user operation_id={op.id}" in line for line in kit.lines)

    two_people(store_dsn, github, body)
