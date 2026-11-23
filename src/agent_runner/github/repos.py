"""Executor `github.list_repositories` (05-runner.md section 12.1, 03-api.md section 9).

`GET /user/installations` -> for each installation `GET /user/installations/{id}/repositories` (both paginated):
every repository the task owner reaches through the GitHub App, once, sorted by name, with `allowed` from the
workgroup's allow-list (pre-check data). A repository entry that does not fit the result contract is left out and
logged; a malformed list is `upstream_error`.

A token that is not a GitHub App user token (the operator's account, KOR-27, ADR-053) cannot list installations:
GitHub refuses `GET /user/installations` with `403`. Then the list is `GET /user/repos` (paginated, the same page
and result limits): every repository the account reaches. The workgroup's allow-list stays the boundary - only an
`allowed` repository passes the pre-checks of the other operations.
"""

from __future__ import annotations

from typing import Any

from agent_runner.github import ops
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.operations.extensions import OperationContext
from agent_runner.operations.rules import branch_valid

MAX_REPOSITORIES = 10000


def _entry(item: object, allowed: frozenset[str]) -> dict[str, Any] | None:
    try:
        data = ops.obj(item)
        repo = ops.text(data.get("full_name"), ops.REPO_RE)
        private = ops.flag(data.get("private"))
    except GitHubError:
        return None
    branch = data.get("default_branch")
    if not branch_valid(branch):
        return None
    return {"repo": repo, "default_branch": branch, "private": private, "allowed": repo.lower() in allowed}


async def _lists(client: GitHubClient) -> list[list[Any]]:
    """The repository lists of the token: one per installation, or the account's list without the App (`403`)."""
    try:
        installations = await client.items("/user/installations", key="installations")
    except GitHubError as error:
        if error.kind != "forbidden":
            raise
        return [await client.items("/user/repos")]
    lists = []
    for installation in installations:
        installation_id = ops.integer(ops.obj(installation).get("id"))
        lists.append(await client.items(f"/user/installations/{installation_id}/repositories", key="repositories"))
    return lists


async def list_repositories(ctx: OperationContext, client: GitHubClient) -> dict[str, Any]:
    allowed = frozenset(str(name) for name in ctx.data.get("allowed", []))
    found: dict[str, dict[str, Any]] = {}
    skipped = 0
    for items in await _lists(client):
        for item in items:
            entry = _entry(item, allowed)
            if entry is None:
                skipped += 1
                continue
            found.setdefault(entry["repo"].lower(), entry)
    if skipped:
        ctx.context.log.write(f"runner.github repositories_skipped operation_id={ctx.operation.id} count={skipped}\n")
        ctx.context.log.flush()
    ordered = [found[name] for name in sorted(found)][:MAX_REPOSITORIES]
    return {"repositories": ordered}


execute = ops.executor(list_repositories)
