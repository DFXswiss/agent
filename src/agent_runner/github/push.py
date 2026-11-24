"""`github.push` (05-runner.md section 12.1, 03-api.md section 9, ADR-015): a fast-forward ref update to a commit of an
earlier `github.commit` of the task (the formal checks ensure that), never forced.

Pre-check: `GET /repos/{r}` (access); its `html_url` is kept for the result URL.

Executor: `GET /repos/{r}/git/ref/heads/{branch}`:

- missing -> `POST /repos/{r}/git/refs {ref: refs/heads/<branch>, sha}`;
- already at the commit -> nothing to change (`identical`);
- otherwise `GET /repos/{r}/compare/{head}...{sha}`: `ahead` -> `PATCH /repos/{r}/git/refs/heads/{branch}
  {sha, force: false}`, `identical` -> nothing to change; `behind`, `diverged` or no common history (`404`) ->
  `non_fast_forward`.

When GitHub refuses the creation or the update (`422`: the branch appeared or moved in between) the ref is read again:
at the commit means an earlier attempt of this operation won (succeeded), anything else is `non_fast_forward`.
Result `{branch, sha, url}` with `url` = `<html_url>/tree/<branch>`.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from agent_runner.github import access, ops
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.operations.extensions import OperationContext, OperationFailed, PrecheckRequest, PrecheckResult

NOT_FAST_FORWARD = "The branch is not an ancestor of the commit."


async def check(request: PrecheckRequest) -> PrecheckResult:
    async with access.client(request) as github:
        info = await access.repository(github, str(request.params["repo"]), write=True)
        url = ops.text(info.get("html_url"), ops.URL_RE)
    return PrecheckResult(data={"html_url": url})


def _ref(branch: str) -> str:
    return quote(branch, safe="/")


async def head(github: GitHubClient, repo: str, branch: str) -> str | None:
    try:
        answer = await github.request("GET", f"/repos/{repo}/git/ref/heads/{_ref(branch)}")
    except GitHubError as error:
        if error.kind == "not_found":
            return None
        raise
    return ops.text(ops.obj(ops.obj(answer.data).get("object")).get("sha"), ops.SHA_RE)


async def _settled(github: GitHubClient, repo: str, branch: str, sha: str) -> None:
    """After a refused write: success when the branch is at the commit now, `non_fast_forward` otherwise."""
    if await head(github, repo, branch) != sha:
        raise OperationFailed("non_fast_forward", message=NOT_FAST_FORWARD)


async def _fast_forward(github: GitHubClient, repo: str, current: str, sha: str) -> bool:
    """Whether `sha` is ahead of `current` (False when they are identical); `non_fast_forward` otherwise."""
    try:
        answer = await github.request("GET", f"/repos/{repo}/compare/{current}...{sha}")
    except GitHubError as error:
        if error.kind == "not_found":
            raise OperationFailed("non_fast_forward", message=NOT_FAST_FORWARD) from None
        raise
    status = ops.obj(answer.data).get("status")
    if status == "ahead":
        return True
    if status == "identical":
        return False
    if status in ("behind", "diverged"):
        raise OperationFailed("non_fast_forward", message=NOT_FAST_FORWARD)
    raise ops.invalid()


async def push(ctx: OperationContext, github: GitHubClient) -> dict[str, Any]:
    repo, branch = str(ctx.params["repo"]), str(ctx.params["branch"])
    sha = ops.text(ctx.params["commit_sha"], ops.SHA_RE)
    url = ops.text(ctx.data.get("html_url"), ops.URL_RE)
    current = await head(github, repo, branch)
    try:
        if current is None:
            body: dict[str, Any] = {"ref": f"refs/heads/{branch}", "sha": sha}
            await github.request("POST", f"/repos/{repo}/git/refs", body=body)
        elif current != sha and await _fast_forward(github, repo, current, sha):
            body = {"sha": sha, "force": False}
            await github.request("PATCH", f"/repos/{repo}/git/refs/heads/{_ref(branch)}", body=body)
    except GitHubError as error:
        if error.kind not in ("unprocessable", "conflict"):
            raise
        await _settled(github, repo, branch, sha)
    return {"branch": branch, "sha": sha, "url": f"{url}/tree/{_ref(branch)}"}


execute = ops.executor(push)
