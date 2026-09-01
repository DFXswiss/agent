"""Tell which GitHub outputs existed before a job and which this instance added.

No execution, no I/O, no Store. The module builds `gh` argument vectors for a
caller to run, and parses already-decoded JSON. Later phases use the two
answers to tell real work from a self-reported claim of work.

The GraphQL `last:100` window can give a false negative under heavy activity
and, after deletions, a false positive against an older baseline. That limit
is inherited deliberately from the existing runner — do not try to fix it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

BASELINE_QUERY = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issueOrPullRequest(number:$number){__typename ... on Issue{comments(last:100){nodes{id}}}... on PullRequest{comments(last:100){nodes{id}}reviews(last:100){nodes{id state}}}}}}"
KINDS_QUERY = "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){issueOrPullRequest(number:$number){__typename ... on Issue{comments(last:100){nodes{id author{login} createdAt}}}... on PullRequest{comments(last:100){nodes{id author{login} createdAt}}reviews(last:100){nodes{id author{login} createdAt submittedAt state}}}}}}"


def _target(repo: str, ref: str) -> tuple[str, str, int]:
    """Split `repo` / `ref` into GraphQL variables, or raise ValueError."""
    if not isinstance(repo, str) or repo.count("/") != 1 or "" in repo.split("/"):
        raise ValueError("_target requires owner/name and a numeric ref")
    if not isinstance(ref, str):
        raise ValueError("_target requires owner/name and a numeric ref")
    # Issue and pull-request refs are written `#123` as often as `123`.
    number_text = ref[1:] if ref.startswith("#") else ref
    if not number_text.isdigit():
        raise ValueError("_target requires owner/name and a numeric ref")
    owner, name = repo.split("/")
    return owner, name, int(number_text)


def _graphql_argv(query: str, repo: str, ref: str) -> list[str]:
    owner, name, number = _target(repo, ref)
    return [
        "gh",
        "api",
        "graphql",
        "-f",
        f"query={query}",
        "-F",
        f"owner={owner}",
        "-F",
        f"name={name}",
        "-F",
        f"number={number}",
    ]


def baseline_argv(repo: str, ref: str) -> list[str]:
    """`gh` argv that lists comment and review ids on the issue or pull request."""
    return _graphql_argv(BASELINE_QUERY, repo, ref)


def kinds_argv(repo: str, ref: str) -> list[str]:
    """`gh` argv that lists comments and reviews with author and timestamps."""
    return _graphql_argv(KINDS_QUERY, repo, ref)


def _nodes(item: dict[str, Any], key: str) -> list[Any] | None:
    wrapper = item.get(key)
    if not isinstance(wrapper, dict):
        return None
    nodes = wrapper.get("nodes")
    if not isinstance(nodes, list):
        return None
    return nodes


def _typed_node(node: Any) -> tuple[dict[str, Any], str] | None:
    if not isinstance(node, dict):
        return None
    ident = node.get("id")
    if not isinstance(ident, str) or not ident:
        return None
    return node, ident


def _is_pending_review(node: dict[str, Any]) -> bool:
    # A PENDING review is a draft visible only to its author — here, this
    # instance — and its node id does not change when it is later submitted.
    # Leaving it in the baseline would make the eventual real review look
    # already known, so genuinely performed work would be recorded as not
    # performed. The same draft must not count as a new output either.
    return node.get("state") == "PENDING"


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if not isinstance(parsed, datetime):
        return None
    return parsed


def _timestamp(node: dict[str, Any]) -> datetime | None:
    # submittedAt takes precedence over createdAt for reviews. What counts is
    # submitting, not drafting: a review begun before the job started but
    # submitted during it carries an old createdAt and would otherwise not
    # count as new work.
    raw = node.get("submittedAt")
    if raw is None:
        raw = node.get("createdAt")
    return _parse_time(raw)


def _issue_or_pr(
    payload: Any,
) -> tuple[list[Any], list[Any] | None] | None:
    """Comment nodes, and review nodes when the item is a PullRequest.

    An Issue has no reviews — the second element is then None. Anything else,
    including a PullRequest whose `reviews.nodes` is not a list, fails closed.
    """
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    repository = data.get("repository")
    if not isinstance(repository, dict):
        return None
    item = repository.get("issueOrPullRequest")
    if not isinstance(item, dict):
        return None
    comments = _nodes(item, "comments")
    if comments is None:
        return None
    typename = item.get("__typename")
    if typename == "Issue":
        return comments, None
    if typename != "PullRequest":
        return None
    reviews = _nodes(item, "reviews")
    if reviews is None:
        return None
    return comments, reviews


def parse_baseline_ids(payload: Any) -> list[str] | None:
    """Ids that already existed: every comment, and every non-PENDING review."""
    parsed = _issue_or_pr(payload)
    if parsed is None:
        return None
    comments, reviews = parsed
    ids: list[str] = []
    for node in comments:
        typed = _typed_node(node)
        if typed is None:
            return None
        ids.append(typed[1])
    if reviews is None:
        return ids
    for node in reviews:
        typed = _typed_node(node)
        if typed is None:
            return None
        review, ident = typed
        if _is_pending_review(review):
            continue
        ids.append(ident)
    return ids


def _is_new(
    node: dict[str, Any],
    ident: str,
    *,
    login: str,
    baseline: list[str],
    since: datetime,
) -> bool | None:
    # A node this instance did not write must not be able to invalidate
    # the answer: its timestamp is unread so a third-party parse miss cannot force None.
    author = node.get("author")
    author_login = author.get("login") if isinstance(author, dict) else None
    if author_login != login:
        return False
    when = _timestamp(node)
    if when is None:
        return None
    try:
        in_window = when >= since
    except TypeError:
        return None
    return ident not in baseline and in_window


def parse_new_kinds(
    payload: Any, *, login: str, baseline: list[str], since: str
) -> set[str] | None:
    """Subset of ``{"comment", "review"}`` this instance published since `since`.

    An empty set means the payload was well-formed and nothing qualified. None
    means the payload cannot be trusted, not that there was no new work.
    """
    if not isinstance(login, str) or not isinstance(baseline, list):
        return None
    since_dt = _parse_time(since)
    if since_dt is None:
        return None
    parsed = _issue_or_pr(payload)
    if parsed is None:
        return None
    comments, reviews = parsed
    found: set[str] = set()
    for node in comments:
        typed = _typed_node(node)
        if typed is None:
            return None
        comment, ident = typed
        hit = _is_new(
            comment, ident, login=login, baseline=baseline, since=since_dt
        )
        if hit is None:
            return None
        if hit:
            found.add("comment")
    if reviews is None:
        return found
    for node in reviews:
        typed = _typed_node(node)
        if typed is None:
            return None
        review, ident = typed
        if _is_pending_review(review):
            continue
        hit = _is_new(
            review, ident, login=login, baseline=baseline, since=since_dt
        )
        if hit is None:
            return None
        if hit:
            found.add("review")
    return found
