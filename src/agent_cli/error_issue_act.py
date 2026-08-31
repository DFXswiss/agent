"""Apply pending error.issue activities: file or update a GitHub issue for a
normalized error template, grouped by template_fingerprint rather than the
finer-grained per-variant fingerprint — or, under dry_run, just log the intended
action instead of calling gh for real."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .errors import known_chain_in
from .runtime import Completed
from .store import Store, StoreError, utcnow

Runner = Callable[[list[str]], Completed]

ISSUE_LABEL = "error-log-agent"
_MARKER_PREFIX = "<!-- error-log-template:"
_MARKER_SUFFIX = " -->"
_VARIANTS_START = "<!-- variants:start -->"
_VARIANTS_END = "<!-- variants:end -->"


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


def _nonempty_str(raw: Any) -> str | None:
    if isinstance(raw, str) and raw != "":
        return raw
    return None


def _error_seen(store: Store, session_id: str, error_id: str) -> dict[str, Any]:
    row = store.row("activity", error_id)
    if (
        row is None
        or row.get("_origin_device_id") != store.device_id()
        or row.get("session_id") != session_id
        or row.get("type") != "error.seen"
    ):
        raise StoreError("error.seen not found")
    return row


def marker_for(template_fingerprint: str) -> str:
    return f"{_MARKER_PREFIX}{template_fingerprint}{_MARKER_SUFFIX}"


def extract_variant(excerpt: str) -> str:
    """Best-effort concrete detail for the variant table: the known chain name
    present in the excerpt, or "generic" if none. Most error lines don't name a
    chain — those never fragment, so there is only ever one variant."""
    return known_chain_in(excerpt) or "generic"


def render_variants_section(variants: dict[str, dict[str, str]]) -> str:
    lines = [_VARIANTS_START, "", "| variant | first seen | last seen |", "|---|---|---|"]
    for name in sorted(variants):
        entry = variants[name]
        lines.append(f"| {name} | {entry.get('first_seen', '')} | {entry.get('last_seen', '')} |")
    lines.append("")
    lines.append(_VARIANTS_END)
    return "\n".join(lines)


def parse_variants_section(body: str) -> dict[str, dict[str, str]]:
    """Parse the existing delimited variants table back out of an issue body."""
    start = body.find(_VARIANTS_START)
    end = body.find(_VARIANTS_END)
    variants: dict[str, dict[str, str]] = {}
    if start == -1 or end == -1 or end < start:
        return variants
    for raw_line in body[start:end].splitlines():
        line = raw_line.strip()
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| variant"):
            continue
        parts = [p.strip() for p in line.strip("|").split("|")]
        if len(parts) != 3 or parts[0] == "":
            continue
        name, first_seen, last_seen = parts
        variants[name] = {"first_seen": first_seen, "last_seen": last_seen}
    return variants


def splice_variants(body: str, variants: dict[str, dict[str, str]]) -> str:
    """Replace only the delimited variants section; never touch the rest of the
    body — that is human territory."""
    start = body.find(_VARIANTS_START)
    end = body.find(_VARIANTS_END)
    section = render_variants_section(variants)
    if start == -1 or end == -1 or end < start:
        sep = "" if body == "" else "\n\n"
        return f"{body}{sep}{section}\n"
    return body[:start] + section + body[end + len(_VARIANTS_END) :]


def _pending_issue(store: Store, row: dict[str, Any]) -> tuple[str, str, dict[str, Any]]:
    payload = row.get("payload")
    if not isinstance(payload, dict):
        raise StoreError("payload must be an object")
    error_id = _nonempty_str(payload.get("error_id"))
    if error_id is None:
        raise StoreError("error_id is required")
    session_id = _nonempty_str(row.get("session_id"))
    if session_id is None:
        raise StoreError("session_id is required")
    seen = _error_seen(store, session_id, error_id)
    seen_payload = seen.get("payload")
    if not isinstance(seen_payload, dict):
        raise StoreError("error.seen payload is invalid")
    template_fp = _nonempty_str(seen_payload.get("template_fingerprint"))
    if template_fp is None:
        raise StoreError("template_fingerprint is required")
    return error_id, template_fp, seen_payload


def find_issue_number(runner: Runner, issue_repo: str, template_fingerprint: str) -> int | None:
    """Search issue_repo for an open, labeled issue carrying this template's
    hidden marker. None if no such issue exists yet."""
    completed = runner(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            issue_repo,
            "--label",
            ISSUE_LABEL,
            "--state",
            "open",
            "--search",
            marker_for(template_fingerprint),
            "--json",
            "number",
        ]
    )
    if completed.returncode != 0:
        raise StoreError((completed.stderr or completed.stdout or "gh issue list failed").strip())
    try:
        found = json.loads(completed.stdout)
    except ValueError as exc:
        raise StoreError("gh issue list returned invalid JSON") from exc
    if not isinstance(found, list) or not found:
        return None
    first = found[0]
    number = first.get("number") if isinstance(first, dict) else None
    if isinstance(number, bool) or not isinstance(number, int):
        raise StoreError("gh issue list returned a non-integer number")
    return number


def _issue_body(runner: Runner, issue_repo: str, number: int) -> str:
    completed = runner(["gh", "issue", "view", str(number), "--repo", issue_repo, "--json", "body"])
    if completed.returncode != 0:
        raise StoreError((completed.stderr or completed.stdout or "gh issue view failed").strip())
    try:
        data = json.loads(completed.stdout)
    except ValueError as exc:
        raise StoreError("gh issue view returned invalid JSON") from exc
    body = data.get("body") if isinstance(data, dict) else None
    return body if isinstance(body, str) else ""


def _create_issue(
    runner: Runner,
    *,
    issue_repo: str,
    title: str,
    template_fingerprint: str,
    excerpt: str,
    variant: str,
    now: str,
) -> str:
    body = (
        f"{marker_for(template_fingerprint)}\n\n"
        "Automated error-log finding.\n\n"
        f"```\n{excerpt}\n```\n\n"
        + render_variants_section({variant: {"first_seen": now, "last_seen": now}})
        + "\n"
    )
    completed = runner(
        [
            "gh",
            "issue",
            "create",
            "--repo",
            issue_repo,
            "--label",
            ISSUE_LABEL,
            "--title",
            title,
            "--body",
            body,
        ]
    )
    if completed.returncode != 0:
        raise StoreError((completed.stderr or completed.stdout or "gh issue create failed").strip())
    return completed.stdout.strip()


def _update_issue(
    runner: Runner, *, issue_repo: str, number: int, variant: str, now: str
) -> bool:
    """Splice the variant into the issue's tracked table; comment only if it is
    new. Returns whether this was a new variant."""
    body = _issue_body(runner, issue_repo, number)
    variants = parse_variants_section(body)
    is_new = variant not in variants
    if is_new:
        variants[variant] = {"first_seen": now, "last_seen": now}
    else:
        variants[variant]["last_seen"] = now
    edit = runner(
        ["gh", "issue", "edit", str(number), "--repo", issue_repo, "--body", splice_variants(body, variants)]
    )
    if edit.returncode != 0:
        raise StoreError((edit.stderr or edit.stdout or "gh issue edit failed").strip())
    if is_new:
        comment = runner(
            [
                "gh",
                "issue",
                "comment",
                str(number),
                "--repo",
                issue_repo,
                "--body",
                f"Also seen on: {variant} ({now}).",
            ]
        )
        if comment.returncode != 0:
            raise StoreError((comment.stderr or comment.stdout or "gh issue comment failed").strip())
    return is_new


def scan_error_issue(
    store: Store, runner: Runner, *, issue_repo: str, dry_run: bool
) -> list[str]:
    with store.exclusive("error-issue-act:" + store.device_id()):
        return _scan_error_issue(store, runner, issue_repo=issue_repo, dry_run=dry_run)


def _scan_error_issue(
    store: Store, runner: Runner, *, issue_repo: str, dry_run: bool
) -> list[str]:
    rows = [
        row
        for row in store.rows("activity")
        if row.get("_origin_device_id") == store.device_id()
        and row.get("type") == "error.issue"
        and row.get("execution_status") == "pending"
    ]
    rows.sort(key=lambda row: str(row.get("id") or ""))
    lines: list[str] = []
    for row in rows:
        rid = str(row.get("id") or "?")
        try:
            _error_id, template_fp, seen_payload = _pending_issue(store, row)
        except StoreError as exc:
            _mark(store, row, status="error", error=str(exc))
            lines.append(f"error.issue {rid} error")
            continue

        excerpt = seen_payload.get("excerpt")
        excerpt = excerpt if isinstance(excerpt, str) else ""
        variant = extract_variant(excerpt)
        now = utcnow()

        if dry_run:
            result = {
                "mode": "dry-run",
                "issue_repo": issue_repo,
                "template_fingerprint": template_fp,
                "variant": variant,
                "excerpt": excerpt[:300],
            }
            _mark(store, row, status="done", result=result)
            lines.append(f"error.issue {rid} dry-run variant={variant}")
            continue

        try:
            number = find_issue_number(runner, issue_repo, template_fp)
        except StoreError as exc:
            _mark(store, row, status="error", error=str(exc))
            lines.append(f"error.issue {rid} error")
            continue

        if number is None:
            service = seen_payload.get("service")
            service = service if isinstance(service, str) and service else "unknown"
            cls = seen_payload.get("class")
            cls = cls if isinstance(cls, str) and cls else "error"
            try:
                url = _create_issue(
                    runner,
                    issue_repo=issue_repo,
                    title=f"{service}: {cls}",
                    template_fingerprint=template_fp,
                    excerpt=excerpt[:1000],
                    variant=variant,
                    now=now,
                )
            except StoreError as exc:
                _mark(store, row, status="error", error=str(exc))
                lines.append(f"error.issue {rid} error")
                continue
            result = {
                "issue_repo": issue_repo,
                "url": url,
                "variant": variant,
                "created": True,
            }
            _mark(store, row, status="done", result=result)
            lines.append(f"error.issue {rid} created variant={variant}")
            continue

        try:
            is_new_variant = _update_issue(
                runner, issue_repo=issue_repo, number=number, variant=variant, now=now
            )
        except StoreError as exc:
            _mark(store, row, status="error", error=str(exc))
            lines.append(f"error.issue {rid} error")
            continue
        result = {
            "issue_repo": issue_repo,
            "number": number,
            "variant": variant,
            "created": False,
            "new_variant": is_new_variant,
        }
        _mark(store, row, status="done", result=result)
        lines.append(
            f"error.issue {rid} updated number={number} variant={variant} new_variant={is_new_variant}"
        )
    return lines
