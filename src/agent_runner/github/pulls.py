"""Executors `github.get_pull_request` and `github.create_pull_request` (05-runner.md section 12.1, 03-api.md
section 9).

`get_pull_request`: `GET /repos/{r}/pulls/{n}`, `GET /repos/{r}/pulls/{n}/files` and
`GET /repos/{r}/issues/{n}/comments` (both paginated). Patches are included while their total stays within 1 MiB
**and** the whole result within `RESULT_BUDGET` bytes of body JSON (the result travels in one encrypted event of at
most 256 KiB, 04-crypto.md section 4.3); every patch left out sets `truncated`. Titles, bodies and comments are cut to
the contract's lengths (also `truncated`); file and comment entries that do not fit the contract are left out
(`truncated`). Before patches are added the rest is fitted into the budget as well (`fit`: newest comments, then the
body, then the last files, then the title).

`mergeable` and `mergeable_state` (KOR-48, ADR-084 section 2) are passed on as GitHub answers them (`mergeability`;
`mergeable` true, false or null, `mergeable_state` GitHub's word, e.g. `clean`, `dirty`, `draft`, `unknown`). GitHub
computes `mergeable` asynchronously and starts on the first read, so `read_pull` reads an open pull request whose
`mergeable` is still null again after 1 s and, if still null, after another 2 s (`REREAD_DELAYS`, the client's sleep);
then null is passed on (a client shows it as "being checked", never as "no conflict"). The device request
`github.pull_status` reads pull requests the same way (`github.pull_status`).

`create_pull_request`: `GET /repos/{r}/pulls?head=<owner>:<head>&base=<base>&state=open` first: an open pull request for
the same head and base ends the operation with it and `existing:true` (the pipeline answers MCP `duplicate`); otherwise
`POST /repos/{r}/pulls {title, head, base, body?, draft}` (`draft` defaulted from the workgroup settings by the formal
checks). A `422` because such a pull request was opened meanwhile is answered the same way; an unknown head or base is
`not_found`; other refusals are `upstream_error`. An identity that may only read the repository opens the pull request
from its fork when the fork holds `head` (the pre-check's `fork`, HOT-33): `head` is then `<fork owner>:<head>` and the
result names the fork (`fork`).
"""

from __future__ import annotations

import re
from typing import Any

from agent_runner.crypto.encoding import body_json
from agent_runner.github import ops
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.operations.extensions import OperationContext, OperationFailed
from agent_runner.operations.rules import branch_valid

PATCH_LIMIT = 1024 * 1024
RESULT_BUDGET = 200 * 1024
MAX_TITLE = 1024
MAX_TEXT = 262144
MAX_FILES = 3000
MAX_COMMENTS = 10000
FILE_STATUSES = frozenset({"added", "removed", "modified", "renamed", "copied", "changed", "unchanged"})
PATH_RE = re.compile(r"^[^/\x00-\x1f\x7f][^\x00-\x1f\x7f]{0,4095}$")
EXISTS_TEXT = "a pull request already exists"
REREAD_DELAYS = (1.0, 2.0)
"""Seconds before the second and the third read of an open pull request whose `mergeable` is still null."""
MERGEABLE_STATE_RE = re.compile(r"^[a-z_]{1,32}$")


def ref_of(value: object) -> dict[str, str]:
    data = ops.obj(value)
    ref = data.get("ref")
    if not branch_valid(ref):
        raise ops.invalid()
    return {"ref": str(ref), "sha": ops.text(data.get("sha"), ops.SHA_RE)}


def cut(value: str, limit: int) -> tuple[str, bool]:
    return (value[:limit], True) if len(value) > limit else (value, False)


def _file(item: object) -> tuple[dict[str, Any], str | None] | None:
    try:
        data = ops.obj(item)
        path = ops.text(data.get("filename"), PATH_RE)
        additions, deletions = data.get("additions"), data.get("deletions")
        counts_ok = all(isinstance(n, int) and not isinstance(n, bool) and n >= 0 for n in (additions, deletions))
    except GitHubError:
        return None
    status = data.get("status")
    if status not in FILE_STATUSES or not counts_ok:
        return None
    patch = data.get("patch")
    entry = {"path": path, "status": status, "additions": additions, "deletions": deletions}
    return entry, patch if isinstance(patch, str) else None


def comment_entry(item: object) -> tuple[dict[str, Any], bool] | None:
    try:
        data = ops.obj(item)
        entry = {
            "id": ops.integer(data.get("id")),
            "author": ops.text(ops.obj(data.get("user")).get("login"), ops.LOGIN_RE),
            "created_at": ops.text(data.get("created_at"), ops.TIME_RE),
        }
    except GitHubError:
        return None
    text = data.get("body")
    body, shortened = cut(text if isinstance(text, str) else "", MAX_TEXT)
    return {**entry, "body": body}, shortened


def _header(pull: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    title, cut_title = cut(ops.text(pull.get("title"), re.compile(r"^.*$", re.S)), MAX_TITLE)
    text = pull.get("body")
    body: str | None = None
    cut_body = False
    if isinstance(text, str):
        body, cut_body = cut(text, MAX_TEXT)
    state = pull.get("state")
    if state not in ("open", "closed"):
        raise ops.invalid()
    found = {
        "number": ops.integer(pull.get("number"), ops.MAX_NUMBER),
        "title": title,
        "body": body,
        "state": state,
        "draft": ops.flag(pull.get("draft")),
        "head": ref_of(pull.get("head")),
        "base": ref_of(pull.get("base")),
        "url": ops.text(pull.get("html_url"), ops.URL_RE),
    }
    return found, cut_title or cut_body


async def read_pull(client: GitHubClient, repo: str, number: int) -> dict[str, Any]:
    """`GET /repos/{repo}/pulls/{number}`, read again while the pull request is open and `mergeable` is null (module
    docstring); the last answer."""
    path = f"/repos/{repo}/pulls/{number}"
    pull = ops.obj((await client.request("GET", path)).data)
    for delay in REREAD_DELAYS:
        if pull.get("state") != "open" or pull.get("mergeable") is not None:
            break
        await client.sleep(delay)
        pull = ops.obj((await client.request("GET", path)).data)
    return pull


def mergeability(pull: dict[str, Any]) -> dict[str, Any]:
    """`{mergeable, mergeable_state}` of GitHub's answer (a missing state is `unknown`); another form is invalid."""
    value = pull.get("mergeable")
    if value is not None and not isinstance(value, bool):
        raise ops.invalid()
    return {"mergeable": value, "mergeable_state": ops.text(pull.get("mergeable_state", "unknown"), MERGEABLE_STATE_RE)}


async def get_pull_request(ctx: OperationContext, client: GitHubClient) -> dict[str, Any]:
    repo, number = str(ctx.params["repo"]), int(ctx.params["number"])
    pull = await read_pull(client, repo, number)
    result, truncated = _header(pull)
    result.update(mergeability(pull))
    files: list[tuple[dict[str, Any], str | None]] = []
    for item in await client.items(f"/repos/{repo}/pulls/{number}/files"):
        found = _file(item)
        if found is None or len(files) >= MAX_FILES:
            truncated = True
            continue
        files.append(found)
    comments: list[dict[str, Any]] = []
    for item in await client.items(f"/repos/{repo}/issues/{number}/comments"):
        made = comment_entry(item)
        if made is None or len(comments) >= MAX_COMMENTS:
            truncated = True
            continue
        comments.append(made[0])
        truncated = truncated or made[1]
    result["files"] = [entry for entry, _patch in files]
    result["comments"] = comments
    result["truncated"] = truncated
    used = fit(result)
    total = 0
    for entry, patch in files[: len(result["files"])]:
        if patch is None:
            continue
        size = len(body_json(patch)) + len(',"patch":')
        if total + len(patch.encode("utf-8")) > PATCH_LIMIT or used + size > RESULT_BUDGET:
            result["truncated"] = True
            continue
        entry["patch"] = patch
        total += len(patch.encode("utf-8"))
        used += size
    return result


def fit(result: dict[str, Any], keys: tuple[str, ...] = ("comments", "body", "files", "title")) -> int:
    """Shrink a result without patches to `RESULT_BUDGET` bytes of body JSON - by default the newest comments first,
    then the body, then the last files, then the title (`github.get_issue` passes its own `keys`); each step sets
    `truncated` - and return its size."""
    used = len(body_json(result))
    for key in keys:
        while used > RESULT_BUDGET and result[key]:
            value = result[key]
            if isinstance(value, list):
                value.pop()
            else:
                result[key] = value[: max(len(value) - (used - RESULT_BUDGET), len(value) // 2)]
            result["truncated"] = True
            used = len(body_json(result))
    return used


def _created(data: object, existing: bool) -> dict[str, Any]:
    pull = ops.obj(data)
    found: dict[str, Any] = {
        "number": ops.integer(pull.get("number"), ops.MAX_NUMBER),
        "url": ops.text(pull.get("html_url"), ops.URL_RE),
        "id": ops.integer(pull.get("id")),
        "draft": ops.flag(pull.get("draft")),
    }
    if existing:
        found["existing"] = True
    return found


async def _open_pull(client: GitHubClient, repo: str, head: str, base: str) -> dict[str, Any] | None:
    """The open pull request of `head` (a branch of `repo`, or `<owner>:<branch>` of a fork) into `base`."""
    label = head if ":" in head else f"{repo.split('/', 1)[0]}:{head}"
    params = {"head": label, "base": base, "state": "open"}
    answer = await client.request("GET", f"/repos/{repo}/pulls", params=params)
    if not isinstance(answer.data, list):
        raise ops.invalid()
    return _created(answer.data[0], True) if answer.data else None


async def create_pull_request(ctx: OperationContext, client: GitHubClient) -> dict[str, Any]:
    params = ctx.params
    repo, head, base = str(params["repo"]), str(params["head"]), str(params["base"])
    routed = ctx.data.get("fork")
    forked: dict[str, str] = {}
    if isinstance(routed, dict):  # the head is a branch of the identity's fork (HOT-33)
        head, forked = str(routed["head"]), {"fork": ops.text(routed["repo"], ops.REPO_RE)}
    existing = await _open_pull(client, repo, head, base)
    if existing is not None:
        return {**existing, **forked}
    body: dict[str, object] = {"title": params["title"], "head": head, "base": base, "draft": bool(params["draft"])}
    if "body" in params:
        body["body"] = params["body"]
    try:
        answer = await client.request("POST", f"/repos/{repo}/pulls", body=body)
    except GitHubError as error:
        if error.kind != "unprocessable":
            raise
        if EXISTS_TEXT in error.message.lower():
            found = await _open_pull(client, repo, head, base)
            if found is not None:
                return {**found, **forked}
        if "field:head" in error.message or "field:base" in error.message:
            raise OperationFailed("not_found", message="The head or base branch does not exist.") from None
        raise OperationFailed("upstream_error", message="GitHub refused to open the pull request.") from None
    return {**_created(answer.data, False), **forked}


execute_get = ops.executor(get_pull_request)
execute_create = ops.executor(create_pull_request)
