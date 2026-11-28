"""Writing through the fork of the acting identity when it may only read the repository (HOT-33).

GitHub answers `permissions` with every `GET /repos/{repo}`. An identity with the role `read` or `triage` reads the
repository (`pull` true) but may not push (`push` false); the usual way to contribute is then the identity's own fork
and a pull request from it. The write types (`reach.PUSH_TYPES`: `github.create_branch`, `github.commit`,
`github.push`) and `github.create_pull_request` take that way by themselves, so a session names the repository it works
on and never the fork:

- `route(client, repo, answer)` (pre-check of the write types): `permissions.push` not false -> None, the write goes to
  the repository as before (`reach.check_write` still checks a GitHub App token's installation). Otherwise the fork of
  the identity (`find`): `GET /repos/{login}/{name}` when that is a fork whose `parent` is the repository, else the
  repository's fork list (`GET /repos/{repo}/forks`) for one owned by the identity, so a fork with another name is
  found as well. A fork found -> `{repo: <fork>, owner, create: false}`; one the identity may not push to either ->
  `repo_read_only`. No fork and the repository does not allow forks (`allow_forking` false: the organisation's or the
  repository's setting) -> `repo_read_only`. No fork, forks allowed -> `{repo: null, owner, create: true}`: the
  executor creates it (`ensure`), so nothing is written before the hub registered the operation.
- `ensure(client, repo, routed)` (executors): the fork's full name and web URL; with `create` `POST
  /repos/{repo}/forks` (GitHub answers an existing fork of the account as well, so a repeated attempt finds the first
  one) and a wait for its default branch (GitHub creates a fork asynchronously): `READY_DELAYS`, then a retryable
  `upstream_error`. GitHub refusing the fork (`403`, `404`, `422`) is `repo_read_only`.
- `sync(client, fork, branch)` (`github.create_branch`, before the branch is created): `POST
  /repos/{fork}/merge-upstream {branch}` brings the fork's base branch to the repository's state. A branch missing in
  the fork or diverged from the repository (`404`, `409`, `422`) is left as it is: the new branch starts at the
  repository's commit anyway (forks share their objects with the repository).
- `pull_route(client, repo, answer, head)` (`github.create_pull_request`): without the right to push and with a fork
  that holds `head` the pull request is opened from the fork (`head` = `<fork owner>:<head>`); otherwise as before.

`repo_read_only` is never held (`operations.pipeline` holds only `github_not_connected` and `repo_not_accessible`):
nothing the platform waits for would change it, so the session and the person get the exact sentence at once. Every
result of a write through a fork names it (`fork`), so the session knows where its branch is.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from agent_runner.github import ops, reach
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.operations.extensions import CheckFailed, OperationFailed

READY_DELAYS = (1.0, 2.0, 4.0, 8.0)
"""Seconds between the reads of a new fork's default branch (GitHub creates a fork asynchronously)."""
RIGHTS = ("admin", "maintain", "push", "triage", "pull")
REFUSED = frozenset({"forbidden", "not_found", "unprocessable"})
"""GitHub's answers to a fork that cannot be created for the account."""
KEEP = frozenset({"not_found", "conflict", "unprocessable"})
"""GitHub's answers to a merge-upstream that leaves the fork's branch as it is (missing or diverged)."""

MESSAGES = {
    "no_forks": (
        "The GitHub account {login} may only read {repo} (its rights there: {rights}), has no fork of it, and {repo}"
        " does not allow forks, so the platform can write neither to it nor through a fork. Next step for the person"
        " who gave this instruction: ask an owner of {repo} to give {login} write access or to allow forks ({url}),"
        " then request the operation again."
    ),
    "fork_refused": (
        "The GitHub account {login} may only read {repo} (its rights there: {rights}), and GitHub refused to create a"
        " fork of it for {login}. Next step for the person who gave this instruction: ask an owner of {repo} to give"
        " {login} write access or to allow forks ({url}), then request the operation again."
    ),
    "fork_read_only": (
        "The GitHub account {login} may only read {repo} (its rights there: {rights}), and the connection may not write"
        " its fork {fork} either. Next step for the person who gave this instruction: give the GitHub connection write"
        " access to {fork}, or ask an owner of {repo} to give {login} write access ({url}), then request the operation"
        " again."
    ),
}
NOT_READY = "GitHub has not finished creating the fork yet."


def rights(answer: Mapping[str, Any]) -> str:
    """The rights `permissions` grants, in GitHub's order (`pull, triage` for the role triage)."""
    permissions = answer.get("permissions")
    granted = [name for name in RIGHTS if isinstance(permissions, dict) and permissions.get(name) is True]
    return ", ".join(granted) or "none"


def read_only(client: GitHubClient, repo: str, why: str, **values: str) -> str:
    """The fixed sentence of `repo_read_only` (printable ASCII, at most 1024 characters)."""
    text = MESSAGES[why].format(repo=repo, url=f"{client.web_url}/{repo}", **values)
    return text[:1024]


def may_push(answer: Mapping[str, Any]) -> bool:
    """Whether a repository answer leaves the push to GitHub: only an explicit `permissions.push` false is no."""
    permissions = answer.get("permissions")
    return not (isinstance(permissions, dict) and permissions.get("push") is False)


async def login(client: GitHubClient) -> str:
    """The account of the token (`GET /user`)."""
    answer = await client.request("GET", "/user")
    return ops.text(ops.obj(answer.data).get("login"), ops.LOGIN_RE)


def _forks(found: Mapping[str, Any], repo: str) -> bool:
    parent = found.get("parent")
    named = parent.get("full_name") if isinstance(parent, dict) else None
    return found.get("fork") is True and isinstance(named, str) and named.lower() == repo.lower()


async def find(client: GitHubClient, repo: str, owner: str) -> dict[str, Any] | None:
    """The repository answer of `owner`'s fork of `repo`, None when there is none (module docstring)."""
    name = repo.split("/", 1)[1]
    try:
        answer = await client.request("GET", f"/repos/{owner}/{name}")
    except GitHubError as error:
        if error.kind != "not_found":
            raise
    else:
        found = ops.obj(answer.data)
        if _forks(found, repo):
            return found
    for item in await client.items(f"/repos/{repo}/forks"):
        listed = item.get("owner") if isinstance(item, dict) else None
        named = item.get("full_name") if isinstance(item, dict) else None
        account = listed.get("login") if isinstance(listed, dict) else None
        if isinstance(account, str) and account.lower() == owner.lower() and isinstance(named, str):
            answer = await client.request("GET", f"/repos/{ops.text(named, ops.REPO_RE)}")
            return ops.obj(answer.data)
    return None


def _named(found: Mapping[str, Any]) -> str:
    return ops.text(found.get("full_name"), ops.REPO_RE)


async def route(client: GitHubClient, repo: str, answer: Mapping[str, Any]) -> dict[str, Any] | None:
    """Where a write to `repo` goes (module docstring): None for the repository itself, else the fork's data."""
    if may_push(answer):
        await reach.check_write(client, repo)
        return None
    account = await login(client)
    found = await find(client, repo, account)
    granted = rights(answer)
    if found is not None:
        fork = _named(found)
        if not may_push(found):
            message = read_only(client, repo, "fork_read_only", login=account, rights=granted, fork=fork)
            raise CheckFailed("repo_read_only", message=message)
        html = ops.text(found.get("html_url"), ops.URL_RE)
        return {"repo": fork, "owner": account, "create": False, "html_url": html, "rights": granted}
    if answer.get("allow_forking") is False:
        raise CheckFailed("repo_read_only", message=read_only(client, repo, "no_forks", login=account, rights=granted))
    return {"repo": None, "owner": account, "create": True, "html_url": None, "rights": granted}


async def pull_route(client: GitHubClient, repo: str, answer: Mapping[str, Any], head: str) -> dict[str, Any] | None:
    """The fork a pull request comes from (module docstring), None for a head in the repository itself."""
    if may_push(answer):
        return None
    found = await find(client, repo, await login(client))
    if found is None:
        return None
    fork = _named(found)
    try:
        await client.request("GET", f"/repos/{fork}/git/ref/heads/{quote(head, safe='/')}")
    except GitHubError as error:
        if error.kind != "not_found":
            raise
        return None
    owner = ops.text(ops.obj(found.get("owner")).get("login"), ops.LOGIN_RE)
    return {"repo": fork, "head": f"{owner}:{head}"}


async def _ready(client: GitHubClient, fork: str, branch: str) -> bool:
    for delay in (0.0, *READY_DELAYS):
        if delay:
            await client.sleep(delay)
        try:
            await client.request("GET", f"/repos/{fork}/git/ref/heads/{quote(branch, safe='/')}")
        except GitHubError as error:
            if error.kind not in {"not_found", "conflict"}:
                raise
            continue
        return True
    return False


async def ensure(client: GitHubClient, repo: str, routed: Mapping[str, Any]) -> tuple[str, str]:
    """The fork's full name and web URL, created first when the pre-check found none (module docstring)."""
    if not routed.get("create"):
        return ops.text(routed.get("repo"), ops.REPO_RE), ops.text(routed.get("html_url"), ops.URL_RE)
    try:
        answer = await client.request("POST", f"/repos/{repo}/forks", body={"default_branch_only": False})
    except GitHubError as error:
        if error.kind not in REFUSED or error.sso:
            raise
        values = {"login": str(routed.get("owner")), "rights": str(routed.get("rights", "pull"))}
        raise OperationFailed("repo_read_only", message=read_only(client, repo, "fork_refused", **values)) from None
    found = ops.obj(answer.data)
    fork, html = _named(found), ops.text(found.get("html_url"), ops.URL_RE)
    default = found.get("default_branch")
    if isinstance(default, str) and not await _ready(client, fork, default):
        raise OperationFailed("upstream_error", retryable=True, message=NOT_READY)
    return fork, html


async def sync(client: GitHubClient, fork: str, branch: str) -> None:
    """Bring the fork's `branch` to the state of the repository it was forked from (module docstring)."""
    try:
        await client.request("POST", f"/repos/{fork}/merge-upstream", body={"branch": branch})
    except GitHubError as error:
        if error.kind not in KEEP:
            raise
