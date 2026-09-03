"""Execute pending pr.open, comment.post, review.post, and issue.write activities via gh."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from .runtime import Completed
from .store import Store

Runner = Callable[[list[str]], Completed]

ACTIVITY_MARKER = "<!-- agent-activity:{id} -->"

_URL_RE = re.compile(
    r"https://github\.com/[^/\s]+/[^/\s]+/(?:pulls?|issues)/(\d+)"
)


class _GhError(Exception):
    """Per-row gh failure; never escapes scan_github."""


def _strip(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not k.startswith("_")}


def _mark(
    store: Store,
    row: dict[str, Any],
    *,
    status: str,
    error: str | None = None,
    result: dict[str, Any] | None = None,
) -> None:
    updated = _strip(row)
    updated["execution_status"] = status
    if error is None:
        updated.pop("execution_error", None)
    else:
        updated["execution_error"] = str(error)[:500]
    if result is not None:
        updated["result"] = result
    store.write("activity", "update", updated["id"], updated)


def _repo_ok(repo: Any) -> str | None:
    if not isinstance(repo, str) or repo.count("/") != 1:
        return None
    owner, name = repo.split("/", 1)
    if not owner or not name:
        return None
    return repo


def _as_int(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str) and raw.isdigit():
        return int(raw)
    return None


def _nonempty_str(raw: Any) -> str | None:
    if isinstance(raw, str) and raw != "":
        return raw
    return None


def _optional_str_field(
    payload: dict[str, Any], key: str, *, nonempty: bool = False
) -> str | None:
    """Return str value, None if missing/null; raise _GhError if present but not str."""
    if key not in payload or payload[key] is None:
        return None
    raw = payload[key]
    if not isinstance(raw, str):
        raise _GhError(f"{key} must be a string")
    if nonempty and raw == "":
        raise _GhError(f"{key} must be a non-empty string")
    return raw


def _with_marker(body: str, activity_id: str) -> str:
    marker = ACTIVITY_MARKER.format(id=activity_id)
    if marker in body:
        return body
    if body:
        return f"{body}\n{marker}"
    return marker


def _parse_url_number(stdout: str) -> tuple[str, int]:
    match = _URL_RE.search(stdout)
    if match is None:
        raise _GhError("gh stdout has no github pull/issue URL")
    return match.group(0), int(match.group(1))


def _gh_json_result(argv: list[str], runner: Runner) -> tuple[Any | None, str | None]:
    try:
        completed = runner(argv)
    except OSError as exc:
        return None, f"gh is not available: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "gh failed").strip()
        return None, detail or "gh failed"
    raw = completed.stdout.strip()
    if raw == "":
        return None, "gh returned empty output"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None, "gh returned invalid JSON"
    if not isinstance(data, (dict, list)):
        return None, "gh output is not a JSON object or array"
    return data, None


def _gh_json(argv: list[str], runner: Runner) -> Any:
    data, err = _gh_json_result(argv, runner)
    if err is not None:
        raise _GhError(err)
    return data


def _gh_text(argv: list[str], runner: Runner) -> str:
    try:
        completed = runner(argv)
    except OSError as exc:
        raise _GhError(f"gh is not available: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "gh failed").strip()
        raise _GhError(detail or "gh failed")
    return completed.stdout or ""


def _resolve_actual_base(
    head: str, repo: str, runner: Runner, fallback: str | None
) -> str | None:
    """Best-effort: re-resolve the ACTUAL applied base via a live `gh pr view`
    call, mirroring the resume path's existing baseRefName resolution. Never
    raises — any failure (gh unavailable, non-zero exit, bad JSON, missing
    field) returns `fallback` unchanged so a successful `gh pr create` is never
    turned into an error just because this best-effort re-resolution failed."""
    try:
        completed = runner(
            ["gh", "pr", "view", head, "--repo", repo, "--json", "baseRefName"]
        )
    except OSError:
        return fallback
    if completed.returncode != 0:
        return fallback
    raw = (completed.stdout or "").strip()
    if raw == "":
        return fallback
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return fallback
    if not isinstance(data, dict):
        return fallback
    real_base = data.get("baseRefName")
    return real_base if isinstance(real_base, str) and real_base else fallback


def _gh_not_found(completed: Completed) -> bool:
    """True only when gh failed because this pull request is missing."""
    if completed.returncode == 0:
        return False
    text = f"{completed.stderr or ''}{completed.stdout or ''}".casefold()
    if "no pull request" in text:
        return True
    if "pull request" in text and ("not found" in text or "could not find" in text):
        return True
    return False


def _is_draft(raw: Any) -> bool:
    if raw is True:
        return True
    if isinstance(raw, str) and raw == "true":
        return True
    return False


def _flatten_comment_pages(data: Any) -> list[Any]:
    if not isinstance(data, list):
        raise _GhError("comments api is not an array")
    if data and all(isinstance(el, list) for el in data):
        flat: list[Any] = []
        for page in data:
            if not all(isinstance(item, dict) for item in page):
                raise _GhError("comments api has unexpected shape")
            flat.extend(page)
        return flat
    if all(isinstance(el, dict) for el in data):
        return data
    raise _GhError("comments api has unexpected shape")


def _run_pr_open(store: Store, runner: Runner, row: dict[str, Any]) -> str:
    rid = str(row["id"])
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"pr.open {rid} error"
    repo = _repo_ok(payload.get("repo"))
    title = _nonempty_str(payload.get("title"))
    head = _nonempty_str(payload.get("head"))
    if repo is None or title is None or head is None:
        _mark(store, row, status="error", error="pr.open requires repo, title, head")
        return f"pr.open {rid} error"
    try:
        body_opt = _optional_str_field(payload, "body")
        body = "" if body_opt is None else body_opt
        base = _optional_str_field(payload, "base", nonempty=True)
        base = base.removeprefix("origin/") if base else base
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"pr.open {rid} error"
    try:
        view_argv = [
            "gh",
            "pr",
            "view",
            head,
            "--repo",
            repo,
            "--json",
            "number,url,state,isDraft,baseRefName",
        ]
        try:
            completed = runner(view_argv)
        except OSError as exc:
            raise _GhError(f"gh is not available: {exc}") from exc
        viewed: dict[str, Any] | None
        if completed.returncode != 0:
            # Not-found → create; any other view failure must not create.
            if _gh_not_found(completed):
                viewed = None
            else:
                detail = (completed.stderr or completed.stdout or "gh failed").strip()
                raise _GhError(detail or "gh failed")
        else:
            raw = completed.stdout.strip()
            if raw == "":
                raise _GhError("gh returned empty output")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise _GhError("gh returned invalid JSON") from exc
            if not isinstance(data, dict):
                raise _GhError("gh output is not a JSON object or array")
            viewed = data
        if isinstance(viewed, dict):
            state = str(viewed.get("state") or "").upper()
            number = _as_int(viewed.get("number"))
            url = viewed.get("url")
            if number is None or not isinstance(url, str) or url == "":
                raise _GhError("pr view missing number/url")
            if state != "OPEN":
                raise _GhError("existing pull request is not open")
            if not _is_draft(viewed.get("isDraft")):
                raise _GhError("existing pull request is not a draft")
            real_base = viewed.get("baseRefName")
            resolved_result_base = (
                real_base if isinstance(real_base, str) and real_base else base
            )
            result = {
                "repo": repo,
                "number": number,
                "url": url,
                "draft": True,
                "base": resolved_result_base,
            }
            _mark(store, row, status="done", result=result)
            return f"pr.open {rid} done number={number}"
        create_body = _with_marker(body, rid)
        argv = [
            "gh",
            "pr",
            "create",
            "--draft",
            "--repo",
            repo,
            "--title",
            title,
            "--body",
            create_body,
            "--head",
            head,
        ]
        if base is not None:
            argv.extend(["--base", base])
        stdout = _gh_text(argv, runner)
        url, number = _parse_url_number(stdout)
        resolved_base = _resolve_actual_base(head, repo, runner, base)
        result = {
            "repo": repo,
            "number": number,
            "url": url,
            "draft": True,
            "base": resolved_base,
        }
        _mark(store, row, status="done", result=result)
        return f"pr.open {rid} done number={number}"
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"pr.open {rid} error"


def _run_issue_write(store: Store, runner: Runner, row: dict[str, Any]) -> str:
    rid = str(row["id"])
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"issue.write {rid} error"
    repo = _repo_ok(payload.get("repo"))
    title = _nonempty_str(payload.get("title"))
    if repo is None or title is None:
        _mark(store, row, status="error", error="issue.write requires repo, title")
        return f"issue.write {rid} error"
    try:
        body_opt = _optional_str_field(payload, "body")
        body = "" if body_opt is None else body_opt
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"issue.write {rid} error"
    marker = ACTIVITY_MARKER.format(id=rid)
    try:
        listed = _gh_json(
            [
                "gh",
                "issue",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--limit",
                "100",
                "--json",
                "number,title,url,body",
            ],
            runner,
        )
        if not isinstance(listed, list):
            raise _GhError("issue list is not an array")
        for issue in listed:
            if not isinstance(issue, dict):
                continue
            issue_body = issue.get("body")
            if not isinstance(issue_body, str) or marker not in issue_body:
                continue
            number = _as_int(issue.get("number"))
            url = issue.get("url")
            if number is None or not isinstance(url, str) or url == "":
                raise _GhError("issue list entry missing number/url")
            result = {"repo": repo, "number": number, "url": url}
            _mark(store, row, status="done", result=result)
            return f"issue.write {rid} done number={number}"
        if len(listed) == 100:
            raise _GhError("issue list truncated")
        create_body = _with_marker(body, rid)
        stdout = _gh_text(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--body",
                create_body,
            ],
            runner,
        )
        url, number = _parse_url_number(stdout)
        result = {"repo": repo, "number": number, "url": url}
        _mark(store, row, status="done", result=result)
        return f"issue.write {rid} done number={number}"
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"issue.write {rid} error"


def _login(runner: Runner) -> str | None:
    """The account gh is authenticated as, or None when it cannot be determined."""
    data, _err = _gh_json_result(["gh", "api", "user"], runner)
    if isinstance(data, dict):
        login = data.get("login")
        if isinstance(login, str) and login != "":
            return login
    return None


def _run_review_post(store: Store, runner: Runner, row: dict[str, Any]) -> str:
    """Submit a pull-request review of type COMMENT carrying the findings.

    A rejected gate's findings are a review, not a remark: posting them as a review
    puts them where an author looks and makes the work visible to anything counting
    review artefacts. COMMENT rather than REQUEST_CHANGES, so a bot cannot hold a
    merge closed through branch protection.
    """
    rid = str(row["id"])
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"review.post {rid} error"
    repo = _repo_ok(payload.get("repo"))
    number = _as_int(payload.get("number"))
    body = _nonempty_str(payload.get("body"))
    if repo is None or number is None or number <= 0 or body is None:
        _mark(store, row, status="error", error="review.post requires repo, number, body")
        return f"review.post {rid} error"
    # COMMENT reports; APPROVE is a merge authorisation and is only inserted once the
    # gates on this head are approved. REQUEST_CHANGES is refused here rather than left
    # to convention: it would let this account hold a merge closed through branch
    # protection, which is a different tool from the one this is.
    event = payload.get("event", "COMMENT")
    if event not in ("COMMENT", "APPROVE"):
        _mark(store, row, status="error", error="review.post event must be COMMENT or APPROVE")
        return f"review.post {rid} error"
    marker = ACTIVITY_MARKER.format(id=rid)
    owner, name = repo.split("/", 1)
    try:
        reviews_raw = _gh_json(
            ["gh", "api", "--paginate", "--slurp", f"repos/{owner}/{name}/pulls/{number}/reviews"],
            runner,
        )
        me: str | None = None
        asked_who_we_are = False
        for review in _flatten_comment_pages(reviews_raw):
            if not isinstance(review, dict):
                continue
            rbody = review.get("body")
            if not isinstance(rbody, str) or marker not in rbody:
                continue
            # Only now: on the ordinary path no review carries this marker, and asking
            # gh who we are would be a round trip to answer a question nobody asked.
            if not asked_who_we_are:
                me = _login(runner)
                asked_who_we_are = True
            if me is None:
                # A candidate exists but the identity is unknown. Retrying later is
                # right; posting a second review because a lookup flaked is not.
                raise _GhError("cannot determine the authenticated account")
            # The marker is visible to anyone reading the pull request. Without the
            # author check a copied marker would suppress this review entirely.
            author = review.get("user")
            login = author.get("login") if isinstance(author, dict) else None
            # Case-insensitive, as DESIGN.md requires of a GitHub login and as
            # watch.py compares one: the same account can be spelled either way.
            if not isinstance(login, str) or login.lower() != me.lower():
                continue
            url = review.get("html_url") or review.get("url")
            if not isinstance(url, str) or url == "":
                raise _GhError("review missing url")
            result: dict[str, Any] = {"repo": repo, "number": number, "url": url}
            rev_id = _as_int(review.get("id"))
            if rev_id is not None and rev_id > 0:
                result["id"] = rev_id
            _mark(store, row, status="done", result=result)
            return f"review.post {rid} done"
        created = _gh_json(
            [
                "gh",
                "api",
                "-X",
                "POST",
                f"repos/{owner}/{name}/pulls/{number}/reviews",
                "-f",
                f"body={_with_marker(body, rid)}",
                "-f",
                f"event={event}",
            ],
            runner,
        )
        if not isinstance(created, dict):
            raise _GhError("review response is not an object")
        url = created.get("html_url") or created.get("url")
        if not isinstance(url, str) or url == "":
            raise _GhError("review missing url")
        result = {"repo": repo, "number": number, "url": url}
        new_id = _as_int(created.get("id"))
        if new_id is not None and new_id > 0:
            result["id"] = new_id
        _mark(store, row, status="done", result=result)
        return f"review.post {rid} done"
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"review.post {rid} error"


def _run_comment_post(store: Store, runner: Runner, row: dict[str, Any]) -> str:
    rid = str(row["id"])
    payload = row.get("payload")
    if not isinstance(payload, dict):
        _mark(store, row, status="error", error="payload must be an object")
        return f"comment.post {rid} error"
    repo = _repo_ok(payload.get("repo"))
    number = _as_int(payload.get("number"))
    body = _nonempty_str(payload.get("body"))
    if repo is None or number is None or number <= 0 or body is None:
        _mark(
            store,
            row,
            status="error",
            error="comment.post requires repo, number, body",
        )
        return f"comment.post {rid} error"
    target_raw = payload.get("target")
    target = "issue"
    if target_raw is not None:
        if target_raw not in ("pr", "issue"):
            _mark(store, row, status="error", error="comment.post target must be pr|issue")
            return f"comment.post {rid} error"
        target = str(target_raw)
    marker = ACTIVITY_MARKER.format(id=rid)
    owner, name = repo.split("/", 1)
    try:
        comments_raw = _gh_json(
            [
                "gh",
                "api",
                "--paginate",
                "--slurp",
                f"repos/{owner}/{name}/issues/{number}/comments",
            ],
            runner,
        )
        comments = _flatten_comment_pages(comments_raw)
        for comment in comments:
            if not isinstance(comment, dict):
                continue
            cbody = comment.get("body")
            if not isinstance(cbody, str) or marker not in cbody:
                continue
            cid = comment.get("id")
            url = comment.get("html_url") or comment.get("url")
            if not isinstance(url, str) or url == "":
                raise _GhError("comment missing url")
            result: dict[str, Any] = {"repo": repo, "number": number, "url": url}
            if cid is not None:
                result["id"] = cid
            _mark(store, row, status="done", result=result)
            return f"comment.post {rid} done"
        post_body = _with_marker(body, rid)
        if target == "pr":
            argv = [
                "gh",
                "pr",
                "comment",
                str(number),
                "--repo",
                repo,
                "--body",
                post_body,
            ]
        else:
            argv = [
                "gh",
                "issue",
                "comment",
                str(number),
                "--repo",
                repo,
                "--body",
                post_body,
            ]
        stdout = _gh_text(argv, runner)
        stripped = stdout.strip()
        if stripped.startswith("{"):
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise _GhError("gh returned invalid JSON") from exc
            if not isinstance(data, dict):
                raise _GhError("gh comment JSON is not an object")
            url = data.get("html_url") or data.get("url")
            if not isinstance(url, str) or url == "":
                raise _GhError("comment response missing url")
            result = {"repo": repo, "number": number, "url": url}
            if data.get("id") is not None:
                result["id"] = data["id"]
            _mark(store, row, status="done", result=result)
            return f"comment.post {rid} done"
        if stripped.startswith("https://github.com/"):
            result = {"repo": repo, "number": number, "url": stripped}
            _mark(store, row, status="done", result=result)
            return f"comment.post {rid} done"
        raise _GhError("gh stdout has no github pull/issue URL")
    except _GhError as exc:
        _mark(store, row, status="error", error=str(exc))
        return f"comment.post {rid} error"


def scan_github(store: Store, runner: Runner) -> list[str]:
    """Execute pending pr.open, comment.post, review.post, issue.write owned by this device.

    Other pending types (subscription.set, query.request, …) are skipped.
    Returns human-readable status lines, one per handled row.
    """
    lines: list[str] = []
    for row in store.pending_work():
        typ = row.get("type")
        try:
            if typ == "pr.open":
                lines.append(_run_pr_open(store, runner, row))
            elif typ == "issue.write":
                lines.append(_run_issue_write(store, runner, row))
            elif typ == "comment.post":
                lines.append(_run_comment_post(store, runner, row))
            elif typ == "review.post":
                lines.append(_run_review_post(store, runner, row))
        except Exception as exc:  # noqa: BLE001 — per-row isolation
            rid = str(row.get("id") or "?")
            _mark(store, row, status="error", error=str(exc))
            label = typ if isinstance(typ, str) else "activity"
            lines.append(f"{label} {rid} error")
    return lines
