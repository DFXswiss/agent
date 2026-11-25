"""Device request `github.pull_status` (KOR-48, ADR-084 section 2; 05-runner.md section 11.5): a fresh read of the
state of pull requests for the pull-request view. Repositories, numbers and states are confidential and tokens never
leave a device, so the client asks the device and the answer is end-to-end encrypted to the requester.

**Body** (`contracts/requests/github.pull_status.request.json`, `parse_request`): exactly `org_id`, `wg_id` (UUIDv7
text), optional `task_id` (UUIDv7) and `pulls`: 1 to `MAX_PULLS` objects `{repo, number}` (`<owner>/<name>`, a number
1..2^31-1), no repository and number twice (the repository compared without case). Any other key or form is
`upstream_error`, as for the other handlers of a malformed body.

**Rights** (in the verified membership log of `org_id`, `agent_runner.orgs.book`): this device must be assigned to
`wg_id` and the requester must be able to read `wg_id` (`OrgState.assigned`, `OrgState.can_read`), else `forbidden`.
A task this device knows (a local session of it, `runner.session`) must belong to `wg_id` and `org_id`, else
`forbidden`; a task it does not know is not used.

**Identity.** A requester who can write the workgroup (`OrgState.can_write_wg`) reads with the identity the ADR-078
resolution picks for an operation of that task (`github.identity.resolve` with a stand-in operation whose acting user
is the requester and a stand-in session of the task, without session or parameter choice): the task's choice when
this device knows the task, the workgroup's default, the organization's default, the requester's own primary record,
the operator's account - every candidate usable by the requester (`identity.usable`). A requester who can only read
uses the own primary record only (`identity.primary`). Every record is opened through `credentials.acting`
(`ops.identity_client`). Nothing usable (also a forbidden or unreadable choice, a refused token: `401`) is
`github_not_connected`; a hub that cannot be reached while a record is needed is `upstream_unreachable`. The account
check of the operations (`identity.check_account`) is not run: nothing is written, and GitHub's answers are read only
into this answer.

**Per pull request**, in the order asked: a repository outside the workgroup's repository list, when it is set
(`operations.settings`, ADR-054), is `{repo, number, found:false, code:"repo_not_allowed"}` without a request; else
`github.pulls.read_pull` (`GET /repos/{r}/pulls/{n}`, read again after 1 s and 2 s while an open pull request's
`mergeable` is null) gives `{repo, number, found:true, title, state, draft, merged, mergeable, mergeable_state,
head_ref, head_sha, head_repo, base_ref, url, updated_at}` (`repo` as asked; `title` cut to 1024 characters;
`head_repo` the head's repository, null when it is gone); `404` is `not_found`, another `403` `repo_not_accessible`,
everything else (an answer outside the contract, `5xx`, rate limits, network errors) `upstream_error` for that pull
request. The answer is `{step:"done", pulls}`; a failure `{step:"failed", code}` (both final). Log lines name the
request, the count, the way the identity was chosen and the codes - never a repository or a number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, cast

from agent_runner.github import identity, ops, pulls
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.github.device_flow import UUID_RE
from agent_runner.operations import settings as workgroup_settings
from agent_runner.operations.extensions import CheckFailed, PrecheckRequest
from agent_runner.operations.rules import branch_valid
from agent_runner.operations.settings import WorkgroupSettings
from agent_runner.orgs.book import book_for
from agent_runner.orgs.db import session
from agent_runner.requests.service import Flow
from agent_runner.sync.http import HubTransportError

KIND = "github.pull_status"
MAX_PULLS = 50
BODY_KEYS = frozenset({"org_id", "wg_id", "task_id", "pulls"})
PULL_KEYS = frozenset({"repo", "number"})
NIL = "00000000-0000-0000-0000-000000000000"
READ_TYPE = "github.get_pull_request"
"""The operation type the stand-in operation names: a read of pull requests (`credentials.acting.needs`)."""
TITLE_MAX = pulls.MAX_TITLE
STATES = frozenset({"open", "closed"})
TEXT_RE = re.compile(r"^.*$", re.S)


class Failed(Exception):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class Request:
    """The checked body."""

    org_id: str
    wg_id: str
    task_id: str | None
    pulls: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class Reader:
    """The stand-in operation of the identity resolution: a read of pull requests whose acting user is the
    requester (`credentials.acting.Acting`)."""

    actor_id: str
    type: str = READ_TYPE
    id: str = NIL


@dataclass(frozen=True)
class Scope:
    """The stand-in session of the identity resolution: the task (NIL when this device does not know it) in its
    workgroup, a main session without a stored choice of its own (`github.selection.choices`)."""

    task_id: str
    org_id: str
    wg_id: str
    id: str = NIL
    depth: int = 0
    parent_id: str | None = None


def _uuid(value: object) -> bool:
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def _pull(item: object) -> tuple[str, int]:
    if not isinstance(item, dict) or set(item) != PULL_KEYS:
        raise ValueError("pull")
    repo, number = item["repo"], item["number"]
    if not isinstance(repo, str) or not ops.REPO_RE.fullmatch(repo):
        raise ValueError("repo")
    if not isinstance(number, int) or isinstance(number, bool) or not 1 <= number <= ops.MAX_NUMBER:
        raise ValueError("number")
    return repo, number


def parse_request(body: dict[str, Any]) -> Request:
    """The body of the request (module docstring); `Failed("upstream_error")` for anything else."""
    items = body.get("pulls")
    try:
        if set(body) - BODY_KEYS or not _uuid(body.get("org_id")) or not _uuid(body.get("wg_id")):
            raise ValueError("keys")
        if "task_id" in body and not _uuid(body["task_id"]):
            raise ValueError("task_id")
        if not isinstance(items, list) or not 1 <= len(items) <= MAX_PULLS:
            raise ValueError("pulls")
        found = tuple(_pull(item) for item in items)
    except ValueError as error:
        raise Failed("upstream_error", f"step=request kind=body field={error}") from None
    if len({(repo.lower(), number) for repo, number in found}) != len(found):
        raise Failed("upstream_error", "step=request kind=body field=duplicate")
    return Request(str(body["org_id"]), str(body["wg_id"]), body.get("task_id"), found)


async def _writer(flow: Flow, request: Request) -> bool:
    """The rights (module docstring): whether the requester can write the workgroup; `forbidden` otherwise."""
    book = book_for(flow.context)
    async with session(flow.context.pool) as conn:
        await book.ensure_loaded(conn)
    log = book.log(request.org_id)
    if log is None or not log.state.assigned(flow.context.identity.device.device_id, request.wg_id):
        raise Failed("forbidden", "step=rights reason=not_assigned")
    if not log.state.can_read(flow.user, "wg", request.wg_id):
        raise Failed("forbidden", "step=rights reason=not_reader")
    return log.state.can_write_wg(flow.user, request.wg_id)


async def _known_task(flow: Flow, request: Request) -> str:
    """The task when this device knows it (NIL otherwise); `forbidden` for a task of another workgroup."""
    if request.task_id is None:
        return NIL
    async with session(flow.context.pool) as conn:
        cur = await conn.execute(
            "SELECT org_id::text, wg_id::text FROM runner.session WHERE task_id = %s ORDER BY created_at, id LIMIT 1",
            (request.task_id,),
        )
        row = await cur.fetchone()
    if row is None:
        return NIL
    if (row[0], row[1]) != (request.org_id, request.wg_id):
        raise Failed("forbidden", "step=task reason=other_workgroup")
    return request.task_id


async def _settings(flow: Flow, wg_id: str) -> WorkgroupSettings:
    async with session(flow.context.pool) as conn:
        return await workgroup_settings.load(conn, flow.service.sync.storage_key, wg_id)


async def _identity(flow: Flow, request: Request, writer: bool, task: str) -> identity.Resolved:
    """The identity the pull requests are read with (module docstring)."""
    operation = cast(Any, Reader(flow.user))
    try:
        if not writer:
            found = await identity.primary(flow.context, operation)
        else:
            scope = cast(Any, Scope(task, request.org_id, request.wg_id))
            check = PrecheckRequest(flow.context, operation, scope, workgroup_settings.DEFAULTS, {})
            try:
                found = await identity.resolve(check)
            except CheckFailed as failed:
                raise Failed("github_not_connected", f"step=identity reason={failed.code}") from None
    except HubTransportError:
        raise Failed("upstream_unreachable", "step=identity kind=hub") from None
    if found is None:
        raise Failed("github_not_connected", "step=identity reason=no_primary")
    return found


def _state(pull: dict[str, Any]) -> dict[str, Any]:
    """The `found:true` fields of GitHub's answer; `GitHubError("invalid_response")` outside the contract."""
    title = ops.text(pull.get("title"), TEXT_RE)
    state = pull.get("state")
    if state not in STATES:
        raise ops.invalid()
    head, base = ops.obj(pull.get("head")), ops.obj(pull.get("base"))
    if not branch_valid(base.get("ref")):
        raise ops.invalid()
    head_ref, repo = pulls.ref_of(head), head.get("repo")
    return {
        "found": True,
        "title": pulls.cut(title, TITLE_MAX)[0],
        "state": state,
        "draft": ops.flag(pull.get("draft")),
        "merged": ops.flag(pull.get("merged")),
        **pulls.mergeability(pull),
        "head_ref": head_ref["ref"],
        "head_sha": head_ref["sha"],
        "head_repo": None if repo is None else ops.text(ops.obj(repo).get("full_name"), ops.REPO_RE),
        "base_ref": str(base["ref"]),
        "url": ops.text(pull.get("html_url"), ops.URL_RE),
        "updated_at": ops.text(pull.get("updated_at"), ops.TIME_RE),
    }


async def _pull_state(client: GitHubClient, allowed: WorkgroupSettings, repo: str, number: int) -> dict[str, Any]:
    if not allowed.allows(repo):
        return {"repo": repo, "number": number, "found": False, "code": "repo_not_allowed"}
    try:
        return {"repo": repo, "number": number, **_state(await pulls.read_pull(client, repo, number))}
    except GitHubError as error:
        if error.kind == "unauthorized":
            raise Failed("github_not_connected", "step=pull kind=unauthorized") from None
        codes = {"not_found": "not_found", "forbidden": "repo_not_accessible"}
        return {"repo": repo, "number": number, "found": False, "code": codes.get(error.kind, "upstream_error")}


async def _read(flow: Flow, request: Request, found: identity.Resolved, allowed: WorkgroupSettings) -> list[Any]:
    operation = cast(Any, Reader(flow.user))
    states: list[Any] = []
    try:
        async with ops.identity_client(flow.context, operation, found.data()) as client:
            for repo, number in request.pulls:
                states.append(await _pull_state(client, allowed, repo, number))
    except ops.NotConnected:
        raise Failed("github_not_connected", "step=open reason=not_connected") from None
    except GitHubError:
        raise Failed("upstream_unreachable", "step=open kind=hub") from None
    return states


async def pull_status(flow: Flow) -> None:
    try:
        request = parse_request(flow.body)
        writer = await _writer(flow, request)
        task = await _known_task(flow, request)
        allowed = await _settings(flow, request.wg_id)
        found = await _identity(flow, request, writer, task)
        states = await _read(flow, request, found, allowed)
    except Failed as failed:
        flow.service.line(f"github_pull_status failed request_id={flow.id} code={failed.code} {failed.detail}")
        await flow.respond({"step": "failed", "code": failed.code}, final=True)
        return
    codes = sorted({str(item["code"]) for item in states if not item["found"]})
    way = "resolved" if writer else "primary"
    flow.service.line(
        f"github_pull_status done request_id={flow.id} pulls={len(states)} identity={way}"
        f" primary={str(found.primary).lower()} codes={','.join(codes) or '-'}"
    )
    await flow.respond({"step": "done", "pulls": states}, final=True)
