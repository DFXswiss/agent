"""Executor `github.comment` (05-runner.md section 12.1, 03-api.md section 9):
`POST /repos/{r}/issues/{n}/comments {body}` -> `{comment_id, url}`; an unknown issue or pull request is `not_found`."""

from __future__ import annotations

from typing import Any

from agent_runner.github import ops
from agent_runner.github.client import GitHubClient
from agent_runner.operations.extensions import OperationContext


async def comment(ctx: OperationContext, client: GitHubClient) -> dict[str, Any]:
    repo, number = str(ctx.params["repo"]), int(ctx.params["number"])
    answer = await client.request(
        "POST", f"/repos/{repo}/issues/{number}/comments", body={"body": str(ctx.params["body"])}
    )
    data = ops.obj(answer.data)
    return {"comment_id": ops.integer(data.get("id")), "url": ops.text(data.get("html_url"), ops.URL_RE)}


execute = ops.executor(comment)
