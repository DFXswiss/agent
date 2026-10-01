"""Author review-completion declaration checked by dfx pr guard.

The guard does not run the review lanes and does not execute pull-request
code. A passing declaration is a consistent author statement plus the
guard's own thread check, not cryptographic proof that a review ran.
The statement must include the review record. The guard rejects a missing,
placeholder, or contradictory record. It does not prove which model ran.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

REVIEW_BEGIN = "<!-- A38-REVIEW:v1 -->"
REVIEW_END = "<!-- /A38-REVIEW:v1 -->"
LIFECYCLE_REASON = "Review completion missing"
# One full run is conformity plus logic of the same suffix. The other run is optional.
RUNS = (
    ("conformity-a", "logic-a"),
    ("conformity-b", "logic-b"),
)
LANE_IDS = tuple(lane_id for run in RUNS for lane_id in run)
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
    seen: list[str] = []
    kinds: set[str] = set()
    na_shas: list[str] = []
    present: set[str] = set()
    for lane in lanes:
        if not isinstance(lane, Mapping):
            _add(reasons, "review lane is not an object")
            continue
        lane_id = lane.get("id")
        if not isinstance(lane_id, str) or lane_id not in LANE_IDS or lane_id in seen:
            _add(reasons, "review lane id is invalid")
            continue
        seen.append(lane_id)
        present.add(lane_id)
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
    complete = 0
    for pair in RUNS:
        count = sum(1 for lane_id in pair if lane_id in present)
        if count == 2:
            complete += 1
        elif count == 1:
            _add(reasons, "review run is incomplete")
    if complete < 1:
        _add(reasons, "review lanes must include one full run")
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


_PLACEHOLDERS = frozenset({
    "not recorded",
    "unknown",
    "n/a",
    "na",
    "none",
    "-",
    "n.a.",
    "tbd",
    "todo",
})
_RECORD_PROMPT = "Review the diff and report each defect with its file and line."
_LANE_HEADER = re.compile(r"^Lane `([a-z0-9-]+)`:$")


def _is_placeholder(value: str, *, allow_none: bool = False) -> bool:
    banned = _PLACEHOLDERS - {"none"} if allow_none else _PLACEHOLDERS
    text = value.strip().casefold()
    return text in banned or text.rstrip(".") in banned


def _named_value(value: str) -> bool:
    text = value.strip()
    return 1 <= len(text) <= 200 and not _is_placeholder(text)


def _runs_line(lane_ids: Sequence[str]) -> str | None:
    present = set(lane_ids)
    complete = [pair for pair in RUNS if all(lane_id in present for lane_id in pair)]
    if len(complete) == 1:
        left, right = complete[0]
        return f"Runs: `{left}` with `{right}`. The second run may be omitted."
    if len(complete) == 2:
        return "Runs: `conformity-a` with `logic-a`, and `conformity-b` with `logic-b`."
    return None


def render_review_record(payload: Mapping[str, Any]) -> str:
    """Record lines the guard requires before the review machine block."""
    lanes = payload.get("lanes")
    if not isinstance(lanes, list):
        lanes = []
    lane_ids = [
        lane.get("id")
        for lane in lanes
        if isinstance(lane, Mapping) and isinstance(lane.get("id"), str)
    ]
    lines = [
        f"Head: `{payload.get('head')}`",
        _runs_line([lane_id for lane_id in lane_ids if isinstance(lane_id, str)]) or "",
        "",
    ]
    for lane in lanes:
        if not isinstance(lane, Mapping):
            continue
        lines.append(f"Lane `{lane.get('id')}`:")
        if lane.get("result") == "n_a":
            lines.append("Result: n_a")
        else:
            lines.extend([
                "Provider: Example",
                "Model: Example model",
                "Model number: example-1",
                "Result: pass",
                "Prompt:",
                "```text",
                _RECORD_PROMPT,
                "```",
            ])
        lines.append("")
    lines.append(f"Final result: `passes` {payload.get('passes')}, `defects` 0.")
    lines.append("Set aside: none.")
    lines.append("")
    return "\n".join(lines)


def _extract_text_fences(text: str) -> tuple[list[str], list[str], bool]:
    """Prose lines and ```text bodies. Any other fence is an error."""
    prose: list[str] = []
    prompts: list[str] = []
    error = False
    index = 0
    lines = text.splitlines()
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped == "```text":
            index += 1
            body: list[str] = []
            closed = False
            while index < len(lines):
                if lines[index].strip() == "```":
                    closed = True
                    index += 1
                    break
                if REVIEW_BEGIN in lines[index] or REVIEW_END in lines[index]:
                    error = True
                body.append(lines[index])
                index += 1
            if not closed:
                error = True
            prompts.append("\n".join(body))
            continue
        if stripped.startswith("```"):
            error = True
            index += 1
            while index < len(lines) and lines[index].strip() != "```":
                index += 1
            if index < len(lines):
                index += 1
            continue
        prose.append(lines[index])
        index += 1
    return prose, prompts, error


def _prompt_ok(text: str) -> bool:
    if REVIEW_BEGIN in text or REVIEW_END in text:
        return False
    stripped = text.strip()
    return len(stripped) >= 40 and not _is_placeholder(stripped)


def _pass_lane_reasons(section: list[str], prompt: str | None) -> list[str]:
    reasons: list[str] = []
    expected = (
        ("Provider: ", "review provider missing", False),
        ("Model: ", "review model missing", False),
        ("Model number: ", "review model number missing", False),
        ("Result: ", "review result missing", True),
    )
    if len(section) != 5:
        _add(reasons, "review lane record missing")
    for index, (prefix, reason, is_result) in enumerate(expected):
        if index >= len(section) or not section[index].startswith(prefix):
            _add(reasons, reason)
            continue
        value = section[index][len(prefix):].strip()
        if is_result:
            if "pass" not in value.casefold():
                _add(reasons, reason)
        elif not _named_value(value):
            _add(reasons, reason)
    if len(section) < 5 or section[4] != "Prompt:":
        _add(reasons, "review prompt missing")
    elif prompt is None or not _prompt_ok(prompt):
        _add(reasons, "review prompt missing")
    return reasons


def _na_lane_reasons(section: list[str]) -> list[str]:
    if section == ["Result: n_a"]:
        return []
    named = ("Provider:", "Model:", "Model number:", "Prompt:")
    if any(line.startswith(named) for line in section):
        return ["review n_a lane must not name a model"]
    return ["review result missing"]


def _set_aside_reasons(lines_after: list[str]) -> list[str]:
    if not lines_after:
        return ["review set aside missing"]
    first = lines_after[0]
    if first == "Set aside:":
        bullets = lines_after[1:]
        if bullets and all(item.startswith("- ") and item[2:].strip() for item in bullets):
            return []
        return ["review set aside missing"]
    if first.startswith("Set aside: "):
        value = first[len("Set aside: "):].strip()
        if value and not _is_placeholder(value, allow_none=True) and len(lines_after) == 1:
            return []
    return ["review set aside missing"]


def validate_review_record(body: str, payload: Mapping[str, Any]) -> list[str]:
    """Require the human record to agree with the declaration.

    Lines inside a ```text fence are the prompt, not record fields.
    """
    if not isinstance(body, str) or not isinstance(payload, Mapping):
        return ["review record missing"]
    begin = body.find(REVIEW_BEGIN)
    record = body[:begin] if begin >= 0 else body
    prose, prompts, fence_error = _extract_text_fences(record)
    lines = [line.strip() for line in prose if line.strip()]
    if not any(
        line.startswith(("Head:", "Runs:", "Lane `", "Final result:", "Set aside:"))
        for line in lines
    ):
        return ["review record missing"]
    reasons: list[str] = []
    if fence_error:
        _add(reasons, "review prompt missing")
    head = payload.get("head")
    heads = [line for line in lines if line.startswith("Head:")]
    if heads != [f"Head: `{head}`"]:
        _add(reasons, "review head line does not match")
    lanes = payload.get("lanes")
    lane_ids: list[str] = []
    lane_results: list[str] = []
    if isinstance(lanes, list):
        for lane in lanes:
            if isinstance(lane, Mapping) and isinstance(lane.get("id"), str):
                lane_ids.append(lane["id"])
                result = lane.get("result")
                lane_results.append(result if isinstance(result, str) else "")
            else:
                lane_ids.append("")
                lane_results.append("")
    runs = [line for line in lines if line.startswith("Runs:")]
    expected_runs = _runs_line(lane_ids)
    if expected_runs is None or runs != [expected_runs]:
        _add(reasons, "review runs line does not match")
    headers = [
        (index, match.group(1))
        for index, line in enumerate(lines)
        if (match := _LANE_HEADER.fullmatch(line)) is not None
    ]
    if [lane_id for _, lane_id in headers] != lane_ids:
        _add(reasons, "review lane record missing")
    passes = payload.get("passes")
    prefix = f"Final result: `passes` {passes}, `defects` 0."
    finals = [index for index, line in enumerate(lines) if line.startswith("Final result:")]
    final_at = finals[0] if len(finals) == 1 else None
    if final_at is None or not lines[final_at].startswith(prefix):
        _add(reasons, "review final result does not match")
    prompt_at = 0
    if len(headers) == len(lane_ids):
        for position, (start, _lane_id) in enumerate(headers):
            end = headers[position + 1][0] if position + 1 < len(headers) else len(lines)
            if final_at is not None:
                end = min(end, final_at)
            section = lines[start + 1 : end]
            if lane_results[position] == "n_a":
                for reason in _na_lane_reasons(section):
                    _add(reasons, reason)
            else:
                prompt = prompts[prompt_at] if prompt_at < len(prompts) else None
                prompt_at += 1
                for reason in _pass_lane_reasons(section, prompt):
                    _add(reasons, reason)
    expected_prompts = sum(1 for result in lane_results if result != "n_a")
    if prompt_at != expected_prompts or len(prompts) != expected_prompts:
        _add(reasons, "review prompt missing")
    if final_at is None:
        _add(reasons, "review set aside missing")
    else:
        for reason in _set_aside_reasons(lines[final_at + 1 :]):
            _add(reasons, reason)
    return reasons


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
    declaration = validate_declaration(payload, head=head, markdown_only=markdown_only)
    reasons.extend(declaration)
    passes = payload.get("passes")
    if _is_int(passes) and passes >= 1:
        reasons.extend(validate_visible(body, passes))
    else:
        _add(reasons, "review visible text missing")
    # A half run fails on the lane rules. Do not also demand a finished record.
    if not declaration:
        reasons.extend(validate_review_record(body, payload))
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
