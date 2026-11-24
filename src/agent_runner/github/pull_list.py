"""Executor `github.list_pull_requests` (KOR-25; 05-runner.md section 12.1, 03-api.md section 9,
`contracts/ops/objects.json` `result_github_list_pull_requests`).

`GET /repos/{r}/pulls?state&sort&direction&per_page&page[&base]` (defaults `open`, `created`, `desc`, 30, 1): exactly
one GitHub page, `next_page` from the `Link` header (`pages.next_page`). Each entry carries number, title (cut to 1024
characters, `truncated`), state, draft, author, the head and base branch names, the times and the URL; an entry that
does not fit the contract (e.g. a head branch name outside the branch rules) is left out (`truncated`). The result
stays within 200 KiB of body JSON (`pages.fit`). Read the files and comments of one with `github.get_pull_request`.
"""

from __future__ import annotations

from typing import Any

from agent_runner.github import ops, pages
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.github.issues import ANY_TEXT
from agent_runner.github.pulls import MAX_TITLE, cut
from agent_runner.operations.extensions import OperationContext
from agent_runner.operations.rules import branch_valid

MAX_PULLS = 100


def _branch(value: object) -> str:
    ref = ops.obj(value).get("ref")
    if not branch_valid(ref):
        raise ops.invalid()
    return str(ref)


def _entry(item: object) -> tuple[dict[str, Any], bool] | None:
    try:
        data = ops.obj(item)
        title, shortened = cut(ops.text(data.get("title"), ANY_TEXT), MAX_TITLE)
        state = data.get("state")
        if state not in ("open", "closed"):
            raise ops.invalid()
        entry = {
            "number": ops.integer(data.get("number"), ops.MAX_NUMBER),
            "title": title,
            "state": state,
            "draft": ops.flag(data.get("draft")),
            "author": ops.text(ops.obj(data.get("user")).get("login"), ops.LOGIN_RE),
            "head": _branch(data.get("head")),
            "base": _branch(data.get("base")),
            "created_at": ops.text(data.get("created_at"), ops.TIME_RE),
            "updated_at": ops.text(data.get("updated_at"), ops.TIME_RE),
            "url": ops.text(data.get("html_url"), ops.URL_RE),
        }
    except GitHubError:
        return None
    return entry, shortened


async def list_pull_requests(ctx: OperationContext, client: GitHubClient) -> dict[str, Any]:
    params = ctx.params
    query: dict[str, str | int] = {
        "state": str(params.get("state", "open")),
        "sort": str(params.get("sort", "created")),
        "direction": str(params.get("direction", "desc")),
        "per_page": int(params.get("per_page", 30)),
        "page": int(params.get("page", 1)),
    }
    if "base" in params:
        query["base"] = str(params["base"])
    answer = await client.request("GET", f"/repos/{params['repo']}/pulls", params=query)
    found: list[dict[str, Any]] = []
    truncated = False
    for item in pages.rows(answer):
        made = _entry(item)
        if made is None or len(found) >= MAX_PULLS:
            truncated = True
            continue
        found.append(made[0])
        truncated = truncated or made[1]
    result = {"pull_requests": found, "next_page": pages.next_page(answer.headers), "truncated": truncated}
    pages.fit(result, "pull_requests")
    return result


execute = ops.executor(list_pull_requests)
