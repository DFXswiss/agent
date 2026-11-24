"""Pre-checks of the GitHub operation types (03-api.md section 9 "Repository-level checks", 05-runner.md section 12
step 3; RUN-07's registry hook `<area>/op_prechecks.py:PRECHECKS`, ADR-041).

They run after the formal checks (which already refused an acting user without a cached `github` credential;
`credentials.acting`) and before the dedupe tag and the registration:

- `connection(request)` (every `github.*` type): the acting user's credential opens on this device and holds a token
  (`github_not_connected` otherwise). `github.list_repositories` uses only this; its pre-check data carries the
  workgroup's allow-list for the executor (`allowed`) and whether it restricts at all (`restricted`; false without a
  list: then every repository is allowed, KOR-30).
- `verified_connection(request)` (`github.request_connection`, KOR-25): additionally `GET /user` with the acting user's
  token, so a revoked token is not answered with its login: `401` -> `github_not_connected` (the runner holds the
  operation and the acting user is asked to connect again), any other failure -> `upstream_error`.
- `repo_access(request)` (every type with `repo`): additionally `GET /repos/{repo}` with the acting user's token: an
  access refusal (`404`, SAML single sign-on, an organisation's restriction of OAuth Apps or classic tokens) ->
  `repo_not_accessible` with the one next step (`reach.refusal`, KOR-65: the pipeline holds the operation and asks the
  acting user to take that step), `401` -> `github_not_connected`; any other failure -> `upstream_error` (a pre-check
  is not retried: the session may request the operation again).

RUN-10 registers its own pre-checks for `github.checkout`, `github.commit` and `github.push` (the registry allows one
per type) and calls `repo_access` first, so every `github.*` type gets the same connection and access checks.
"""

from __future__ import annotations

from agent_runner.github import ops, reach
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.operations.extensions import CheckFailed, PrecheckRequest, PrecheckResult

ACCESS_FAILED = "The repository could not be checked at GitHub."


async def connection(request: PrecheckRequest) -> PrecheckResult:
    try:
        async with ops.actor_client(request.context, request.operation):
            pass
    except ops.NotConnected:
        raise CheckFailed("github_not_connected", message=ops.NOT_CONNECTED) from None
    except GitHubError:
        raise CheckFailed("upstream_error", message=ACCESS_FAILED) from None
    return PrecheckResult()


async def verified_connection(request: PrecheckRequest) -> PrecheckResult:
    try:
        async with ops.actor_client(request.context, request.operation) as client:
            await client.request("GET", "/user")
    except ops.NotConnected:
        raise CheckFailed("github_not_connected", message=ops.NOT_CONNECTED) from None
    except GitHubError as error:
        if error.kind == "unauthorized":
            raise CheckFailed("github_not_connected", message=ops.NOT_CONNECTED) from None
        raise CheckFailed("upstream_error", message=ops.SSO_REQUIRED if error.sso else ACCESS_FAILED) from None
    return PrecheckResult()


async def list_repositories(request: PrecheckRequest) -> PrecheckResult:
    await connection(request)
    settings = request.settings
    return PrecheckResult(data={"allowed": sorted(settings.repositories), "restricted": settings.restricted})


async def repo_access(request: PrecheckRequest) -> PrecheckResult:
    repo = str(request.params["repo"])
    client: GitHubClient | None = None
    try:
        async with ops.actor_client(request.context, request.operation) as client:
            await client.request("GET", f"/repos/{repo}")
    except ops.NotConnected:
        raise CheckFailed("github_not_connected", message=ops.NOT_CONNECTED) from None
    except GitHubError as error:
        refused = None if client is None else reach.refusal(client, repo, error)
        if refused is not None:
            raise refused from None
        if error.kind == "unauthorized":
            raise CheckFailed("github_not_connected", message=ops.NOT_CONNECTED) from None
        raise CheckFailed("upstream_error", message=ACCESS_FAILED) from None
    return PrecheckResult()
