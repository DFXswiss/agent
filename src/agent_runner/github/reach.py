"""The one next step when a GitHub connection does not reach a repository (KOR-65, rule E-29, ADR-015 addendum).

A repository-level pre-check (`precheck.repo_access`, `access.repository`) that GitHub refuses for access - `404` (the
token does not see the repository), a `403` naming an organisation's restriction of OAuth Apps or of classic personal
access tokens, or a `403` for SAML single sign-on - is not a dead end any more: `refusal(client, repo, error)` turns it
into `CheckFailed("repo_not_accessible", message, next=...)`, and the pipeline holds the operation (`github_access`,
`operations.pipeline` step 1a) and shows the person who gave the instruction the step in the task's chat. The step
depends on what refused and on the kind of token (`client.token_kind`, from GitHub's token prefixes):

- `403` with `X-GitHub-SSO: required` (any token): `authorize_sso` with GitHub's authorisation URL from the header
  when it is exactly `<web>/orgs/<org>/sso?authorization_request=...`, else `<web>/orgs/<owner>/sso`;
- `403` naming "OAuth App access restrictions" (an OAuth App token): `approve_app` with
  `<web>/settings/connections/applications/<client id>` for the platform's OAuth App (a connection of its device
  flow), else `<web>/settings/applications` (for example the GitHub CLI's token);
- `403` naming that the organisation forbids "personal access token (classic)": `new_token` with a pre-filled
  fine-grained token for the owner (`fine_grained_url`);
- `404` with a GitHub App token: `connect` when the OAuth App is set up (one device flow reaches every repository),
  else `install_app` (`<web>/apps/<slug>/installations/new`) when the App's URL name is set, else `new_token`
  (`classic_url`);
- `404` with a fine-grained token: `new_token` with a pre-filled fine-grained token for the owner;
- `404` with any other token (OAuth App, classic, unknown): `check_access` with `<web>/<owner>/<name>` - the account
  itself does not reach it (or it does not exist).

Every URL is built from the configured web URL and values the platform checked (`ops.REPO_RE`); nothing of GitHub's
answer is passed on except a checked SSO URL. The message is a fixed English sentence ending with the step, so a
session (and an agent reading the result) sees the same next step as the card. Any other GitHub failure is not an
access refusal and stays as before (`upstream_error`, `github_not_connected`).

The pre-filled token links: GitHub documents the URL parameters of a fine-grained token (`name`, `description`,
`target_name`, `expires_in`, permissions such as `contents=write`); a fine-grained token reaches one owner. The
classic link (`scopes`, `description`) reaches every repository of the account and is the common form tools use.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.operations.extensions import CheckFailed

OAUTH_RESTRICTED = "oauth app access restrictions"
CLASSIC_FORBIDDEN = "personal access token (classic)"
TOKEN_NAME = "agent.dfx.swiss"
FINE_GRAINED_PERMISSIONS = (
    ("contents", "write"),
    ("pull_requests", "write"),
    ("issues", "write"),
    ("workflows", "write"),
    ("metadata", "read"),
)
CLASSIC_SCOPES = "repo,workflow"

MESSAGES = {
    "connect": (
        "The GitHub connection of the person who gave this instruction does not reach {repo}. Next step for that"
        " person: connect GitHub again with full repository access (Settings, GitHub, Connect GitHub)."
    ),
    "install_app": (
        "The GitHub App is not installed for {owner}, so the connection does not reach {repo}. Next step for the"
        " person who gave this instruction: install the app for {owner} (or request it): {url}"
    ),
    "approve_app": (
        "The organisation {owner} restricts access by OAuth Apps, so the connection does not reach {repo}. Next step"
        " for the person who gave this instruction: request (or, as an owner, grant) the approval: {url}"
    ),
    "authorize_sso": (
        "The organisation {owner} requires SAML single sign-on for {repo}. Next step for the person who gave this"
        " instruction: authorise the GitHub connection for {owner}: {url}"
    ),
    "new_token": (
        "The GitHub token of the person who gave this instruction does not reach {repo}. Next step for that person:"
        " create a token that reaches {owner} and store it (Settings, GitHub, Use an existing token): {url}"
    ),
    "check_access": (
        "The GitHub account of the person who gave this instruction has no access to {repo} (or it does not exist)."
        " Next step for that person: open {url} and ask its owner for access, or connect another GitHub account."
    ),
}


def fine_grained_url(web_url: str, owner: str) -> str:
    query = [("name", TOKEN_NAME), ("target_name", owner), ("expires_in", "none"), *FINE_GRAINED_PERMISSIONS]
    return f"{web_url}/settings/personal-access-tokens/new?{urlencode(query)}"


def classic_url(web_url: str) -> str:
    return f"{web_url}/settings/tokens/new?{urlencode([('scopes', CLASSIC_SCOPES), ('description', TOKEN_NAME)])}"


def _approval_url(client: GitHubClient) -> str:
    if client.source == "device_flow" and client.oauth_client_id is not None:
        return f"{client.web_url}/settings/connections/applications/{client.oauth_client_id}"
    return f"{client.web_url}/settings/applications"


def _missing(client: GitHubClient, repo: str, owner: str) -> dict[str, str]:
    """The step for a repository the token does not see (`404`)."""
    web = client.web_url
    if client.token_kind == "github_app":
        if client.oauth_client_id is not None:
            return {"action": "connect", "owner": owner}
        if client.app_slug is not None:
            return {"action": "install_app", "owner": owner, "url": f"{web}/apps/{client.app_slug}/installations/new"}
        return {"action": "new_token", "owner": owner, "url": classic_url(web)}
    if client.token_kind == "fine_grained":
        return {"action": "new_token", "owner": owner, "url": fine_grained_url(web, owner)}
    return {"action": "check_access", "owner": owner, "url": f"{web}/{repo}"}


def next_step(client: GitHubClient, repo: str, error: GitHubError) -> dict[str, str] | None:
    """The next step of an access refusal (module docstring), or None when `error` is not one."""
    owner = repo.split("/", 1)[0]
    web = client.web_url
    message = error.message.lower()
    if error.kind == "forbidden" and error.sso:
        return {"action": "authorize_sso", "owner": owner, "url": error.sso_url or f"{web}/orgs/{owner}/sso"}
    if error.kind == "forbidden" and OAUTH_RESTRICTED in message:
        return {"action": "approve_app", "owner": owner, "url": _approval_url(client)}
    if error.kind == "forbidden" and CLASSIC_FORBIDDEN in message:
        return {"action": "new_token", "owner": owner, "url": fine_grained_url(web, owner)}
    if error.kind == "not_found":
        return _missing(client, repo, owner)
    return None


def message(step: Mapping[str, Any], repo: str) -> str:
    """The fixed sentence of a step (at most 1024 printable ASCII characters: the URL is cut at the contract's limit
    of a message, never in the step itself)."""
    text = MESSAGES[step["action"]].format(repo=repo, owner=step["owner"], url=step.get("url", ""))
    return text[:1024]


def refusal(client: GitHubClient, repo: str, error: GitHubError) -> CheckFailed | None:
    """`repo_not_accessible` with the next step for an access refusal, else None."""
    step = next_step(client, repo, error)
    if step is None:
        return None
    return CheckFailed("repo_not_accessible", message=message(step, repo), next_step=step)
