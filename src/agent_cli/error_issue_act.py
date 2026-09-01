"""Apply pending error.issue activities: file or update a GitHub issue for a
normalized error template, grouped by template_fingerprint rather than the
finer-grained per-variant fingerprint — or, under dry_run, just log the intended
action instead of calling gh for real.

Two throttles sit in front of the per-template logic:

- Burst detection: if one run resolves more templates that were never filed
  before than storm_threshold, that is treated as one anomaly (a likely shared
  root cause) rather than N unrelated problems — those fold into a single,
  reused "burst" issue instead of N individual ones. Templates that already have
  history keep updating their own issue: a backlog draining after downtime is a
  volume spike, not a burst of new problems.
- Cooldown: a template that was already touched (created, updated, or folded
  into a burst) within cooldown_minutes is skipped entirely — checked against
  local history, not a live gh call, so a fast-recurring error does not cost a
  round trip or an issue edit every time it repeats. A dry run is a preview and
  never opens that window.

All pending rows of one template are handled together, so two variants seen in
the same run land in one issue instead of the first becoming a cooldown touch
that drops the second.

Both are plain comparisons against local state; neither involves model
judgment."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .errors import known_asset_in, known_chain_in
from .runtime import Completed
from .store import Store, StoreError, utcnow

Runner = Callable[[list[str]], Completed]

ISSUE_LABEL = "error-log-agent"
_MARKER_PREFIX = "<!-- error-log-template:"
_MARKER_SUFFIX = " -->"
_VARIANTS_START = "<!-- variants:start -->"
_VARIANTS_END = "<!-- variants:end -->"
STORM_MARKER = "<!-- error-log-storm -->"

DEFAULT_COOLDOWN_MINUTES = 60
DEFAULT_STORM_THRESHOLD = 8
# GitHub rejects an issue body over 65536 characters. Refuse a little earlier and
# loudly, so a long-lived burst issue reports the ceiling instead of every later
# edit failing at the API with a generic error.
MAX_ISSUE_BODY = 60000
# One page of candidates, as in github_act. A full page is treated as truncated
# rather than as "no match".
_ISSUE_LIST_LIMIT = 100
# Keep the variants table bounded so normal growth can never walk an issue into
# MAX_ISSUE_BODY and wedge every later update on it.
MAX_TRACKED_VARIANTS = 200
# Result modes that never touched GitHub, so they must not start a cooldown
# window: a dry run is a preview, not a touch.
_DRY_RUN_MODES = frozenset({"dry-run", "storm-dry-run"})


def _inert_block(text: str) -> str:
    """Log lines are untrusted data (DESIGN.md §19.2). A line carrying this
    module's own marker would otherwise move the boundary of the machine-owned
    section: a later splice would find the excerpt's marker first and rewrite
    everything between it and the real one, destroying issue content. Breaking
    the comment opener keeps the line readable inside its code fence while
    making it inert."""
    return text.replace("<!--", "<!- -")


def _inert_cell(text: str) -> str:
    """Same, for text that lands in a table cell: a pipe or a newline there
    would break the row apart and the name would not survive a round trip."""
    return _inert_block(text).replace("|", "/").replace("\n", " ").replace("\r", " ")


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
    """Best-effort concrete detail for the variant table: "Chain/Asset" if both
    are present in the excerpt, whichever one is present if only one is, or
    "generic" if neither. Most error lines don't name either — those never
    fragment, so there is only ever one variant."""
    chain = known_chain_in(excerpt)
    asset = known_asset_in(excerpt)
    if chain is not None and asset is not None:
        return f"{chain}/{asset}"
    return chain or asset or "generic"


def render_variants_section(variants: dict[str, dict[str, str]]) -> str:
    """Render the machine-owned table, keeping at most MAX_TRACKED_VARIANTS rows.

    The cap is what stops a long-lived issue from growing into MAX_ISSUE_BODY,
    where every later update would fail and the template would be stuck erroring
    for good. The most recently seen variants are the ones worth keeping; the
    dropped count stays visible in the section so the table never silently
    understates what was seen."""
    kept = variants
    dropped = 0
    if len(variants) > MAX_TRACKED_VARIANTS:
        by_recency = sorted(
            variants.items(), key=lambda item: (item[1].get("last_seen", ""), item[0]), reverse=True
        )
        kept = dict(by_recency[:MAX_TRACKED_VARIANTS])
        dropped = len(variants) - MAX_TRACKED_VARIANTS
    lines = [_VARIANTS_START, "", "| variant | first seen | last seen |", "|---|---|---|"]
    for name in sorted(kept):
        entry = kept[name]
        lines.append(f"| {name} | {entry.get('first_seen', '')} | {entry.get('last_seen', '')} |")
    lines.append("")
    if dropped:
        lines.append(f"_{dropped} older variants dropped to stay within the issue body limit._")
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


def _ensure_issue_body(body: str) -> str:
    """One ceiling for every body this module sends to gh, whether it is spliced
    into an existing issue or built for a new one."""
    if len(body) > MAX_ISSUE_BODY:
        raise StoreError("issue body would exceed the GitHub body limit")
    return body


def splice_variants(body: str, variants: dict[str, dict[str, str]]) -> str:
    """Replace only the delimited variants section; never touch the rest of the
    body — that is human territory. A body carrying exactly one of the two
    markers, or them in the wrong order, is damaged (a hand edit truncated the
    section): appending a second section there would silently strand the
    variants already recorded above, so fail loud instead."""
    start = body.find(_VARIANTS_START)
    end = body.find(_VARIANTS_END)
    section = render_variants_section(variants)
    if start == -1 and end == -1:
        sep = "" if body == "" else "\n\n"
        spliced = f"{body}{sep}{section}\n"
    elif start == -1 or end == -1 or end < start:
        raise StoreError("issue body has a damaged variants section")
    else:
        spliced = body[:start] + section + body[end + len(_VARIANTS_END) :]
    return _ensure_issue_body(spliced)


def _parse_iso(ts: str) -> datetime:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def touch_history(store: Store) -> dict[str, datetime]:
    """Most recent real touch per template, read once from local activity
    history. A touch is a completed error.issue row that actually created,
    updated or folded the template's issue — not one merely skipped by an
    earlier cooldown check (which must not renew its own window) and not a dry
    run (a preview must not suppress the real run that follows it).

    Read as a snapshot before a scan mutates anything: rows written earlier in
    the same scan are not touches for the rows behind them, otherwise the second
    variant of one template would be skipped against the first."""
    history: dict[str, datetime] = {}
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin or row.get("type") != "error.issue":
            continue
        if row.get("execution_status") != "done":
            continue
        result = row.get("result")
        if not isinstance(result, dict) or result.get("skipped"):
            continue
        if result.get("mode") in _DRY_RUN_MODES:
            continue
        template_fingerprint = result.get("template_fingerprint")
        if not isinstance(template_fingerprint, str):
            continue
        touched_at = result.get("at")
        if not isinstance(touched_at, str):
            continue
        try:
            touched_dt = _parse_iso(touched_at)
        except ValueError:
            continue
        previous = history.get(template_fingerprint)
        if previous is None or touched_dt > previous:
            history[template_fingerprint] = touched_dt
    return history


def _within_cooldown(
    history: dict[str, datetime], template_fingerprint: str, now: str, cooldown_minutes: int
) -> bool:
    """Whether this template's issue was touched within cooldown_minutes, against
    a history snapshot. No gh call, so a fast-recurring error costs no round trip
    and no issue edit on every repeat."""
    if cooldown_minutes <= 0:
        return False
    touched_dt = history.get(template_fingerprint)
    if touched_dt is None:
        return False
    try:
        now_dt = _parse_iso(now)
    except ValueError:
        return False
    elapsed = now_dt - touched_dt
    # A touch dated in the future (clock skew, a backward clock, a damaged row)
    # would otherwise look like "no time has passed" forever and silently skip
    # this template on every future scan. Treat it as not in cooldown.
    if elapsed < timedelta(0):
        return False
    return elapsed < timedelta(minutes=cooldown_minutes)



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


def _gh(runner: Runner, argv: list[str], fallback: str) -> str:
    """Run one gh command and return its stdout. A missing or broken binary
    raises the same StoreError as a non-zero exit, so it stays a per-row failure
    instead of aborting the whole scan (same contract as error_fix_act and
    github_act)."""
    try:
        completed = runner(argv)
    except OSError as exc:
        raise StoreError(f"{fallback}: {exc}") from exc
    if completed.returncode != 0:
        raise StoreError((completed.stderr or completed.stdout or fallback).strip())
    return completed.stdout


def _gh_json(runner: Runner, argv: list[str], fallback: str) -> Any:
    stdout = _gh(runner, argv, fallback)
    try:
        return json.loads(stdout)
    except ValueError as exc:
        raise StoreError(f"{fallback}: invalid JSON") from exc


def find_issue_number(runner: Runner, issue_repo: str, marker: str) -> int | None:
    """The open, labeled issue whose body carries this marker, or None if none
    exists yet.

    The label bounds the candidates and the marker is matched against the body
    here rather than handed to `--search`: GitHub's search is full-text and
    tokenizing, so it can both miss the marker and return an issue that does not
    carry it, and the returned candidate's body would never be checked. This
    mirrors the find-or-create in github_act."""
    listed = _gh_json(
        runner,
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
            "--limit",
            str(_ISSUE_LIST_LIMIT),
            "--json",
            "number,body",
        ],
        "gh issue list failed",
    )
    if not isinstance(listed, list):
        raise StoreError("gh issue list is not an array")
    for issue in listed:
        if not isinstance(issue, dict):
            continue
        body = issue.get("body")
        if not isinstance(body, str) or marker not in body:
            continue
        number = issue.get("number")
        if isinstance(number, bool) or not isinstance(number, int):
            raise StoreError("gh issue list returned a non-integer number")
        return number
    if len(listed) == _ISSUE_LIST_LIMIT:
        # A full page means the marker may sit on an issue we never saw. Failing
        # here beats reporting "no issue yet" and filing a duplicate.
        raise StoreError("gh issue list truncated")
    return None


def _issue_body(runner: Runner, issue_repo: str, number: int) -> str:
    data = _gh_json(
        runner,
        ["gh", "issue", "view", str(number), "--repo", issue_repo, "--json", "body"],
        "gh issue view failed",
    )
    if not isinstance(data, dict):
        raise StoreError("gh issue view did not return an object")
    body = data.get("body")
    # An issue that never had a description reports null; that is genuinely empty.
    return body if isinstance(body, str) else ""


def _create_issue(
    runner: Runner,
    *,
    issue_repo: str,
    title: str,
    template_fingerprint: str,
    excerpt: str,
    variants: list[str],
    now: str,
) -> str:
    body = _ensure_issue_body(
        f"{marker_for(template_fingerprint)}\n\n"
        "Automated error-log finding.\n\n"
        f"```\n{_inert_block(excerpt)}\n```\n\n"
        + render_variants_section({v: {"first_seen": now, "last_seen": now} for v in variants})
        + "\n"
    )
    return _gh(
        runner,
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
        ],
        "gh issue create failed",
    ).strip()


def _update_issue(
    runner: Runner, *, issue_repo: str, number: int, variants: list[str], now: str
) -> tuple[list[str], str | None]:
    """Splice these variants into the issue's tracked table; comment once for the
    ones that are genuinely new. Returns the new variants and, if the comment
    failed, its error.

    The edit runs before the comment so the durable record (the variant table)
    is written first and a retry can never double-file a variant. That makes the
    comment a best-effort notification: failing the whole row on a comment error
    would strand a row whose table entry already landed, and no retry could ever
    send that comment again (the variant is no longer new). So a comment failure
    is reported on the row instead of discarding the successful edit."""
    body = _issue_body(runner, issue_repo, number)
    tracked = parse_variants_section(body)
    new_variants = [v for v in variants if v not in tracked]
    for variant in variants:
        if variant in tracked:
            tracked[variant]["last_seen"] = now
        else:
            tracked[variant] = {"first_seen": now, "last_seen": now}
    _gh(
        runner,
        [
            "gh",
            "issue",
            "edit",
            str(number),
            "--repo",
            issue_repo,
            "--body",
            splice_variants(body, tracked),
        ],
        "gh issue edit failed",
    )
    if not new_variants:
        return new_variants, None
    try:
        _gh(
            runner,
            [
                "gh",
                "issue",
                "comment",
                str(number),
                "--repo",
                issue_repo,
                "--body",
                f"Also seen on: {', '.join(new_variants)} ({now}).",
            ],
            "gh issue comment failed",
        )
    except StoreError as exc:
        return new_variants, str(exc)
    return new_variants, None


def _storm_label(template_fingerprint: str, seen_payload: dict[str, Any]) -> str:
    service = seen_payload.get("service")
    service = service if isinstance(service, str) and service else "unknown"
    cls = seen_payload.get("class")
    cls = cls if isinstance(cls, str) and cls else "error"
    parts = template_fingerprint.split("|")
    short = parts[2][:8] if len(parts) >= 3 and parts[2] else template_fingerprint[:8]
    return _inert_cell(f"{service}: {cls} ({short})")


def _create_storm_issue(
    runner: Runner, *, issue_repo: str, templates: dict[str, dict[str, str]], now: str
) -> str:
    body = _ensure_issue_body(
        f"{STORM_MARKER}\n\n"
        "Automated burst finding: this run saw more distinct new error templates "
        "than usual in one pass. That is more likely one shared root cause than "
        "many unrelated bugs — investigate the cause, not each row below "
        "individually.\n\n"
        + render_variants_section(templates)
        + "\n"
    )
    return _gh(
        runner,
        [
            "gh",
            "issue",
            "create",
            "--repo",
            issue_repo,
            "--label",
            ISSUE_LABEL,
            "--title",
            f"Error-log burst: {len(templates)} new templates in one run",
            "--body",
            body,
        ],
        "gh issue create failed",
    ).strip()


def _update_storm_issue(
    runner: Runner, *, issue_repo: str, number: int, templates: dict[str, dict[str, str]]
) -> None:
    body = _issue_body(runner, issue_repo, number)
    existing = parse_variants_section(body)
    for name, entry in templates.items():
        if name in existing:
            existing[name]["last_seen"] = entry["last_seen"]
        else:
            existing[name] = dict(entry)
    _gh(
        runner,
        [
            "gh",
            "issue",
            "edit",
            str(number),
            "--repo",
            issue_repo,
            "--body",
            splice_variants(body, existing),
        ],
        "gh issue edit failed",
    )


def _process_storm(
    store: Store,
    runner: Runner,
    *,
    issue_repo: str,
    dry_run: bool,
    resolved: list[tuple[dict[str, Any], str, str, dict[str, Any]]],
    now: str,
) -> list[str]:
    templates: dict[str, dict[str, str]] = {}
    for _row, _error_id, template_fp, seen_payload in resolved:
        label = _storm_label(template_fp, seen_payload)
        if label in templates:
            templates[label]["last_seen"] = now
        else:
            templates[label] = {"first_seen": now, "last_seen": now}

    lines: list[str] = []

    if dry_run:
        for row, _error_id, template_fp, _seen_payload in resolved:
            rid = str(row.get("id") or "?")
            result = {
                "mode": "storm-dry-run",
                "issue_repo": issue_repo,
                "template_fingerprint": template_fp,
                "at": now,
                "storm_size": len(templates),
            }
            _mark(store, row, status="done", result=result)
            lines.append(f"error.issue {rid} storm-dry-run size={len(templates)}")
        return lines

    try:
        number = find_issue_number(runner, issue_repo, STORM_MARKER)
        if number is None:
            url = _create_storm_issue(runner, issue_repo=issue_repo, templates=templates, now=now)
            extra: dict[str, Any] = {"url": url, "created": True}
        else:
            _update_storm_issue(runner, issue_repo=issue_repo, number=number, templates=templates)
            extra = {"number": number, "created": False}
    except StoreError as exc:
        for row, _error_id, _template_fp, _seen_payload in resolved:
            rid = str(row.get("id") or "?")
            _mark(store, row, status="error", error=str(exc))
            lines.append(f"error.issue {rid} error")
        return lines

    for row, _error_id, template_fp, _seen_payload in resolved:
        rid = str(row.get("id") or "?")
        result = {
            "mode": "storm",
            "issue_repo": issue_repo,
            "template_fingerprint": template_fp,
            "at": now,
            **extra,
        }
        _mark(store, row, status="done", result=result)
        lines.append(f"error.issue {rid} storm size={len(templates)}")
    return lines


def scan_error_issue(
    store: Store,
    runner: Runner,
    *,
    issue_repo: str,
    dry_run: bool,
    cooldown_minutes: int = DEFAULT_COOLDOWN_MINUTES,
    storm_threshold: int = DEFAULT_STORM_THRESHOLD,
) -> list[str]:
    with store.exclusive("error-issue-act:" + store.device_id()):
        return _scan_error_issue(
            store,
            runner,
            issue_repo=issue_repo,
            dry_run=dry_run,
            cooldown_minutes=cooldown_minutes,
            storm_threshold=storm_threshold,
        )


def _scan_error_issue(
    store: Store,
    runner: Runner,
    *,
    issue_repo: str,
    dry_run: bool,
    cooldown_minutes: int,
    storm_threshold: int,
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
    resolved: list[tuple[dict[str, Any], str, str, dict[str, Any]]] = []
    for row in rows:
        rid = str(row.get("id") or "?")
        try:
            error_id, template_fp, seen_payload = _pending_issue(store, row)
        except StoreError as exc:
            _mark(store, row, status="error", error=str(exc))
            lines.append(f"error.issue {rid} error")
            continue
        resolved.append((row, error_id, template_fp, seen_payload))

    if not resolved:
        return lines

    now = utcnow()
    # One snapshot of local history for the whole scan. Taken before any row is
    # marked, so rows written by this scan cannot start a cooldown window
    # against the rows behind them.
    history = touch_history(store)

    groups: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for row, _error_id, template_fp, seen_payload in resolved:
        groups.setdefault(template_fp, []).append((row, seen_payload))

    # Burst detection counts only templates never filed before. A backlog of
    # already-tracked templates draining after downtime is a volume spike, not a
    # burst of new problems, and must keep updating its own issues normally.
    new_templates = [fp for fp in groups if fp not in history]
    if len(new_templates) > storm_threshold:
        storm_rows = [
            (row, "", template_fp, seen_payload)
            for template_fp in new_templates
            for row, seen_payload in groups[template_fp]
        ]
        lines.extend(
            _process_storm(
                store, runner, issue_repo=issue_repo, dry_run=dry_run, resolved=storm_rows, now=now
            )
        )
        for template_fp in new_templates:
            del groups[template_fp]

    open_burst = _OpenBurst(runner, issue_repo)
    for template_fp, members in groups.items():
        lines.extend(
            _process_template(
                store,
                runner,
                issue_repo=issue_repo,
                dry_run=dry_run,
                template_fp=template_fp,
                members=members,
                now=now,
                history=history,
                cooldown_minutes=cooldown_minutes,
                open_burst=open_burst,
            )
        )
    return lines


class _OpenBurst:
    """The templates the currently open burst issue already lists, fetched once
    per scan and only when a template is about to get its own issue.

    A burst writes its issue in one gh call but marks its rows one at a time, so
    a process that dies mid-loop leaves rows pending for templates that carry no
    local history at all — the fold is recorded only in the issue. Without this
    lookup the retry would file a second, individual issue for a template the
    burst issue already covers, splitting one template across two issues."""

    def __init__(self, runner: Runner, issue_repo: str) -> None:
        self._runner = runner
        self._issue_repo = issue_repo
        self._number: int | None = None
        self._labels: set[str] | None = None

    def covers(self, label: str) -> int | None:
        """The open burst issue's number when it already lists this label."""
        if self._labels is None:
            self._number = find_issue_number(self._runner, self._issue_repo, STORM_MARKER)
            if self._number is None:
                self._labels = set()
            else:
                body = _issue_body(self._runner, self._issue_repo, self._number)
                self._labels = set(parse_variants_section(body))
        if label in self._labels:
            return self._number
        return None


def _process_template(
    store: Store,
    runner: Runner,
    *,
    issue_repo: str,
    dry_run: bool,
    template_fp: str,
    members: list[tuple[dict[str, Any], dict[str, Any]]],
    now: str,
    history: dict[str, datetime],
    cooldown_minutes: int,
    open_burst: _OpenBurst,
) -> list[str]:
    """Handle every pending row of one template together. Rows are grouped
    because two variants of the same template in one scan belong in one issue:
    processing them one by one would make the first a cooldown touch for the
    second and silently drop that variant."""
    lines: list[str] = []
    excerpts: list[str] = []
    for _row, seen_payload in members:
        excerpt = seen_payload.get("excerpt")
        excerpts.append(excerpt if isinstance(excerpt, str) else "")
    row_variants = [extract_variant(excerpt) for excerpt in excerpts]
    variants: list[str] = []
    for variant in row_variants:
        if variant not in variants:
            variants.append(variant)

    if _within_cooldown(history, template_fp, now, cooldown_minutes):
        for row, _seen_payload in members:
            rid = str(row.get("id") or "?")
            _mark(
                store,
                row,
                status="done",
                result={
                    "issue_repo": issue_repo,
                    "template_fingerprint": template_fp,
                    "at": now,
                    "skipped": "cooldown",
                },
            )
            lines.append(f"error.issue {rid} skipped-cooldown")
        return lines

    if dry_run:
        for index, (row, _seen_payload) in enumerate(members):
            rid = str(row.get("id") or "?")
            variant = row_variants[index]
            _mark(
                store,
                row,
                status="done",
                result={
                    "mode": "dry-run",
                    "issue_repo": issue_repo,
                    "template_fingerprint": template_fp,
                    "at": now,
                    "variant": variant,
                    "excerpt": excerpts[index][:300],
                },
            )
            lines.append(f"error.issue {rid} dry-run variant={variant}")
        return lines

    def fail_all(exc: StoreError) -> list[str]:
        for row, _seen_payload in members:
            rid = str(row.get("id") or "?")
            _mark(store, row, status="error", error=str(exc))
            lines.append(f"error.issue {rid} error")
        return lines

    try:
        number = find_issue_number(runner, issue_repo, marker_for(template_fp))
    except StoreError as exc:
        return fail_all(exc)

    first_payload = members[0][1]
    if number is None:
        try:
            burst_number = open_burst.covers(_storm_label(template_fp, first_payload))
        except StoreError as exc:
            return fail_all(exc)
        if burst_number is not None:
            for row, _seen_payload in members:
                rid = str(row.get("id") or "?")
                _mark(
                    store,
                    row,
                    status="done",
                    result={
                        "mode": "storm",
                        "issue_repo": issue_repo,
                        "template_fingerprint": template_fp,
                        "at": now,
                        "number": burst_number,
                        "created": False,
                    },
                )
                lines.append(f"error.issue {rid} already-in-burst number={burst_number}")
            return lines
        service = first_payload.get("service")
        service = service if isinstance(service, str) and service else "unknown"
        cls = first_payload.get("class")
        cls = cls if isinstance(cls, str) and cls else "error"
        try:
            url = _create_issue(
                runner,
                issue_repo=issue_repo,
                title=f"{service}: {cls}",
                template_fingerprint=template_fp,
                excerpt=excerpts[0][:1000],
                variants=variants,
                now=now,
            )
        except StoreError as exc:
            return fail_all(exc)
        for index, (row, _seen_payload) in enumerate(members):
            rid = str(row.get("id") or "?")
            variant = row_variants[index]
            _mark(
                store,
                row,
                status="done",
                result={
                    "issue_repo": issue_repo,
                    "template_fingerprint": template_fp,
                    "at": now,
                    "url": url,
                    "variant": variant,
                    "created": True,
                },
            )
            lines.append(f"error.issue {rid} created variant={variant}")
        return lines

    try:
        new_variants, comment_error = _update_issue(
            runner, issue_repo=issue_repo, number=number, variants=variants, now=now
        )
    except StoreError as exc:
        return fail_all(exc)

    for index, (row, _seen_payload) in enumerate(members):
        rid = str(row.get("id") or "?")
        variant = row_variants[index]
        result: dict[str, Any] = {
            "issue_repo": issue_repo,
            "template_fingerprint": template_fp,
            "at": now,
            "number": number,
            "variant": variant,
            "created": False,
            "new_variant": variant in new_variants,
        }
        if comment_error is not None:
            result["comment_error"] = comment_error[:500]
        _mark(store, row, status="done", result=result)
        suffix = " comment-failed" if comment_error is not None else ""
        lines.append(
            f"error.issue {rid} updated number={number} variant={variant} "
            f"new_variant={variant in new_variants}{suffix}"
        )
    return lines
