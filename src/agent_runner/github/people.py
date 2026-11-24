"""The humans of a task as GitHub co-authors (KOR-47, ADR-078 section 4).

`account(context, user)` is `(GitHub account ID, login)` of the user's **own** primary identity
(`credentials.acting.attribution`: attribution, not use of the connection - only the account ID and login are read,
never the token), None when the user has none, it does not open, its body has no valid `user_id`/`login`, or it is a
copy of the operator's account: `source:"operator"` or a foreign copy (`acting.attribution`), or - on a platform
device with `OPERATOR_GITHUB_TOKEN_FILE` - the account ID of the operator's token (`operator_account_id`). That ID
comes from `GET /user` with the operator's token (`operator.read_token`), once per daemon; a failed lookup is not
remembered and counts as unknown (only `source:"operator"` decides then).

The humans who may be named are the authors of the task's delivered prompts (`acting.instructors`).
"""

from __future__ import annotations

import re
from typing import Any

from agent_runner.credentials import acting
from agent_runner.github import operator, ops
from agent_runner.github.client import GitHubError, settings_for

ATTRIBUTE = "operator_github_account"
LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")


def _line(context: Any, text: str) -> None:
    context.log.write(f"runner.github {text}\n")
    context.log.flush()


async def operator_account_id(context: Any) -> int | None:
    """The GitHub account ID of the operator's token on a platform device (module docstring)."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, int):
        return found
    path = context.config.operator_github_token_file
    if context.identity.device.kind != "platform" or path is None:
        return None
    try:
        token = operator.read_token(path, operator.state_for(context), lambda text: _line(context, text))
        async with settings_for(context).client(token, None) as client:
            del token
            answer = await client.request("GET", "/user")
        account = ops.integer(ops.obj(answer.data).get("id"))
    except (operator.Unusable, GitHubError):
        _line(context, "operator_account unknown")
        return None
    setattr(context, ATTRIBUTE, account)
    return account


def login_of(body: dict[str, Any]) -> tuple[int, str] | None:
    login, user_id = body.get("login"), body.get("user_id")
    if not isinstance(login, str) or not LOGIN_RE.fullmatch(login):
        return None
    if not isinstance(user_id, int) or isinstance(user_id, bool) or not 1 <= user_id <= ops.MAX_GITHUB_ID:
        return None
    return user_id, login


async def account(context: Any, user: str) -> tuple[int, str] | None:
    """See the module docstring."""
    found = await acting.attribution(context, user)
    if found is None or found[0] == await operator_account_id(context):
        return None
    return found
