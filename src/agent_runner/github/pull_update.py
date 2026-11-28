"""Executor `github.update_pull_request` (HOT-30; 05-runner.md section 12.1, 03-api.md section 9): a task changes the
title, the text, the draft state or the labels of a pull request in a repository it may use (the formal checks apply
the workgroup's repository list, the pre-check `precheck.repo_access` the resolved identity's access, as for every
`github.*` type). The type has no field that closes, merges or approves: merging stays `github.merge` with its own
rights profile, approving is no operation at all.

1. `GET /repos/{r}/pulls/{n}`: `404` is `not_found` and nothing is changed; the answer gives the pull request's node
   id and its draft state.
2. `title` or `body` given: `PATCH /repos/{r}/pulls/{n}` with exactly those fields.
3. `labels` given: `PATCH /repos/{r}/issues/{n} {labels}` (GitHub keeps a pull request's labels on its issue; the list
   replaces every label, `[]` removes them all).
4. `draft` given and different from the current state: GraphQL `convertPullRequestToDraft` (`true`) or
   `markPullRequestReadyForReview` (`false`) with the node id (`client.graphql`; the REST API cannot change it). The
   same state sends nothing. A GraphQL refusal is `upstream_error`, one of type `NOT_FOUND` `not_found`.
5. `GET /repos/{r}/pulls/{n}` again -> `{number, url, title, state, draft, labels}` after the change (a title beyond
   1024 characters is cut, labels outside the contract are left out; the result has no `truncated`).

An answer that does not fit the result contract is `upstream_error` (`ops.executor`).
"""

from __future__ import annotations

import re
from typing import Any

from agent_runner.github import ops
from agent_runner.github.client import GitHubClient
from agent_runner.github.issues import label_names
from agent_runner.github.pulls import MAX_TITLE, cut
from agent_runner.operations.extensions import OperationContext

NODE_ID_RE = re.compile(r"^[A-Za-z0-9_=+/-]{1,128}$")
TEXT_FIELDS = ("title", "body")
TO_DRAFT = "mutation($id: ID!) { convertPullRequestToDraft(input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
TO_READY = (
    "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
)


def result_of(pull: dict[str, Any]) -> dict[str, Any]:
    """The result fields of a pull request answer (`GitHubError` when it does not fit the contract)."""
    state = pull.get("state")
    if state not in ("open", "closed"):
        raise ops.invalid()
    title, _cut = cut(ops.text(pull.get("title"), re.compile(r"^.*$", re.S)), MAX_TITLE)
    labels, _left_out = label_names(pull.get("labels", []))
    return {
        "number": ops.integer(pull.get("number"), ops.MAX_NUMBER),
        "url": ops.text(pull.get("html_url"), ops.URL_RE),
        "title": title,
        "state": state,
        "draft": ops.flag(pull.get("draft")),
        "labels": labels,
    }


async def update_pull_request(ctx: OperationContext, client: GitHubClient) -> dict[str, Any]:
    params = ctx.params
    repo, number = str(params["repo"]), int(params["number"])
    path = f"/repos/{repo}/pulls/{number}"
    current = ops.obj((await client.request("GET", path)).data)
    texts = {name: params[name] for name in TEXT_FIELDS if name in params}
    if texts:
        await client.request("PATCH", path, body=texts)
    if "labels" in params:
        await client.request("PATCH", f"/repos/{repo}/issues/{number}", body={"labels": list(params["labels"])})
    if "draft" in params and bool(params["draft"]) != ops.flag(current.get("draft")):
        node = ops.text(current.get("node_id"), NODE_ID_RE)
        await client.graphql(TO_DRAFT if params["draft"] else TO_READY, {"id": node})
    return result_of(ops.obj((await client.request("GET", path)).data))


execute = ops.executor(update_pull_request)
