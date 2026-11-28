"""Executor `github.create_branch` (05-runner.md section 12.1, 03-api.md section 9).

`from` is resolved to a commit: a full SHA is used as it is; a branch name through
`GET /repos/{r}/git/ref/heads/{from}`; a short SHA through `GET /repos/{r}/commits/{from}` (an unknown name or SHA is
`not_found`). Then `POST /repos/{r}/git/refs {ref: refs/heads/<branch>, sha}`. A branch that exists already (GitHub:
`409`, or `422` "Reference already exists") is `branch_exists` (not retryable) - except on a retry of this operation
when the branch points at exactly the resolved commit: the earlier attempt created it and only its answer was lost.

An identity that may only read the repository creates the branch in its fork (the pre-check's `fork`, HOT-33):
`from` is resolved in the repository, the fork is created when it has none yet (`fork.ensure`), a `from` that names a
branch is brought to the repository's state in the fork first (`fork.sync`, merge-upstream), and the branch is created
in the fork at the repository's commit. The result names the fork (`fork`).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from agent_runner.github import fork, ops
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.operations.rules import FULL_SHA_RE, SHA_RE

EXISTS_TEXT = "reference already exists"


def ref_path(branch: str) -> str:
    return quote(branch, safe="/")


async def resolve(client: GitHubClient, repo: str, source: str) -> str:
    """The commit SHA of a branch name or (short) SHA."""
    if FULL_SHA_RE.fullmatch(source):
        return source
    if SHA_RE.fullmatch(source):
        answer = await client.request("GET", f"/repos/{repo}/commits/{source}")
        return ops.text(ops.obj(answer.data).get("sha"), ops.SHA_RE)
    answer = await client.request("GET", f"/repos/{repo}/git/ref/heads/{ref_path(source)}")
    return ops.text(ops.obj(ops.obj(answer.data).get("object")).get("sha"), ops.SHA_RE)


async def _head(client: GitHubClient, repo: str, branch: str) -> str | None:
    try:
        return await resolve(client, repo, branch)
    except GitHubError:
        return None


async def create_branch(ctx: OperationContext, client: GitHubClient) -> dict[str, Any]:
    repo, branch, source = str(ctx.params["repo"]), str(ctx.params["branch"]), str(ctx.params["from"])
    sha = await resolve(client, repo, source)
    target, done = repo, {"branch": branch, "sha": sha}
    routed = ctx.data.get("fork")
    if isinstance(routed, dict):
        target = done["fork"] = (await fork.ensure(client, repo, routed))[0]
        if not SHA_RE.fullmatch(source):
            await fork.sync(client, target, source)
    body = {"ref": f"refs/heads/{branch}", "sha": sha}
    try:
        await client.request("POST", f"/repos/{target}/git/refs", body=body)
    except GitHubError as error:
        exists = error.kind == "conflict" or (error.kind == "unprocessable" and EXISTS_TEXT in error.message.lower())
        if not exists:
            raise
        if ctx.attempt > 1 and await _head(client, target, branch) == sha:
            return done
        raise OperationFailed("branch_exists", message="The branch exists already.") from None
    return done


execute = ops.executor(create_branch)
