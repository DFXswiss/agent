"""Executor `github.comment` (05-runner.md section 12.1, 03-api.md section 9):
`POST /repos/{r}/issues/{n}/comments {body}` -> `{comment_id, url}`; an unknown issue or pull request is `not_found`.
With `a38_review: true` (FIX-02) the comment is posted only when the A38 guard would count it (`a38.check`); the
result then carries `a38 {run, head, signal?, timings_ms}`."""

from __future__ import annotations

import time
from typing import Any

from agent_runner.github import a38, ops
from agent_runner.github.client import GitHubClient
from agent_runner.operations.extensions import OperationContext


async def comment(ctx: OperationContext, client: GitHubClient) -> dict[str, Any]:
    repo, number, body = str(ctx.params["repo"]), int(ctx.params["number"]), str(ctx.params["body"])
    checked = await a38.check(ctx, client, repo, number, body) if ctx.params.get("a38_review") is True else None
    post_began = time.monotonic()
    answer = await client.request("POST", f"/repos/{repo}/issues/{number}/comments", body={"body": body})
    data = ops.obj(answer.data)
    result: dict[str, Any] = {
        "comment_id": ops.integer(data.get("id")),
        "url": ops.text(data.get("html_url"), ops.URL_RE),
    }
    if checked is not None:
        result["a38"] = a38.finish(ctx, checked, post_began)
    return result


execute = ops.executor(comment)
