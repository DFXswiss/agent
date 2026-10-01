"""Author review-completion declaration checked by dfx pr guard.

The guard does not run the review lanes and does not execute pull-request
code. A passing declaration is a consistent author statement plus the
guard's own thread check, not cryptographic proof that a review ran.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

REVIEW_BEGIN = "<!-- A38-REVIEW:v1 -->"
REVIEW_END = "<!-- /A38-REVIEW:v1 -->"
LIFECYCLE_REASON = "Review completion missing"
LANE_IDS = ("conformity-a", "logic-a", "conformity-b", "logic-b")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_NA_EVIDENCE = re.compile(
    r"^markdown-only rebase, previously approved at ([0-9a-f]{40})$"
)
_FENCE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
_PASS_KEYS = frozenset({"id", "result", "status"})
_NA_KEYS = frozenset({"id", "result", "evidence"})
_OBJECT_KEYS = frozenset({"schema", "head", "passes", "defects", "lanes"})


class ReviewError(ValueError):
    """The comment is not a readable a38-review/v1 block."""


def looks_like_review(body: str | None) -> bool:
    return isinstance(body, str) and "A38-REVIEW:v1" in body


def _reject_nonfinite(name: str) -> None:
    raise ReviewError(f"JSON contains non-finite number: {name}")


def _pairs_no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ReviewError(f"JSON contains duplicate key: {key}")
        out[key] = value
    return out


def loads_strict(text: str) -> Any:
    try:
        return json.loads(
            text,
            parse_constant=_reject_nonfinite,
            object_pairs_hook=_pairs_no_duplicates,
        )
    except ReviewError:
        raise
    except json.JSONDecodeError as exc:
        raise ReviewError(f"JSON is invalid: {exc.msg}") from exc


def parse_review_block(body: str) -> dict[str, Any]:
    if not isinstance(body, str) or not body:
        raise ReviewError("review comment body missing")
    if body.count(REVIEW_BEGIN) != 1 or body.count(REVIEW_END) != 1:
        raise ReviewError("review markers missing or duplicated")
    begin = body.find(REVIEW_BEGIN)
    end = body.find(REVIEW_END)
    if begin > end:
        raise ReviewError("review markers out of order")
    inside = body[begin + len(REVIEW_BEGIN) : end]
    fences = _FENCE.findall(inside)
    if len(fences) != 1:
        raise ReviewError("review block must contain one json fence")
    payload = loads_strict(fences[0].strip())
    if not isinstance(payload, dict):
        raise ReviewError("review block must be a JSON object")
    return payload


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _add(reasons: list[str], text: str) -> None:
    if text not in reasons:
        reasons.append(text)


def validate_visible(body: str, passes: int) -> list[str]:
    if not isinstance(body, str) or not _is_int(passes) or passes < 1:
        return ["review visible text missing"]
    begin = body.find(REVIEW_BEGIN)
    end = body.find(REVIEW_END)
    visible = body
    if 0 <= begin < end:
        visible = body[:begin] + body[end + len(REVIEW_END) :]
    lines = visible.splitlines()
    en_sentence = f"Ready after {passes} review passes."
    de_sentence = f"Bereit nach {passes} Review-Durchläufen."

    def next_content(start: int) -> int:
        """Next non-blank line. Blank lines may separate the required lines; other text may not."""
        for index in range(start, len(lines)):
            if lines[index].strip():
                return index
        return -1

    en_at = next_content(0)
    ready_at = next_content(en_at + 1) if en_at >= 0 and lines[en_at] == "EN:" else -1
    summary_at = next_content(ready_at + 1) if ready_at >= 0 and lines[ready_at] == en_sentence else -1
    if summary_at >= 0 and lines[summary_at] == "DE:":
        summary_at = -1
    de_at = next_content(summary_at + 1) if summary_at >= 0 else -1
    if de_at >= 0 and lines[de_at] != "DE:":
        de_at = -1
    de_sentence_at = next_content(de_at + 1) if de_at >= 0 else -1
    if de_sentence_at >= 0 and lines[de_sentence_at] != de_sentence:
        de_sentence_at = -1
    de_summary = next_content(de_sentence_at + 1) if de_sentence_at >= 0 else -1
    if min(en_at, ready_at, summary_at, de_at, de_sentence_at, de_summary) < 0:
        return ["review visible text missing"]
    return []


def _validate_lanes(
    lanes: list[Any], *, head: str, markdown_only: bool
) -> list[str]:
    reasons: list[str] = []
    if len(lanes) != 4:
        return ["review lanes must be the four required ids"]
    seen: list[str] = []
    kinds: set[str] = set()
    na_shas: list[str] = []
    for lane in lanes:
        if not isinstance(lane, Mapping):
            _add(reasons, "review lane is not an object")
            continue
        lane_id = lane.get("id")
        if not isinstance(lane_id, str) or lane_id not in LANE_IDS or lane_id in seen:
            _add(reasons, "review lanes must be the four required ids")
        elif isinstance(lane_id, str):
            seen.append(lane_id)
        result = lane.get("result")
        if result == "pass":
            kinds.add("pass")
            if set(lane) != _PASS_KEYS or lane.get("status") != "complete":
                _add(reasons, "review pass lane is malformed")
        elif result == "n_a":
            kinds.add("n_a")
            if set(lane) != _NA_KEYS:
                _add(reasons, "review n_a lane is malformed")
            else:
                evidence = lane.get("evidence")
                match = _NA_EVIDENCE.fullmatch(evidence) if isinstance(evidence, str) else None
                if match is None:
                    _add(reasons, "review n_a evidence is malformed")
                else:
                    sha = match.group(1)
                    if sha == head:
                        _add(reasons, "review n_a SHA equals the current head")
                    na_shas.append(sha)
        else:
            _add(reasons, "review lane result is invalid")
    if len(seen) != 4:
        _add(reasons, "review lanes must be the four required ids")
    if "pass" in kinds and "n_a" in kinds:
        _add(reasons, "review lanes mix pass and n_a")
    if "n_a" in kinds:
        if not markdown_only:
            _add(reasons, "review n_a requires a markdown-only change set")
        if len(set(na_shas)) > 1:
            _add(reasons, "review n_a SHAs differ")
    return reasons


def validate_declaration(
    payload: Mapping[str, Any], *, head: str, markdown_only: bool
) -> list[str]:
    if not isinstance(payload, Mapping):
        return ["review declaration is not an object"]
    reasons: list[str] = []
    if set(payload) - _OBJECT_KEYS:
        _add(reasons, "review declaration has unknown keys")
    if payload.get("schema") != "a38-review/v1":
        _add(reasons, "review schema is not a38-review/v1")
    declared = payload.get("head")
    if not isinstance(declared, str) or _SHA.fullmatch(declared) is None:
        _add(reasons, "review head is not a 40-hex SHA")
    elif declared != head:
        _add(reasons, "review head does not match the pull request")
    passes = payload.get("passes")
    if not _is_int(passes) or passes < 1:
        _add(reasons, "review passes must be an integer >= 1")
    if payload.get("defects") != 0 or not _is_int(payload.get("defects")):
        _add(reasons, "review defects must be 0")
    lanes = payload.get("lanes")
    if not isinstance(lanes, list):
        _add(reasons, "review lanes missing")
    else:
        reasons.extend(_validate_lanes(lanes, head=head, markdown_only=markdown_only))
    return reasons


def _utc(value: str) -> datetime | None:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _comment_stamp(comment: Mapping[str, Any]) -> datetime | None:
    """When the author last set this comment: the later of created and updated."""
    created_raw = comment.get("created_at")
    updated_raw = comment.get("updated_at")
    created = _utc(created_raw) if isinstance(created_raw, str) else None
    updated = _utc(updated_raw) if isinstance(updated_raw, str) else None
    if created is None:
        return updated
    if updated is None:
        return created
    return updated if updated >= created else created


def _declaration_reasons(body: str, *, head: str, markdown_only: bool) -> list[str]:
    reasons: list[str] = []
    try:
        payload = parse_review_block(body)
    except ReviewError as exc:
        _add(reasons, str(exc))
        return reasons
    reasons.extend(validate_declaration(payload, head=head, markdown_only=markdown_only))
    passes = payload.get("passes")
    if _is_int(passes) and passes >= 1:
        reasons.extend(validate_visible(body, passes))
    else:
        _add(reasons, "review visible text missing")
    return reasons


def select_review_comment(
    comments: Sequence[Mapping[str, Any]],
    *,
    author_id: int,
    guard_user_id: int,
    committed_at: str,
    head: str,
    markdown_only: bool,
) -> tuple[Mapping[str, Any] | None, list[str]]:
    """Latest valid author declaration created or edited after the head commit.

    Other comments, including later ones, do not remove an earlier valid
    declaration. A newer malformed declaration falls back to an older valid one.
    """
    committed = _utc(committed_at)
    if committed is None:
        return None, ["review commit time unavailable"]
    candidates: list[tuple[datetime, int, Mapping[str, Any]]] = []
    for comment in comments:
        user = comment.get("user") if isinstance(comment, Mapping) else None
        if not isinstance(user, Mapping):
            return None, ["review comment inventory invalid"]
        if user.get("id") == guard_user_id:
            continue
        if not isinstance(comment.get("id"), int) or not isinstance(comment.get("created_at"), str):
            return None, ["review comment inventory invalid"]
        if user.get("id") != author_id:
            continue
        body = comment.get("body")
        if not looks_like_review(body if isinstance(body, str) else None):
            continue
        stamp = _comment_stamp(comment)
        if stamp is None or stamp <= committed:
            continue
        candidates.append((stamp, int(comment["id"]), comment))
    if not candidates:
        return None, ["review completion comment missing"]
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    newest_reasons: list[str] = []
    for _stamp, _cid, comment in candidates:
        body = comment.get("body")
        text = body if isinstance(body, str) else ""
        reasons = _declaration_reasons(text, head=head, markdown_only=markdown_only)
        if not reasons:
            return comment, []
        if not newest_reasons:
            newest_reasons = reasons
    return None, newest_reasons


def _is_bot(author: Any) -> bool:
    if not isinstance(author, Mapping):
        return False
    if author.get("__typename") == "Bot":
        return True
    login = author.get("login")
    return isinstance(login, str) and login.endswith("[bot]")


def _comment_nodes(thread: Mapping[str, Any]) -> list[Mapping[str, Any]] | None:
    comments = thread.get("comments")
    if not isinstance(comments, Mapping):
        return None
    nodes = comments.get("nodes")
    if not isinstance(nodes, list):
        return None
    out: list[Mapping[str, Any]] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            return None
        out.append(node)
    return out


def _decision_marker(body: str) -> str | None:
    latest: str | None = None
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("User-Entscheid aufgehoben:"):
            latest = "off"
        elif stripped.startswith("User-Entscheid:"):
            latest = "on"
    return latest


def _open_thread_allowed(nodes: Sequence[Mapping[str, Any]]) -> bool:
    if not nodes:
        return False
    if _is_bot(nodes[0].get("author")):
        return False
    latest: str | None = None
    for node in nodes:
        body = node.get("body")
        if not isinstance(body, str):
            continue
        marker = _decision_marker(body)
        if marker is not None:
            latest = marker
    return latest == "on"


def unresolved_thread_reasons(threads: Sequence[Mapping[str, Any]]) -> list[str]:
    if isinstance(threads, (str, bytes)) or not isinstance(threads, Sequence):
        return ["review thread payload invalid"]
    blocked = False
    for thread in threads:
        if not isinstance(thread, Mapping) or not isinstance(thread.get("isResolved"), bool):
            return ["review thread payload invalid"]
        if thread["isResolved"]:
            continue
        nodes = _comment_nodes(thread)
        if nodes is None:
            return ["review thread payload invalid"]
        if not _open_thread_allowed(nodes):
            blocked = True
    if blocked:
        return ["unresolved review threads"]
    return []


_THREADS_QUERY = """
query($owner: String!, $name: String!, $number: Int!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 50, after: $cursor) {
        pageInfo { hasNextPage endCursor }
        nodes {
          isResolved
          comments(first: 100) {
            pageInfo { hasNextPage endCursor }
            nodes { body author { login __typename } }
          }
        }
      }
    }
  }
}
""".strip()


def _load_threads(api: Any, repo: str, number: int) -> list[str]:
    # A thread whose first comment page is truncated fails closed. Paging
    # every thread's comments would be a second query shape; 100 comments
    # is the cap this gate will read.
    if repo.count("/") != 1:
        return ["review thread query failed"]
    owner, name = repo.split("/", 1)
    nodes: list[Any] = []
    cursor: str | None = None
    for _page in range(20):
        try:
            status, data, _headers = api.request(
                "POST",
                "/graphql",
                body={
                    "query": _THREADS_QUERY,
                    "variables": {
                        "owner": owner,
                        "name": name,
                        "number": number,
                        "cursor": cursor,
                    },
                },
                retry=False,
            )
        except Exception:
            return ["review thread query failed"]
        if status != 200 or not isinstance(data, Mapping) or data.get("errors"):
            return ["review thread query failed"]
        repository = (data.get("data") or {}) if isinstance(data.get("data"), Mapping) else None
        if not isinstance(repository, Mapping):
            return ["review thread query failed"]
        pull = (repository.get("repository") or {}) if isinstance(repository.get("repository"), Mapping) else None
        if not isinstance(pull, Mapping):
            return ["review thread query failed"]
        pull_request = pull.get("pullRequest")
        connection = pull_request.get("reviewThreads") if isinstance(pull_request, Mapping) else None
        if not isinstance(connection, Mapping):
            return ["review thread query failed"]
        page = connection.get("nodes")
        info = connection.get("pageInfo")
        if not isinstance(page, list) or not isinstance(info, Mapping):
            return ["review thread query failed"]
        for node in page:
            comments = node.get("comments") if isinstance(node, Mapping) else None
            page_info = comments.get("pageInfo") if isinstance(comments, Mapping) else None
            # A missing page, a non-boolean flag, or another page all fail closed.
            if not isinstance(page_info, Mapping) or page_info.get("hasNextPage") is not False:
                return ["review thread query failed"]
        nodes.extend(page)
        if info.get("hasNextPage") is True:
            cursor = info.get("endCursor")
            if not isinstance(cursor, str) or not cursor:
                return ["review thread query failed"]
            continue
        if info.get("hasNextPage") is not False:
            return ["review thread query failed"]
        return unresolved_thread_reasons(nodes)
    return ["review thread query failed"]


def _head_committed_at(api: Any, repo: str, head: str) -> str | None:
    """Committer time of the current head. Missing or unreadable time fails closed."""
    if _SHA.fullmatch(head) is None:
        return None
    try:
        status, data, _headers = api.request("GET", f"/repos/{repo}/commits/{head}")
    except Exception:
        return None
    if status != 200 or not isinstance(data, Mapping):
        return None
    commit = data.get("commit")
    committer = commit.get("committer") if isinstance(commit, Mapping) else None
    date = committer.get("date") if isinstance(committer, Mapping) else None
    if not isinstance(date, str) or _utc(date) is None:
        return None
    return date


def evaluate_review_gate(
    api: Any,
    *,
    repo: str,
    number: int,
    head: str,
    author_id: int,
    comments: Sequence[Mapping[str, Any]],
    markdown_only: bool,
) -> tuple[bool, list[str]]:
    try:
        guard_id, _login = api.resolve_own_user()
    except Exception as exc:
        from .a38_guard import GuardError

        if not isinstance(exc, GuardError):
            raise
        return False, ["guard identity unavailable"]
    if not isinstance(guard_id, int):
        return False, ["guard identity unavailable"]
    reasons: list[str] = []
    committed_at = _head_committed_at(api, repo, head)
    if committed_at is None:
        _add(reasons, "review commit time unavailable")
    else:
        _comment, selected = select_review_comment(
            comments,
            author_id=author_id,
            guard_user_id=guard_id,
            committed_at=committed_at,
            head=head,
            markdown_only=markdown_only,
        )
        reasons.extend(selected)
    reasons.extend(_load_threads(api, repo, number))
    if reasons:
        return False, reasons
    return True, []
