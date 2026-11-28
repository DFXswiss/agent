"""U-DBL-github (HOT-30): `PATCH …/pulls/{number}` and the GraphQL draft mutations of the GitHub double."""

from __future__ import annotations

import base64
from collections.abc import Callable
from typing import Any

from .test_server import REPO, issue_seed

TO_DRAFT = "mutation($id: ID!) { convertPullRequestToDraft(input: {pullRequestId: $id}) { pullRequest { isDraft } } }"
TO_READY = "mutation($id: ID!) { markPullRequestReadyForReview(input: {pullRequestId: $id}) { pullRequest { isDraft } } }"


def mutate(gh: Any, query: str, node: object, **kwargs: Any) -> Any:
    response = gh.api("POST", "/graphql", {"query": query, "variables": {"id": node}}, **kwargs)
    assert response.status == 200, response.body
    return response.json()


def test_update_pull_changes_title_body_and_state(start_github: Callable[..., Any], seed: Callable[[], Any]) -> None:
    gh = start_github(issue_seed(seed))
    before = gh.get(f"{REPO}/pulls/4")

    def patch(body: dict[str, Any]) -> Any:
        response = gh.api("PATCH", f"{REPO}/pulls/4", body)
        assert response.status == 200, response.body
        return response.json()

    renamed = patch({"title": "Renamed", "body": "Text."})
    assert (renamed["title"], renamed["body"], renamed["state"]) == ("Renamed", "Text.", "open")
    assert renamed["updated_at"] > before["updated_at"]
    assert [label["name"] for label in renamed["labels"]] == ["bug"]
    kept = patch({})
    assert (kept["title"], kept["body"]) == ("Renamed", "Text.")
    closed = patch({"state": "closed", "body": None})
    assert (closed["state"], closed["body"], closed["closed_at"] is not None) == ("closed", None, True)
    reopened = patch({"state": "open"})
    assert (reopened["state"], reopened["closed_at"]) == ("open", None)
    assert gh.api("PATCH", f"{REPO}/pulls/9", {"title": "x"}).status == 404
    assert gh.state()["ok"] is True
    for body, field in (({"title": ""}, "title"), ({"state": "merged"}, "state")):
        refused = gh.api("PATCH", f"{REPO}/pulls/4", body)
        assert (refused.status, refused.json()["errors"][0]["field"]) == (422, field)
    # GitHub ignores `draft` on this route: the double counts a client that sends it as a failure
    assert gh.api("PATCH", f"{REPO}/pulls/4", {"draft": True}).json()["draft"] is False
    assert gh.state()["failures"] == [
        "PATCH /repos/{owner}/{repo}/pulls/{number}: draft is ignored by GitHub (GraphQL changes it)"
    ]


def test_graphql_converts_to_draft_and_marks_ready(start_github: Callable[..., Any], seed: Callable[[], Any]) -> None:
    gh = start_github(issue_seed(seed))
    node = gh.get(f"{REPO}/pulls/4")["node_id"]
    drafted = mutate(gh, TO_DRAFT, node)
    assert drafted == {"data": {"convertPullRequestToDraft": {"pullRequest": {"isDraft": True}}}}
    assert gh.get(f"{REPO}/pulls/4")["draft"] is True
    ready = mutate(gh, TO_READY, node)
    assert ready == {"data": {"markPullRequestReadyForReview": {"pullRequest": {"isDraft": False}}}}
    assert gh.get(f"{REPO}/pulls/4")["draft"] is False
    unknown = mutate(gh, TO_DRAFT, base64.b64encode(b"PullRequest:1").decode())
    assert unknown["data"] == {"convertPullRequestToDraft": None}
    assert unknown["errors"][0]["type"] == "NOT_FOUND"
    assert unknown["errors"][0]["path"] == ["convertPullRequestToDraft"]
    # another user without the installation does not see the node's repository
    hidden = mutate(gh, TO_DRAFT, node, token="double-other-token")
    assert hidden["errors"][0]["type"] == "NOT_FOUND"
    gh.api("PATCH", f"{REPO}/pulls/4", {"state": "closed"})
    closed = mutate(gh, TO_DRAFT, node)
    assert closed["errors"][0]["type"] == "UNPROCESSABLE"
    assert gh.get(f"{REPO}/pulls/4")["draft"] is False
    assert gh.state()["ok"] is True


def test_graphql_needs_the_right_to_write_pull_requests(
    start_github: Callable[..., Any], seed: Callable[[], Any]
) -> None:
    data = issue_seed(seed)
    data["installations"][0]["permissions"] = {"contents": "write", "issues": "write", "pull_requests": "read"}
    gh = start_github(data)
    node = gh.get(f"{REPO}/pulls/4")["node_id"]
    refused = mutate(gh, TO_DRAFT, node)
    assert refused["errors"][0]["type"] == "FORBIDDEN"
    assert gh.api("PATCH", f"{REPO}/pulls/4", {"title": "x"}).status == 403
    assert gh.get(f"{REPO}/pulls/4")["draft"] is False
    assert gh.state()["ok"] is True


def test_graphql_answers_only_the_draft_mutations(start_github: Callable[..., Any], seed: Callable[[], Any]) -> None:
    gh = start_github(issue_seed(seed))
    for body in (
        {"query": "query { viewer { login } }", "variables": {}},
        {"query": TO_DRAFT, "variables": None},
        {"query": 5},
    ):
        answer = gh.api("POST", "/graphql", body)
        assert answer.status == 200
        assert answer.json()["errors"][0]["type"] == "UNSUPPORTED"
        assert "data" not in answer.json()
    assert gh.state()["failures"] == ["POST /graphql: unsupported GraphQL request"] * 3
