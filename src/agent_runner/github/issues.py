"""Executors of the issue operation types (KOR-25; 05-runner.md section 12.1, 03-api.md section 9,
`contracts/ops/objects.json`): REST with the acting user's credential (HOT-14), one GitHub page per listing
(`github.list_issues`: as many as it takes to fill a page of issues, bounded).

- `github.list_issues`: `per_page` and `page` count **issues** (HOT-09). GitHub lists pull requests among the issues
  of `GET /repos/{r}/issues` and cannot leave them out, so the executor reads that list from its start in GitHub pages
  of `SCAN_PER_PAGE` (`state`, `sort`, `direction`, `labels` passed on; defaults `open`, `created`, `desc`, 30, 1),
  skips the entries with a `pull_request` key and answers the issues `(page-1)*per_page` up to `page*per_page` of
  what is left. A page is empty only when there are no (further) issues; paging with `next_page` lists every issue
  once (as long as the list does not change meanwhile). `next_page` is `page + 1` while another issue follows (it
  reads on until it sees one or the list ends; a complete page at the flood limit names the next page as well). Flood
  limit: at most `SCAN_PAGES` GitHub pages per operation; a page that needs more is answered with what was found,
  `truncated: true`, `next_page: null` and a `note` that says how to go on (filter by labels or state, or turn the
  order around with `direction`). A page that does not fit the result budget loses its last issues (`pages.fit`); it
  gets a `note` as well (ask for a smaller `per_page`), since `next_page` goes on after them.
- `github.get_issue`: `GET /repos/{r}/issues/{n}` and one page of `GET /repos/{r}/issues/{n}/comments` (oldest
  first; default 100 per page); `pull_request` tells that the number is a pull request, `comments_total` is GitHub's
  comment count. The result stays within 200 KiB of body JSON: the newest comments of the page, then the body, then the
  title are cut (`pulls.fit`).
- `github.create_issue`: `POST /repos/{r}/issues {title, body?, labels?}` -> `{number, url, id}` (duplicates are held
  off by the dedupe tag of the registration: repo, sha256 of title and body).
- `github.update_issue`: `GET /repos/{r}/issues/{n}` first - a number that belongs to a pull request is `not_found`
  with `PULL_REQUEST` and nothing is changed (GitHub would edit or close the pull request through the issue route) -,
  then `PATCH /repos/{r}/issues/{n}` with exactly the given fields among `state`, `state_reason`, `title`, `body`,
  `labels` (the formal checks require one of them and `state_reason` only with `state`) -> the issue's
  `{number, url, state, state_reason, title, labels}` after the change (the result has no `truncated`: a title beyond
  1024 characters is cut silently).

Every entry is built from the fields the contract allows: titles beyond 1024 and texts beyond 262144 characters are
cut, labels and assignees that do not fit are left out, an unknown `state_reason` becomes `null` - each sets
`truncated`; an issue of a list that does not fit at all is left out (`truncated`, it still takes its place in the
page count), the single issue of `get_issue`,
`create_issue` and `update_issue` that does not fit is `upstream_error`. `404` is `not_found`, `410` (issues disabled
in the repository) `not_found` with "Issues are disabled in this repository." (`ops.failure`).
"""

from __future__ import annotations

import re
from typing import Any

from agent_runner.github import ops, pages
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.github.pulls import MAX_TEXT, MAX_TITLE, comment_entry, cut, fit
from agent_runner.operations.extensions import OperationContext, OperationFailed

STATES = frozenset({"open", "closed"})
STATE_REASONS = frozenset({"completed", "not_planned", "reopened", "duplicate"})
MAX_LABELS = 100
MAX_ASSIGNEES = 100
SCAN_PER_PAGE = 100
"""GitHub's largest page: the executor reads the raw list in pages of this size."""
SCAN_PAGES = 10
"""GitHub pages `github.list_issues` reads at most per operation (flood limit, HOT-09)."""
FIT_NOTE = (
    "The page was too large for one result and its last issues were left out; ask for this page again with a "
    "smaller per_page to see them."
)
SCAN_NOTE = (
    "Only the first {rows} entries of the list (issues and pull requests together) were read. To reach issues "
    "beyond them, filter by labels or state, or turn the order around with direction."
)
MAX_COUNT = 9_007_199_254_740_991
ANY_TEXT = re.compile(r"^.*$", re.S)
UPDATE_FIELDS = ("state", "state_reason", "title", "body", "labels")
PULL_REQUEST = "The number is a pull request, not an issue."


def _count(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= MAX_COUNT:
        raise ops.invalid()
    return value


def _state(value: object) -> str:
    if value not in STATES:
        raise ops.invalid()
    return str(value)


def _reason(value: object) -> tuple[str | None, bool]:
    """GitHub's `state_reason`: a known value, `null`, or an unknown value as `null` (cut)."""
    if value is None or value in STATE_REASONS:
        return (None if value is None else str(value)), False
    return None, True


def label_names(value: object) -> tuple[list[str], bool]:
    """Label names of label objects (or plain names); names outside 1-100 characters are left out (cut)."""
    if not isinstance(value, list):
        raise ops.invalid()
    names: list[str] = []
    shortened = False
    for item in value:
        name = item.get("name") if isinstance(item, dict) else item
        if not isinstance(name, str) or not 1 <= len(name) <= 100 or len(names) >= MAX_LABELS:
            shortened = True
            continue
        names.append(name)
    return names, shortened


def _assignees(value: object) -> tuple[list[str], bool]:
    if not isinstance(value, list):
        raise ops.invalid()
    logins: list[str] = []
    shortened = False
    for item in value:
        login = item.get("login") if isinstance(item, dict) else None
        if not isinstance(login, str) or not ops.LOGIN_RE.fullmatch(login) or len(logins) >= MAX_ASSIGNEES:
            shortened = True
            continue
        logins.append(login)
    return logins, shortened


def _closed_at(value: object) -> str | None:
    return None if value is None else ops.text(value, ops.TIME_RE)


def fields(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """The issue fields every issue result shares, and whether something was cut; `GitHubError` when the issue does
    not fit the contract."""
    title, cut_title = cut(ops.text(data.get("title"), ANY_TEXT), MAX_TITLE)
    reason, cut_reason = _reason(data.get("state_reason"))
    labels, cut_labels = label_names(data.get("labels"))
    assignees, cut_assignees = _assignees(data.get("assignees", []))
    found = {
        "number": ops.integer(data.get("number"), ops.MAX_NUMBER),
        "title": title,
        "state": _state(data.get("state")),
        "state_reason": reason,
        "author": ops.text(ops.obj(data.get("user")).get("login"), ops.LOGIN_RE),
        "labels": labels,
        "assignees": assignees,
        "created_at": ops.text(data.get("created_at"), ops.TIME_RE),
        "updated_at": ops.text(data.get("updated_at"), ops.TIME_RE),
        "closed_at": _closed_at(data.get("closed_at")),
        "url": ops.text(data.get("html_url"), ops.URL_RE),
    }
    return found, cut_title or cut_reason or cut_labels or cut_assignees


def _listed(item: object) -> tuple[dict[str, Any], bool] | None:
    try:
        data = ops.obj(item)
        found, shortened = fields(data)
        found["comments"] = _count(data.get("comments"))
    except GitHubError:
        return None
    return found, shortened


async def list_issues(ctx: OperationContext, client: GitHubClient) -> dict[str, Any]:
    params = ctx.params
    per_page, page = int(params.get("per_page", 30)), int(params.get("page", 1))
    query: dict[str, str | int] = {
        "state": str(params.get("state", "open")),
        "sort": str(params.get("sort", "created")),
        "direction": str(params.get("direction", "desc")),
        "per_page": SCAN_PER_PAGE,
        "page": 1,
    }
    if params.get("labels"):
        query["labels"] = ",".join(str(name) for name in params["labels"])
    first, end = (page - 1) * per_page, page * per_page
    issues: list[dict[str, Any]] = []
    truncated = False
    seen = 0  # issues of the list before the current entry
    more: bool | None = None  # another issue follows the page (None: not known yet)
    for _read in range(SCAN_PAGES):
        answer = await client.request("GET", f"/repos/{params['repo']}/issues", params=query)
        for item in pages.rows(answer):
            if isinstance(item, dict) and "pull_request" in item:
                continue
            index, seen = seen, seen + 1
            if index >= end:
                more = True
                break
            if index < first:
                continue
            made = _listed(item)
            if made is None:
                truncated = True
                continue
            issues.append(made[0])
            truncated = truncated or made[1]
        following = pages.next_page(answer.headers)
        if more or following is None:
            more = bool(more)
            break
        query["page"] = following
    result: dict[str, Any] = {"issues": issues, "next_page": None, "truncated": truncated}
    if more is None and seen < end:
        # the flood limit ended the reading before the page was complete
        result["truncated"] = True
        result["note"] = SCAN_NOTE.format(rows=SCAN_PAGES * SCAN_PER_PAGE)
    elif more is not False and page < pages.MAX_PAGE:
        result["next_page"] = page + 1
    listed = len(issues)
    pages.fit(result, "issues")
    if len(issues) < listed:
        result["note"] = FIT_NOTE
    return result


async def get_issue(ctx: OperationContext, client: GitHubClient) -> dict[str, Any]:
    params = ctx.params
    repo, number = str(params["repo"]), int(params["number"])
    issue = ops.obj((await client.request("GET", f"/repos/{repo}/issues/{number}")).data)
    result, truncated = fields(issue)
    text = issue.get("body")
    body: str | None = None
    if isinstance(text, str):
        body, cut_body = cut(text, MAX_TEXT)
        truncated = truncated or cut_body
    query = {"per_page": int(params.get("comments_per_page", 100)), "page": int(params.get("comments_page", 1))}
    answer = await client.request("GET", f"/repos/{repo}/issues/{number}/comments", params=query)
    comments: list[dict[str, Any]] = []
    for item in pages.rows(answer):
        made = comment_entry(item)
        if made is None:
            truncated = True
            continue
        comments.append(made[0])
        truncated = truncated or made[1]
    result.update(
        body=body,
        pull_request="pull_request" in issue,
        comments_total=_count(issue.get("comments")),
        comments=comments,
        comments_next_page=pages.next_page(answer.headers),
        truncated=truncated,
    )
    fit(result, ("comments", "body", "title"))
    return result


async def create_issue(ctx: OperationContext, client: GitHubClient) -> dict[str, Any]:
    params = ctx.params
    body: dict[str, object] = {"title": params["title"]}
    for name in ("body", "labels"):
        if name in params:
            body[name] = params[name]
    data = ops.obj((await client.request("POST", f"/repos/{params['repo']}/issues", body=body)).data)
    return {
        "number": ops.integer(data.get("number"), ops.MAX_NUMBER),
        "url": ops.text(data.get("html_url"), ops.URL_RE),
        "id": ops.integer(data.get("id")),
    }


async def update_issue(ctx: OperationContext, client: GitHubClient) -> dict[str, Any]:
    params = ctx.params
    repo, number = str(params["repo"]), int(params["number"])
    current = ops.obj((await client.request("GET", f"/repos/{repo}/issues/{number}")).data)
    if "pull_request" in current:
        raise OperationFailed("not_found", message=PULL_REQUEST)
    body = {name: params[name] for name in UPDATE_FIELDS if name in params}
    data = ops.obj((await client.request("PATCH", f"/repos/{repo}/issues/{number}", body=body)).data)
    found, _cut = fields(data)  # the result contract has no `truncated`: a title beyond 1024 characters is only cut
    return {name: found[name] for name in ("number", "url", "state", "state_reason", "title", "labels")}


execute_list = ops.executor(list_issues)
execute_get = ops.executor(get_issue)
execute_create = ops.executor(create_issue)
execute_update = ops.executor(update_issue)
