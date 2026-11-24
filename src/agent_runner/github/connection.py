"""Executor `github.request_connection` (KOR-25; 03-api.md section 9, `contracts/ops/objects.json`
`result_github_request_connection`).

The session asks for the task owner's GitHub connection when its task needs GitHub but no other type fits yet. The
formal checks and the pre-check (`precheck.connection`) see whether the owner is connected; while not, the runner
holds the operation and asks the owner in the task's chat (KOR-25 part 3, `operations.pipeline`). Once connected the
executor answers `{login}` from the owner's credential body - no GitHub request. A missing owner, a credential that is
missing, refused or does not open, or a body without a valid login is `github_not_connected`; a hub that cannot be
asked for the record is `upstream_error` (retryable).
"""

from __future__ import annotations

import re
from typing import Any

from agent_runner.credentials.cache import CredentialUnavailable, opened
from agent_runner.github import ops
from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.sync.http import HubRefusal, HubTransportError

LOGIN_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")


async def execute(ctx: OperationContext) -> dict[str, Any]:
    owner = ctx.session.owner_id
    login: object = None
    try:
        if owner is not None:
            async with opened(ctx.context, owner, "github") as body:
                login = body.get("login")
    except (CredentialUnavailable, HubRefusal):
        login = None
    except HubTransportError:
        raise OperationFailed("upstream_error", retryable=True) from None
    if not isinstance(login, str) or not LOGIN_RE.fullmatch(login):
        raise OperationFailed("github_not_connected", message=ops.NOT_CONNECTED)
    return {"login": login}
