"""GitHub access inside the pre-checks of `github.checkout`, `github.commit` and `github.push` (03-api.md section 9
"Repository-level checks"; the same codes as `precheck.repo_access` of RUN-09).

`client(request)` opens the acting user's client (`ops.actor_client`) for a pre-check and turns every failure into
`CheckFailed`: a missing or refused connection (`NotConnected`, `401`) is `github_not_connected`, any other GitHub
failure `upstream_error` (a pre-check is not retried; the session may request the operation again; an organisation
that requires SSO authorisation of the token is named, `ops.SSO_REQUIRED`). `repository(client, repo)` is
`GET /repos/{repo}` with an access refusal -> `repo_not_accessible` with the one next step (`reach.refusal`, KOR-65)
and returns the answer (default branch, `html_url`), so the pre-checks need no second request for it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agent_runner.github import ops, reach
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.github.precheck import ACCESS_FAILED
from agent_runner.operations.extensions import CheckFailed, PrecheckRequest


@asynccontextmanager
async def client(request: PrecheckRequest) -> AsyncIterator[GitHubClient]:
    try:
        async with ops.actor_client(request.context, request.operation) as opened:
            yield opened
    except ops.NotConnected:
        raise CheckFailed("github_not_connected", message=ops.NOT_CONNECTED) from None
    except GitHubError as error:
        if error.kind == "unauthorized":
            raise CheckFailed("github_not_connected", message=ops.NOT_CONNECTED) from None
        raise CheckFailed("upstream_error", message=ops.SSO_REQUIRED if error.sso else ACCESS_FAILED) from None


async def repository(github: GitHubClient, repo: str) -> dict[str, Any]:
    try:
        answer = await github.request("GET", f"/repos/{repo}")
    except GitHubError as error:
        refused = reach.refusal(github, repo, error)
        if refused is not None:
            raise refused from None
        raise
    return ops.obj(answer.data)
