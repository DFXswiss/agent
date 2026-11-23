"""Pre-checks of the GitHub operation types (03-api.md section 9 "Repository-level checks", 05-runner.md section 12
step 3; RUN-07's registry hook `<area>/op_prechecks.py:PRECHECKS`, ADR-041).

They run after the formal checks (which already refused a task owner without a cached `github` credential) and before
the dedupe tag and the registration:

- `connection(request)` (every `github.*` type): the task owner's credential opens on this device and holds a token
  (`github_not_connected` otherwise). `github.list_repositories` uses only this; its pre-check data carries the
  workgroup's allow-list for the executor (`allowed`).
- `repo_access(request)` (every type with `repo`): additionally `GET /repos/{repo}` with the owner's token: `404` ->
  `repo_not_accessible` (the repository is allowed by the settings but not reachable with the owner's token: GitHub App
  installations, or the account's own access for the operator's token of ADR-053), `401` -> `github_not_connected`;
  any other failure -> `upstream_error` (a pre-check is not retried: the session may request the operation again).

RUN-10 registers its own pre-checks for `github.checkout`, `github.commit` and `github.push` (the registry allows one
per type) and calls `repo_access` first, so every `github.*` type gets the same connection and access checks.
"""

from __future__ import annotations

from agent_runner.github import ops
from agent_runner.github.client import GitHubError
from agent_runner.operations.extensions import CheckFailed, PrecheckRequest, PrecheckResult

ACCESS_FAILED = "The repository could not be checked at GitHub."


async def connection(request: PrecheckRequest) -> PrecheckResult:
    try:
        async with ops.owner_client(request.context, request.session.owner_id):
            pass
    except ops.NotConnected:
        raise CheckFailed("github_not_connected", message=ops.NOT_CONNECTED) from None
    except GitHubError:
        raise CheckFailed("upstream_error", message=ACCESS_FAILED) from None
    return PrecheckResult()


async def list_repositories(request: PrecheckRequest) -> PrecheckResult:
    await connection(request)
    return PrecheckResult(data={"allowed": sorted(request.settings.repositories)})


async def repo_access(request: PrecheckRequest) -> PrecheckResult:
    repo = str(request.params["repo"])
    try:
        async with ops.owner_client(request.context, request.session.owner_id) as client:
            await client.request("GET", f"/repos/{repo}")
    except ops.NotConnected:
        raise CheckFailed("github_not_connected", message=ops.NOT_CONNECTED) from None
    except GitHubError as error:
        if error.kind == "not_found":
            raise CheckFailed(
                "repo_not_accessible", message="The repository is not accessible with the owner's GitHub connection."
            ) from None
        if error.kind == "unauthorized":
            raise CheckFailed("github_not_connected", message=ops.NOT_CONNECTED) from None
        raise CheckFailed("upstream_error", message=ACCESS_FAILED) from None
    return PrecheckResult()
