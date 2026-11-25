"""U-RUN-github (KOR-48, ADR-084 section 2): the device request `github.pull_status` and the `mergeable` re-read of
`github.get_pull_request`. Real request frames through the request service, the hub kit with the identity routes,
real PostgreSQL, the verified membership log of the reconcile rig and the GitHub double (asynchronous `mergeable`,
conflicts after the base moved, the `mergeable_unknown` fault).

- rights: a device not assigned to the workgroup, a requester who cannot read it, a known task of another workgroup
  are `forbidden`; a malformed body is `upstream_error`;
- identity: a writer reads with the ADR-078 resolution of the task (task choice of a known task, workgroup default,
  own primary record), a reader with the own primary record only; nothing usable is `github_not_connected`;
- per pull request: found with `mergeable` null -> value after a re-read, still null after 1 s and 2 s, a conflict,
  `not_found`, `repo_not_accessible`, `repo_not_allowed` (the workgroup's list), `upstream_error`; a refused token ends
  the request (`github_not_connected`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from agent_guest.schema import SchemaSet
from agent_runner.credentials import cache
from agent_runner.crypto.ids import uuid7
from agent_runner.crypto.log import vectors as v
from agent_runner.github import identity, ops, pull_status, pulls
from agent_runner.github.client import GitHubError, GitHubOptions
from agent_runner.github.executors import EXECUTORS
from agent_runner.github.requests import HANDLERS
from agent_runner.operations import settings
from agent_runner.operations.extensions import OperationFailed
from agent_runner.requests.service import service_for
from agent_runner.sync.http import HubTransportError

from ..credentials.identity_kit import ORG_A, TASK, WG_A, IdentityHub, choose, identity_body
from ..credentials.kit import ALICE, BOB, DEVICE, Env, kit_env, make_record, pid, seal_request, world_with_users
from ..reconcile.rig import CERT_SEQ, World, seal
from ..settle import settle as settle_tasks
from .conftest import OTHER_TOKEN, REPO, TOKEN, GitHub, seed
from .test_executors import context_for, result_ok

SCHEMAS = SchemaSet(Path(__file__).resolve().parents[4] / "contracts")
WG_ONE = pid("a/wg/one")
WG_TWO = pid("a/wg/two")
PULL_ROUTE = "GET /repos/{owner}/{repo}/pulls/{number}"
SSO_TOKEN = "double-sso-token"  # not a secret: the double only compares it
Body = Callable[[Env, IdentityHub], Awaitable[None]]


def world() -> World:
    """`world_with_users` (alice founds org `a`, bob reads workgroup one, the device serves the default workgroup)
    plus workgroup two (nobody but the admin alice reads it) and the device assigned to workgroups one and two."""
    found = world_with_users()
    log = found.orgs[ORG_A].log
    log.add("wg_create", "alice", wg=WG_TWO, epoch=1)
    log.add("device_assign", "alice", device=pid(DEVICE), wg=WG_ONE, platform_seq=CERT_SEQ)
    log.add("device_assign", "alice", device=pid(DEVICE), wg=WG_TWO, platform_seq=CERT_SEQ)
    return found


def github_world() -> dict[str, Any]:
    """The conftest world plus a user whose token is not authorized for the SAML organization `octo-org`."""
    found = seed()
    found["users"].append({"login": "sso-user", "id": 103, "tokens": [{"token": SSO_TOKEN, "sso": ["octo-org"]}]})
    return found


@pytest.fixture
def gh(start_github: Callable[..., GitHub]) -> GitHub:
    return start_github(world=github_world())


def run(dsn: str, github: GitHub, body: Body) -> None:
    async def main() -> None:
        async with kit_env(dsn, world(), github_url=github.url, github_wall=github.clock) as env:
            await body(env, IdentityHub(env.hub).install())

    asyncio.run(main())
    assert github.failures() == []


async def legacy(env: Env, owner: str = ALICE, author: str = "alice", **body: Any) -> str:
    """A primary record of `owner` at the hub (legacy endpoint); its record ID."""
    record = make_record(identity_body(**body), owner=owner, author=author)
    env.hub.records[(owner, "github")] = record
    await env.add_ck(owner=owner)
    return str(record["header"]["id"])


def request(*pulls_asked: tuple[str, int], wg: str = WG_A, **extra: Any) -> dict[str, Any]:
    return {"org_id": ORG_A, "wg_id": wg, "pulls": [{"repo": r, "number": n} for r, n in pulls_asked], **extra}


async def ask(env: Env, body: object, *, user: str = "alice") -> dict[str, Any]:
    """One `github.pull_status` of `user`; its only response (checked against the contract)."""
    service = service_for(env.context)
    assert service is not None
    request_id = env.hub.add_request(seal_request("github.pull_status", body, user=user))
    await service.process(request_id)
    await settle_tasks(lambda: [f.task for f in service.flows.values()], "the request flows")
    (answer,) = env.hub.bodies(request_id, user=user)
    assert SCHEMAS.errors("requests/github.pull_status.response.json", answer) == []
    return answer


async def known_task(env: Env, task_id: str = TASK, org_id: str = ORG_A, wg_id: str = WG_A) -> None:
    async with env.pool.connection() as conn:
        await conn.execute(
            "INSERT INTO runner.session (id, task_id, org_id, wg_id, vm_id, state) VALUES (%s, %s, %s, %s, NULL,"
            " 'running')",
            (uuid7(), task_id, org_id, wg_id),
        )


def found(number: int = 1, *, mergeable: bool | None = True, state: str = "clean", **extra: Any) -> dict[str, Any]:
    return {"repo": REPO, "number": number, "found": True, "mergeable": mergeable, "mergeable_state": state, **extra}


def subset(answer: dict[str, Any]) -> list[dict[str, Any]]:
    """Per pull request the fields `found` names."""
    keys = ("repo", "number", "found", "mergeable", "mergeable_state", "code")
    return [{key: item[key] for key in keys if key in item} for item in answer["pulls"]]


def test_the_handler_is_registered() -> None:
    assert HANDLERS["github.pull_status"] is pull_status.pull_status


# --- body and rights --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"org_id": ORG_A, "wg_id": WG_A},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": []},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": "x"},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": [{"repo": REPO, "number": 1}] * 51},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": [{"repo": REPO, "number": 1}], "extra": 1},
        {"org_id": "x", "wg_id": WG_A, "pulls": [{"repo": REPO, "number": 1}]},
        {"org_id": ORG_A, "wg_id": 5, "pulls": [{"repo": REPO, "number": 1}]},
        {"org_id": ORG_A, "wg_id": WG_A, "task_id": "t", "pulls": [{"repo": REPO, "number": 1}]},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": ["x"]},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": [{"repo": REPO}]},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": [{"repo": REPO, "number": 1, "x": 1}]},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": [{"repo": "no-slash", "number": 1}]},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": [{"repo": 5, "number": 1}]},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": [{"repo": REPO, "number": 0}]},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": [{"repo": REPO, "number": True}]},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": [{"repo": REPO, "number": "1"}]},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": [{"repo": REPO, "number": 2**31}]},
        {"org_id": ORG_A, "wg_id": WG_A, "pulls": [{"repo": REPO, "number": 1}, {"repo": REPO.upper(), "number": 1}]},
    ],
)
def test_malformed_bodies_are_refused(body: dict[str, Any]) -> None:
    with pytest.raises(pull_status.Failed) as refused:
        pull_status.parse_request(body)
    assert refused.value.code == "upstream_error"


def test_a_well_formed_body() -> None:
    task = uuid7()
    asked = request((REPO, 1), (REPO, 2), ("Octo-Org/Second", 1), task_id=task)
    assert pull_status.parse_request(asked) == pull_status.Request(
        ORG_A, WG_A, task, ((REPO, 1), (REPO, 2), ("Octo-Org/Second", 1))
    )
    assert pull_status.parse_request(request(*[(REPO, n) for n in range(1, 51)])).task_id is None


def test_rights_tasks_and_malformed_requests(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        await legacy(env, token=TOKEN)
        failed = {"step": "failed", "code": "forbidden"}
        # an organization this device does not serve, a workgroup it is not assigned to
        assert await ask(env, request((REPO, 1)) | {"org_id": uuid7()}) == failed
        assert await ask(env, request((REPO, 1), wg=uuid7())) == failed
        # bob is a member who cannot read workgroup two (the device serves it)
        assert await ask(env, request((REPO, 1), wg=WG_TWO), user="bob") == failed
        # a task this device knows in another workgroup or organization
        await known_task(env, wg_id=WG_ONE)
        assert await ask(env, request((REPO, 1), task_id=TASK)) == failed
        other = uuid7()
        await known_task(env, other, org_id=pid("b/org"))
        assert await ask(env, request((REPO, 1), task_id=other)) == failed
        assert await ask(env, {"pulls": []}) == {"step": "failed", "code": "upstream_error"}
        assert gh.journal() == []  # nothing was read for a refused request
        log = env.log()
        for reason in ("not_assigned", "not_reader", "other_workgroup", "kind=body"):
            assert reason in log
        assert REPO not in log

    run(store_dsn, gh, body)


# --- identities -------------------------------------------------------------------------------------------------


def test_a_writer_reads_with_the_resolved_identity_a_reader_with_the_own_primary_record(
    store_dsn: str, gh: GitHub
) -> None:
    async def body(env: Env, hub: IdentityHub) -> None:
        # alice's primary record is `other-user` (no access to octo-org/hello); the company record is octo-user's
        await legacy(env, token=OTHER_TOKEN, login="other-user", user_id=102)
        await env.add_ck(owner=ORG_A)
        company = hub.add(identity_body(login="octo-org"), owner=ORG_A, kind="org")
        await choose(env.pool, defaults=[("wg", WG_A, company), ("wg", WG_ONE, company)])
        # alice writes the default workgroup: the workgroup's default
        writer = await ask(env, request((REPO, 1)))
        assert writer["step"] == "done"
        assert subset(writer) == [found()]
        (pull,) = writer["pulls"]
        assert pull == {
            "repo": REPO,
            "number": 1,
            "found": True,
            "title": "Feature",
            "state": "open",
            "draft": False,
            "merged": False,
            "mergeable": True,
            "mergeable_state": "clean",
            "head_ref": "feature",
            "head_sha": pull["head_sha"],
            "head_repo": REPO,
            "base_ref": "main",
            "url": "https://github.example/octo-org/hello/pull/1",
            "updated_at": pull["updated_at"],
        }
        assert env.clock.sleeps[-1:] == [1.0]  # the first read was null: read again after 1 s
        # bob only reads workgroup one: his own primary record (which does not reach the repository), never the
        # workgroup's default (which would)
        await legacy(env, owner=BOB, author="bob", token=OTHER_TOKEN, login="other-user", user_id=102)
        reader = await ask(env, request((REPO, 1), wg=WG_ONE), user="bob")
        assert subset(reader) == [{"repo": REPO, "number": 1, "found": False, "code": "not_found"}]
        # a task this device knows: its choice wins over the workgroup's default
        await known_task(env)
        second = hub.add(identity_body(token=OTHER_TOKEN, login="other-user", user_id=102), owner=BOB, author="bob")
        await choose(env.pool, task_choice=company, defaults=[("wg", WG_A, second)])
        assert subset(await ask(env, request((REPO, 1), task_id=TASK))) == [found()]
        # an unknown task is not used: the (unusable) default is skipped, alice's primary record is used
        assert subset(await ask(env, request((REPO, 1), task_id=uuid7()))) == [
            {"repo": REPO, "number": 1, "found": False, "code": "not_found"}
        ]
        # a task choice the requester may not use (bob's personal record): nothing usable
        await choose(env.pool, task_choice=second)
        assert await ask(env, request((REPO, 1), task_id=TASK)) == {"step": "failed", "code": "github_not_connected"}
        assert "reason=identity_forbidden" in env.log()
        assert "identity=resolved primary=false" in env.log()
        assert "identity=primary primary=true codes=not_found" in env.log()

    run(store_dsn, gh, body)


class Failing:
    """A stand-in of `ops.identity_client` whose opening fails with `error`."""

    def __init__(self, error: Exception) -> None:
        self.error = error

    def __call__(self, *_args: Any) -> Failing:
        return self

    async def __aenter__(self) -> None:
        raise self.error

    async def __aexit__(self, *_exc: object) -> None:
        return None


def test_without_a_usable_identity_the_request_is_not_connected(
    store_dsn: str, gh: GitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        refused = {"step": "failed", "code": "github_not_connected"}
        assert await ask(env, request((REPO, 1))) == refused  # a writer without any identity
        assert await ask(env, request((REPO, 1), wg=WG_ONE), user="bob") == refused  # a reader without a record
        assert "reason=no_primary" in env.log()
        assert "reason=github_not_connected" in env.log()
        await legacy(env, token=TOKEN)

        async def unreachable(_context: Any, _operation: Any) -> None:
            raise HubTransportError("unreachable")

        monkeypatch.setattr(identity, "primary", unreachable)
        unreachable_answer = await ask(env, request((REPO, 1), wg=WG_ONE), user="bob")
        assert unreachable_answer == {"step": "failed", "code": "upstream_unreachable"}
        monkeypatch.undo()

        monkeypatch.setattr(ops, "identity_client", Failing(ops.NotConnected()))
        assert await ask(env, request((REPO, 1))) == refused
        monkeypatch.setattr(ops, "identity_client", Failing(GitHubError("unreachable", retryable=True)))
        assert await ask(env, request((REPO, 1))) == {"step": "failed", "code": "upstream_unreachable"}
        assert gh.journal() == []

    run(store_dsn, gh, body)


# --- per pull request -------------------------------------------------------------------------------------------


def test_each_pull_request_is_answered_on_its_own(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        await legacy(env, token=TOKEN)
        asked = request((REPO, 1), (REPO, 99), ("octo-org/hidden", 1), ("octo-org/second", 5))
        gh.faults({"route": PULL_ROUTE, "repo": "octo-org/second", "kind": "error", "status": 502})
        answer = await ask(env, asked)
        assert subset(answer) == [
            found(),
            {"repo": REPO, "number": 99, "found": False, "code": "not_found"},
            {"repo": "octo-org/hidden", "number": 1, "found": False, "code": "not_found"},
            {"repo": "octo-org/second", "number": 5, "found": False, "code": "upstream_error"},
        ]
        assert "codes=not_found,upstream_error" in env.log()
        # the base moves with a conflicting change: unknown on the first read, a conflict after the re-read
        moved = gh.control.request(
            "POST",
            "/_double/commits",
            {"repo": REPO, "branch": "main", "commit": {"files": {"src/app.py": "x = 3\n"}}},
        )
        assert moved.status == 200
        env.clock.sleeps.clear()
        assert subset(await ask(env, request((REPO, 1)))) == [found(mergeable=False, state="dirty")]
        assert env.clock.sleeps == [1.0]
        # GitHub still computing: read again after 1 s and 2 s, then null is passed on
        gh.faults({"route": PULL_ROUTE, "kind": "mergeable_unknown", "times": 3})
        env.clock.sleeps.clear()
        assert subset(await ask(env, request((REPO, 1)))) == [found(mergeable=None, state="unknown")]
        assert env.clock.sleeps == [1.0, 2.0]
        # computed already: no re-read
        env.clock.sleeps.clear()
        assert subset(await ask(env, request((REPO, 1)))) == [found(mergeable=False, state="dirty")]
        assert env.clock.sleeps == []
        assert REPO not in env.log()

    run(store_dsn, gh, body)


def test_the_repository_list_and_a_refusing_organization(store_dsn: str, gh: GitHub) -> None:
    async def body(env: Env, _hub: IdentityHub) -> None:
        await legacy(env, token=TOKEN)
        org = env.world.orgs[ORG_A]
        allowed = {"repositories": [REPO], "protected_branches": ["main"], "pr_draft_default": True}
        sealed = seal(org, "alice", "settings.workgroup", label="s1", body=allowed)
        async with env.pool.connection() as conn:
            await settings.keep(
                conn,
                org_id=ORG_A,
                wg_id=WG_A,
                event_id=uuid7(),
                scope_seq=1,
                author_sig=v.sig_pk("alice"),
                sealed=sealed,
            )
        answer = await ask(env, request(("Octo-Org/Hello", 1), ("octo-org/second", 1)))
        assert subset(answer) == [
            found() | {"repo": "Octo-Org/Hello"},
            {"repo": "octo-org/second", "number": 1, "found": False, "code": "repo_not_allowed"},
        ]
        assert gh.paths().count("GET /repos/octo-org/second/pulls/1") == 0
        # a token the SAML organization refuses (403): not accessible
        await legacy(env, token=SSO_TOKEN, login="sso-user", user_id=103)
        await cache.purge(env.context, ALICE, "github")
        assert subset(await ask(env, request((REPO, 1)))) == [
            {"repo": REPO, "number": 1, "found": False, "code": "repo_not_accessible"}
        ]
        # a revoked token ends the request
        await legacy(env, token=TOKEN)
        await cache.purge(env.context, ALICE, "github")
        gh.control.request("POST", "/_double/tokens", {"revoke": TOKEN})
        assert await ask(env, request((REPO, 1))) == {"step": "failed", "code": "github_not_connected"}
        assert "step=pull kind=unauthorized" in env.log()

    run(store_dsn, gh, body)


PULL = {
    "title": "t" * 2000,
    "state": "closed",
    "draft": True,
    "merged": True,
    "mergeable": None,
    "mergeable_state": "unknown",
    "head": {"ref": "h", "sha": "a" * 40, "repo": None},
    "base": {"ref": "main", "sha": "b" * 40},
    "html_url": "https://github.com/o/r/pull/5",
    "updated_at": "2026-01-01T00:00:00Z",
}


def test_the_fields_of_an_answer() -> None:
    state = pull_status._state(dict(PULL))
    assert (len(state["title"]), state["head_repo"], state["merged"], state["mergeable"]) == (1024, None, True, None)
    for broken in (
        {"state": "merged"},
        {"base": {"ref": "-bad", "sha": "b" * 40}},
        {"head": {"ref": "h", "sha": "x", "repo": None}},
        {"head": {"ref": "h", "sha": "a" * 40, "repo": {"full_name": "no-slash"}}},
        {"mergeable": "yes"},
        {"mergeable_state": "Not A Word"},
        {"merged": None},
        {"updated_at": "yesterday"},
        {"title": None},
    ):
        with pytest.raises(GitHubError):
            pull_status._state({**PULL, **broken})


# --- github.get_pull_request ------------------------------------------------------------------------------------


def test_get_pull_request_reads_mergeable_again_and_passes_null_on(store_dsn: str, gh: GitHub) -> None:
    async def main() -> None:
        async with kit_env(store_dsn, github_url=gh.url, github_wall=gh.clock) as env:
            env.hub.records[(ALICE, "github")] = make_record(identity_body(token=TOKEN))
            await env.add_ck()
            gh.faults({"route": PULL_ROUTE, "kind": "mergeable_unknown", "times": 3})
            result = await EXECUTORS["github.get_pull_request"](context_for(env, {"repo": REPO, "number": 1}))
            result_ok("github.get_pull_request", result)
            assert (result["mergeable"], result["mergeable_state"]) == (None, "unknown")
            assert env.clock.sleeps == [1.0, 2.0]
            assert gh.paths().count(f"GET /repos/{REPO}/pulls/1") == 3

    asyncio.run(main())
    assert gh.failures() == []


def test_get_pull_request_of_a_closed_pull_request_reads_once(store_dsn: str) -> None:
    answers = [{**PULL, "number": 5, "title": "Closed", "body": None, "mergeable_state": "unknown"}]
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith(("/files", "/comments")):
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=answers[0])

    async def main() -> None:
        async with kit_env(store_dsn, github_url="https://api.mock") as env:
            env.context.github_options = GitHubOptions(transport=httpx.MockTransport(handle))
            env.hub.records[(ALICE, "github")] = make_record(identity_body(token=TOKEN))
            await env.add_ck()
            result = await EXECUTORS["github.get_pull_request"](context_for(env, {"repo": "o/r", "number": 5}))
            result_ok("github.get_pull_request", result)
            assert (result["state"], result["mergeable"], result["mergeable_state"]) == ("closed", None, "unknown")
            assert seen.count("/repos/o/r/pulls/5") == 1
            assert env.clock.sleeps == []
            answers[0] = {**answers[0], "state": "open", "mergeable": "maybe"}
            with pytest.raises(OperationFailed) as invalid:
                await EXECUTORS["github.get_pull_request"](context_for(env, {"repo": "o/r", "number": 5}))
            assert invalid.value.code == "upstream_error"
            answers[0] = {key: value for key, value in answers[0].items() if key != "mergeable_state"}
            answers[0]["mergeable"] = False
            result = await EXECUTORS["github.get_pull_request"](context_for(env, {"repo": "o/r", "number": 5}))
            assert (result["mergeable"], result["mergeable_state"]) == (False, "unknown")

    asyncio.run(main())


def test_the_re_read_delays() -> None:
    assert pulls.REREAD_DELAYS == (1.0, 2.0)
