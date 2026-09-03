"""Automated fixer driver for error-fix implement tasks.

Drains open tasks with payload.error_id from spec_written through a draft
pr.open, the PR gates, and task state done using script control flow and
lane.launch() only — no Claude session. A human still merges the PR.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .chain import Step, close_allowed, is_error_fix_originated, next_steps
from .error_fix_act import _error_seen, _nonempty_str, _pr_open_result_number, _repo_ok
from .lane import LaneResult, Runner as LaneRunner, extract_findings_text
from .runtime import Completed, run_argv_killing_tree
from .run_core import (
    DEFAULT_ROUND_CAP,
    AgentLaunchPlan,
    RunOutcome,
    _agent_finish,
    _check_record,
    _fence_marker,
    _round_start,
    complete_spine_agent_step,
    execute_spine_step,
    launch_agent_plan,
    prepare_spine_agent_step,
)
from .store import Store, StoreError

Runner = Callable[[list[str]], Completed]

# Bound the per-task step loop (rounds × spine length, with headroom).
_MAX_STEPS_PER_TASK = 40

# Same-vendor PR-review dimensions that CONTRIBUTING.md requires in parallel.
_PR_DIMENSION_PAIRS = (
    frozenset({"grok_pr_quality", "grok_pr_logic"}),
    frozenset({"codex_pr_quality", "codex_pr_logic"}),
)

_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+")
# Longer forms first so "Mrs" wins over "Mr" on endswith checks.
_ABBREVIATIONS = ("Mrs", "e.g", "i.e", "etc", "Dr", "Mr", "vs")


def _ends_with_abbrev(prefix: str) -> bool:
    """True when prefix ends with a denylisted abbreviation (word-bounded)."""
    for abbr in _ABBREVIATIONS:
        if not prefix.endswith(abbr):
            continue
        start = len(prefix) - len(abbr)
        if start == 0 or not prefix[start - 1].isalnum():
            return True
    return False


def _first_sentence(text: str) -> str:
    """Return the first sentence of text, split on '. '/'!'/'?' boundaries.

    Periods after common abbreviations (e.g., Dr., vs.) are not boundaries.
    """
    stripped = text.strip()
    if not stripped:
        return ""
    for match in _SENTENCE_BOUNDARY_RE.finditer(stripped):
        punct_pos = match.start() - 1
        if punct_pos >= 0 and stripped[punct_pos] == ".":
            if _ends_with_abbrev(stripped[:punct_pos]):
                continue
        return stripped[: match.start()]
    return stripped


def _error_fix_brief(store: Store, session_id: str, error_id: str) -> str | None:
    """Return payload.brief from the session's error.fix row for this error_id."""
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("session_id") != session_id:
            continue
        if row.get("type") != "error.fix":
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict) or _nonempty_str(payload.get("error_id")) != error_id:
            continue
        brief = payload.get("brief")
        if isinstance(brief, str) and brief.strip():
            return brief.strip()
    return None


def write_error_fix_spec(
    store: Store,
    tid: str,
    *,
    error_id: str,
    session_id: str,
    repo: str,
    rejection_feedback: str | None = None,
) -> Path:
    """Write a five-part spec under $AGENT_HOME/error-fix-specs/<task_id>/.spec.md."""
    seen = _error_seen(store, session_id, error_id)
    seen_payload = seen.get("payload") if isinstance(seen.get("payload"), dict) else {}
    brief = _error_fix_brief(store, session_id, error_id) or ""
    fingerprint = _nonempty_str(seen_payload.get("fingerprint")) or ""
    service = _nonempty_str(seen_payload.get("service")) or ""
    environment = _nonempty_str(seen_payload.get("environment")) or ""
    class_name = _nonempty_str(seen_payload.get("class")) or ""
    # Never feed raw log excerpt fields into the spec (DESIGN.md §19.2).
    # Sibling of error-fix-work — never inside the pushed git worktree.
    parent = Path(store.home) / "error-fix-specs" / tid
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = parent / ".spec.md"
    if rejection_feedback:
        extracted = extract_findings_text(rejection_feedback)
        content = extracted if extracted else rejection_feedback
        fence = _fence_marker(content)
        rejection_section = (
            f"# Prior Rejection Feedback\n\n{fence}\n{content}\n{fence}\n\n"
        )
    else:
        rejection_section = ""
    brief_text = brief or "(no brief provided)"
    brief_fence = _fence_marker(brief_text)
    body = (
        f"# Context\n\n"
        f"- repo: `{repo}`\n"
        f"- error_id: `{error_id}`\n"
        f"- fingerprint: `{fingerprint}`\n"
        f"- service: `{service}`\n"
        f"- environment: `{environment}`\n"
        f"- class: `{class_name}`\n\n"
        f"# Task\n\n"
        f"{brief_fence}\n{brief_text}\n{brief_fence}\n\n"
        f"{rejection_section}"
        f"# Constraints\n\n"
        f"- Patch only what the brief requires.\n"
        f"- Do not commit secrets, credentials, or raw production log lines.\n"
        f"- Follow the target repository CONTRIBUTING.\n"
        f"- Do not push, open a PR, or run git/gh commands yourself -- the driver handles that after your patch.\n\n"
        f"# Verification\n\n"
        f"- Run the repository's usual local check (typically `pytest -q`).\n"
        f"- Confirm the failure mode described by the brief is addressed.\n\n"
        f"# Definition of Done\n\n"
        f"- Spec implemented and inner reviewer approved.\n"
        f"- Local checks pass.\n"
        f"- Four PR-review gates approved on this head.\n"
        f"- Contributing-doc check and any declared deviation resolved (allowed n_a where applicable).\n"
    )
    path.write_text(body, encoding="utf-8")
    return path


def template_pr_open_payload(
    *,
    session_id: str,
    repo: str,
    error_id: str,
    brief: str,
    fingerprint: str,
    title_suffix: str | None = None,
    base: str | None = None,
) -> dict[str, Any]:
    """Build pr.open payload (repo/title/head/body) per CONTRIBUTING.md."""
    short = error_id[:8]
    head = f"error-fix-{short}"
    suffix = (title_suffix or brief or f"error-fix {short}").strip()
    # One-line title body after the session prefix.
    if "\n" in suffix:
        suffix = suffix.splitlines()[0].strip()
    if len(suffix) > 72:
        suffix = suffix[:69] + "..."
    title = f"{session_id[:8]} - {suffix}"
    brief_summary = _first_sentence(brief).splitlines()[0].strip() if brief else ""
    # _first_sentence preserves the source's own terminal punctuation only
    # when a sentence-boundary match is found; text with no .!? at all comes
    # back unchanged, with nothing added. The empty-fallback literal already
    # ends in a period regardless.
    brief_part = brief_summary[:200] if brief_summary else "see task spec."
    brief_part_de = brief_summary[:200] if brief_summary else "siehe Task-Spec."
    en = (
        f"Automated error-fix for `{fingerprint or short}` in `{repo}`. "
        f"Draft only; a human merges. "
        f"Brief: {brief_part}"
    )
    de = (
        f"Automatischer error-fix für `{fingerprint or short}` in `{repo}`. "
        f"Nur Entwurf; ein Mensch merged. "
        f"Brief: {brief_part_de}"
    )
    brief_text = brief or "(none)"
    brief_fence = _fence_marker(brief_text)
    details = (
        f"<details>\n"
        f"<summary>Details</summary>\n\n"
        f"- error_id: `{error_id}`\n"
        f"- fingerprint: `{fingerprint}`\n"
        f"- head: `{head}`\n"
        f"- brief:\n\n{brief_fence}\n{brief_text}\n{brief_fence}\n\n"
        f"</details>\n"
    )
    body = f"EN:\n{en}\n\nDE:\n{de}\n\n{details}"
    return {
        "repo": repo,
        "title": title,
        "head": head,
        "body": body,
        "base": base,
    }


def _pr_open_row_exists(store: Store, *, head: str, repo: str) -> bool:
    """True when a successful pr.open already exists for this branch head.

    Only `done` skips the insert/resume path this predicate gates. A
    `pending` row is resumed via scan_github (no re-insert). An `error`
    row that carries a recorded `result.number` is re-pended (that
    specific row) then resumed via scan_github, preserving the number so
    resume does not recreate a duplicate PR. An `error` row with no
    recorded number, or no row at all, triggers a fresh
    insert_pr_open_and_scan. A real insert_pr_open_and_scan leaves `done`
    or `error` synchronously via scan_github.
    """
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "pr.open":
            continue
        if row.get("execution_status") != "done":
            continue
        payload = row.get("payload")
        if (
            isinstance(payload, dict)
            and payload.get("head") == head
            and payload.get("repo") == repo
        ):
            return True
    return False


def _pr_open_number(store: Store, *, head: str, repo: str) -> int | None:
    """Return result.number from a done pr.open for head, or None if missing."""
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "pr.open":
            continue
        if row.get("execution_status") != "done":
            continue
        payload = row.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("head") != head
            or payload.get("repo") != repo
        ):
            continue
        result = row.get("result")
        if not isinstance(result, dict):
            continue
        number = _pr_open_result_number(result)
        if number is None:
            continue
        return number
    return None


def _pr_open_base(store: Store, *, head: str, repo: str) -> str | None:
    """Return result.base from a done pr.open for head, or None if missing."""
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "pr.open":
            continue
        if row.get("execution_status") != "done":
            continue
        payload = row.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("head") != head
            or payload.get("repo") != repo
        ):
            continue
        result = row.get("result")
        if not isinstance(result, dict):
            continue
        base = result.get("base")
        if isinstance(base, str) and base:
            return base
        continue
    return None


def _pr_open_pending_row_exists(store: Store, *, head: str, repo: str) -> bool:
    """True when a mid-flight pr.open (execution_status=pending) exists for head."""
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "pr.open":
            continue
        if row.get("execution_status") != "pending":
            continue
        payload = row.get("payload")
        if (
            isinstance(payload, dict)
            and payload.get("head") == head
            and payload.get("repo") == repo
        ):
            return True
    return False


def _pr_open_recorded_number(
    store: Store, *, head: str, repo: str
) -> tuple[int, str] | None:
    """Return (number, execution_status) from a pending-or-error pr.open row for
    head/repo carrying a recorded result.number, or None if no such row exists.

    execution_status is one of "pending" or "error" -- whichever status the
    matching row currently has -- so the caller can pick the right message.
    """
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "pr.open":
            continue
        status = row.get("execution_status")
        if status not in ("pending", "error"):
            continue
        payload = row.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("head") != head
            or payload.get("repo") != repo
        ):
            continue
        result = row.get("result")
        if not isinstance(result, dict):
            continue
        number = _pr_open_result_number(result)
        if number is None:
            continue
        return (number, str(status))
    return None


def _pr_open_error_row_with_number(
    store: Store, *, head: str, repo: str
) -> dict[str, Any] | None:
    """Return the error-status pr.open row for head/repo carrying a recorded
    result.number, or None if no such row exists.

    Used to re-pend a specific row (rather than inserting a fresh one) when
    an earlier gh pr create succeeded but a later step (e.g. a permanent gh
    pr view auth failure) left the row in error status. Re-pending preserves
    has_prior_number for github_act.py's resume path, which is what stops a
    spurious "not found" from re-creating a duplicate PR.
    """
    origin = store.device_id()
    for row in store.rows("activity"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("type") != "pr.open":
            continue
        if row.get("execution_status") != "error":
            continue
        payload = row.get("payload")
        if (
            not isinstance(payload, dict)
            or payload.get("head") != head
            or payload.get("repo") != repo
        ):
            continue
        result = row.get("result")
        if not isinstance(result, dict):
            continue
        number = _pr_open_result_number(result)
        if number is None:
            continue
        return row
    return None


def insert_pr_open_and_scan(
    store: Store,
    *,
    session_id: str,
    payload: dict[str, Any],
    runner: Runner,
) -> list[str]:
    """Insert pending pr.open (cmd_activity-equivalent) and run scan_github.

    Direct store.write mirrors cmd_activity's non-error-fix branch for
    ACTIVITY_TYPES members (same pattern as error_fix_act owning its rows).
    """
    from .github_act import scan_github

    activity_id = str(uuid.uuid4())
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": session_id,
            "type": "pr.open",
            "payload": payload,
            "execution_status": "pending",
        },
    )
    return scan_github(store, runner)


def _close_spec_written(store: Store, tid: str, *, evidence: str) -> None:
    from . import main as main_mod

    snap = main_mod._chain_snapshot(store, tid)
    wf = str(snap["workflow"])
    verdict = close_allowed(
        wf,
        "spec_written",
        checklist=snap["checklist"],
        source="script",
        evidence=evidence,
        snapshot=snap,
    )
    if not verdict.allowed:
        raise StoreError(verdict.reason)
    main_mod.cmd_close_step(
        [
            "--task",
            tid,
            "--key",
            "spec_written",
            "--source",
            "script",
            "--evidence",
            evidence,
        ]
    )


def _contributing_ok_evidence(snap: dict[str, Any]) -> str:
    """Cite approved PR-gate records already in the ledger (vendor/dim/verdict@head).

    Only rows with verdict==approved on the snapshot's current head_sha count
    (same head scoping as chain._latest_gate). Missing/unapproved pairs raise
    StoreError instead of writing a literal "missing" into evidence.
    """
    want = (
        ("grok", "quality"),
        ("grok", "logic"),
        ("codex", "quality"),
        ("codex", "logic"),
    )
    want_head = str(snap.get("head_sha") or "").strip().lower()
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for g in snap.get("gates") or []:
        if not isinstance(g, dict):
            continue
        if str(g.get("verdict") or "") != "approved":
            continue
        g_head = str(g.get("head_sha") or "").strip().lower()
        if g_head != want_head:
            continue
        vendor = str(g.get("vendor") or "")
        dim = str(g.get("dimension") or "")
        by_key[(vendor, dim)] = g
    missing = [f"{vendor}/{dim}" for vendor, dim in want if (vendor, dim) not in by_key]
    if missing:
        raise StoreError(
            "approved PR gates missing or not on current head: "
            + ", ".join(missing)
        )
    parts: list[str] = []
    head = ""
    for vendor, dim in want:
        g = by_key[(vendor, dim)]
        verd = str(g.get("verdict") or "")
        sha = str(g.get("head_sha") or "")
        if sha and not head:
            head = sha
        parts.append(f"{vendor}/{dim}={verd}@{sha or '-'}")
    head_bit = f" head={head}" if head else ""
    return f"PR gates approved: {', '.join(parts)}{head_bit}"


def _ensure_done_readiness(store: Store, tid: str, *, brief: str) -> None:
    """Close contributing_ok, error-fix deviation n_a keys, and summaries.

    For ordinary implement tasks, deviation_declared / deviation_granted stay
    human-only (HUMAN_KEYS). For an error-fix-originated task, the same
    script-authorship carve-out chain.py grants for spec_written also lets
    this driver close both with n_a — a mechanically generated error-fix
    task has, by design, no deliberate CONTRIBUTING.md rule-bending to declare.
    """
    from . import main as main_mod

    checklist = {
        str(r["key"]): str(r["status"])
        for r in store.rows("checklist_item")
        if r.get("task_id") == tid
    }
    if checklist.get("contributing_ok") in (None, "pending", "nein"):
        snap = main_mod._chain_snapshot(store, tid)
        ready = next_steps(str(snap["workflow"]), snap["checklist"], spine_only=True)
        if ready and ready[0].key == "contributing_ok":
            main_mod.cmd_close_step(
                [
                    "--task",
                    tid,
                    "--key",
                    "contributing_ok",
                    "--source",
                    "script",
                    "--evidence",
                    _contributing_ok_evidence(snap),
                ]
            )
    evidence = (
        "error-fix task: no CONTRIBUTING.md deviation, mechanically generated"
    )
    for key in ("deviation_declared", "deviation_granted"):
        snap = main_mod._chain_snapshot(store, tid)
        checklist = snap["checklist"]
        if checklist.get(key) not in (None, "pending", "nein"):
            continue
        ready = next_steps(str(snap["workflow"]), checklist, spine_only=False)
        if not any(s.key == key for s in ready):
            continue
        verdict = close_allowed(
            str(snap["workflow"]),
            key,
            checklist=checklist,
            source="script",
            evidence=evidence,
            status="n_a",
            snapshot=snap,
        )
        if not verdict.allowed:
            raise StoreError(verdict.reason)
        main_mod.cmd_close_step(
            [
                "--task",
                tid,
                "--key",
                key,
                "--source",
                "script",
                "--status",
                "n_a",
                "--evidence",
                evidence,
            ]
        )
    task = store.row("task", tid)
    if task is None:
        return
    en = (task.get("change_summary_en") or "").strip()
    de = (task.get("change_summary_de") or "").strip()
    if not en or not de:
        one = (brief or task.get("title") or "error-fix").splitlines()[0].strip()
        if len(one) > 120:
            one = one[:117] + "..."
        de_one = (
            f"Automatischer error-fix-Patch. Brief: {one}"
            if one
            else "Automatischer error-fix Patch."
        )
        if len(de_one) > 120:
            de_one = de_one[:117] + "..."
        main_mod.cmd_task(
            [
                "summary",
                "--id",
                tid,
                "--en",
                one or "error-fix patch.",
                "--de",
                de_one,
            ]
        )


def _finish_task_done(store: Store, tid: str, *, brief: str) -> str:
    """Close readiness gates, then set task state done; report StoreError as blocked.

    A StoreError from _ensure_done_readiness (e.g. a stale/missing PR-gate
    evidence check inside _contributing_ok_evidence) must not propagate —
    callers driving this in a scan loop would otherwise hit the identical
    failure on every subsequent scan with no visible terminal signal.
    """
    from . import main as main_mod

    try:
        _ensure_done_readiness(store, tid, brief=brief)
    except StoreError as exc:
        return f"error-fix-work {tid} contributing_ok-blocked ({exc})"
    try:
        main_mod.cmd_task(["state", tid, "done"])
    except SystemExit as exc:
        return f"error-fix-work {tid} done-blocked ({exc})"
    return f"error-fix-work {tid} done"


def _open_error_fix_tasks(store: Store) -> list[dict[str, Any]]:
    origin = store.device_id()
    out: list[dict[str, Any]] = []
    for row in store.rows("task"):
        if row.get("_origin_device_id") != origin:
            continue
        if row.get("workflow") != "implement":
            continue
        state = str(row.get("state") or "")
        if state in ("done", "failed"):
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict) or not _nonempty_str(payload.get("error_id")):
            continue
        out.append(row)
    out.sort(key=lambda r: str(r.get("id") or ""))
    return out


def _ready_pr_dimension_pair(ready: list[Step]) -> list[Step] | None:
    """If ready holds both dimensions of one vendor pair, return them in ready order."""
    ready_keys = {s.key for s in ready}
    for pair in _PR_DIMENSION_PAIRS:
        if pair <= ready_keys:
            return [s for s in ready if s.key in pair]
    return None


def _apply_drive_outcome(
    store: Store,
    tid: str,
    outcome: RunOutcome,
    *,
    head: str | None,
    brief: str,
) -> tuple[str | None, str | None, bool]:
    """Map one RunOutcome to (return_message | None, updated_head, should_continue).

    Per-outcome only: does not write rejection feedback (the batch aggregator
    combines findings and writes once). Callers must not early-return on the
    first message when processing a multi-outcome batch — use
    ``_aggregate_drive_outcomes`` instead.
    """
    updated_head = outcome.head_sha or head

    if outcome.kind == "idle":
        return _finish_task_done(store, tid, brief=brief), updated_head, False

    if outcome.kind == "human_required":
        return (
            f"error-fix-work {tid} human-required key={outcome.key}",
            updated_head,
            False,
        )

    if outcome.kind == "failed":
        return (
            f"error-fix-work {tid} failed "
            f"({outcome.message or outcome.reason or 'failed'})",
            updated_head,
            False,
        )

    if outcome.kind == "local_check_failed":
        return f"error-fix-work {tid} failed (local_check)", updated_head, False

    if outcome.kind == "agent_handoff":
        return (
            f"error-fix-work {tid} blocked (agent handoff key={outcome.key})",
            updated_head,
            False,
        )

    if outcome.kind == "not_closable":
        return (
            f"error-fix-work {tid} not-closable "
            f"key={outcome.key} ({outcome.reason})",
            updated_head,
            False,
        )

    if outcome.kind == "vendor_unavailable":
        return (
            f"error-fix-work {tid} vendor-cli-unavailable "
            f"({outcome.reason or outcome.message or 'lane unavailable'})",
            updated_head,
            False,
        )

    if outcome.kind == "rejected_new_round":
        # PR-gate rejection resets `pushed` (see _PR_REJECT_RESET_KEYS) and
        # expects a new commit — drop the stale head so the next push is
        # not compared against the pre-rejection sha. Inner reviewer
        # rejection keeps key="reviewer_approved" and does not reset pushed.
        if outcome.key != "reviewer_approved":
            updated_head = None
        return None, updated_head, True

    if outcome.kind in ("closed", "agent_closed"):
        return None, updated_head, True

    return (
        f"error-fix-work {tid} stop kind={outcome.kind}",
        updated_head,
        False,
    )


# When every outcome in a batch is terminal (no cont), pick one message.
# More actionable kinds win; ties keep pair / encounter order.
_TERMINAL_MESSAGE_PRIORITY = {
    "failed": 0,
    "not_closable": 1,
    "agent_handoff": 2,
    "vendor_unavailable": 3,
}


def _combine_rejection_feedback(sections: list[tuple[str, str]]) -> str:
    """Attribute each dimension's findings under a ``## <key>`` heading."""
    parts: list[str] = []
    for key, findings in sections:
        body = findings.strip()
        parts.append(f"## {key}\n\n{body}" if body else f"## {key}")
    return "\n\n".join(parts)


def _write_rejection_feedback(
    store: Store,
    tid: str,
    outcomes: list[RunOutcome],
    *,
    error_id: str,
    repo: str,
    session_id: str,
) -> None:
    """Combine every rejected_new_round outcome's findings and persist once, if any.

    Shared by ``_aggregate_drive_outcomes`` (normal-return batch aggregation) and
    ``_drive_parallel_pr_pair``'s exception path (so rejection feedback already
    recorded before a launch-phase exception still reaches ``.spec.md``).
    """
    rejection_sections: list[tuple[str, str]] = []
    for outcome in outcomes:
        if outcome.kind != "rejected_new_round":
            continue
        findings = outcome.rejection_findings
        if not findings:
            continue
        rejection_sections.append((outcome.key or "rejection", findings))
    if rejection_sections and error_id and repo:
        write_error_fix_spec(
            store,
            tid,
            error_id=error_id,
            session_id=session_id,
            repo=repo,
            rejection_feedback=_combine_rejection_feedback(rejection_sections),
        )


def _batch_should_round_start(outcomes: list[RunOutcome]) -> bool:
    """True when a deferred round-start is owed and no outcome failed the task.

    A ``failed`` outcome in the same batch must never be followed by
    ``_round_start``: that would reopen ``task.state`` back to ``implementing``.
    """
    return not any(o.kind == "failed" for o in outcomes) and any(
        o.needs_round_start for o in outcomes
    )


def _aggregate_drive_outcomes(
    store: Store,
    tid: str,
    outcomes: list[RunOutcome],
    *,
    head: str | None,
    error_id: str,
    repo: str,
    session_id: str,
    brief: str,
) -> tuple[str | None, str | None, bool]:
    """Decide continue vs. message once for a batch of RunOutcomes.

    Writes combined rejection feedback at most once, applies every outcome
    unconditionally, then: a ``failed`` outcome's message wins over any
    sibling ``cont=True``; otherwise any ``cont=True`` wins over any message;
    otherwise the most actionable terminal message is returned. A PR-gate
    rejection in the batch forces ``head=None`` regardless of processing order.
    """
    _write_rejection_feedback(
        store,
        tid,
        outcomes,
        error_id=error_id,
        repo=repo,
        session_id=session_id,
    )

    any_cont = False
    force_head_none = False
    messages: list[tuple[str, str]] = []
    current_head = head
    for outcome in outcomes:
        msg, current_head, cont = _apply_drive_outcome(
            store,
            tid,
            outcome,
            head=current_head,
            brief=brief,
        )
        if (
            outcome.kind == "rejected_new_round"
            and outcome.key != "reviewer_approved"
        ):
            force_head_none = True
        if cont:
            any_cont = True
        if msg is not None:
            messages.append((outcome.kind, msg))

    final_head: str | None = None if force_head_none else current_head

    # A failed outcome must surface even when a sibling wants to continue;
    # otherwise the unattended loop would keep retrying a terminal failure.
    if any_cont and not any(o.kind == "failed" for o in outcomes):
        return None, final_head, True

    if not messages:
        kind = outcomes[-1].kind if outcomes else "empty"
        return f"error-fix-work {tid} stop kind={kind}", final_head, False

    # failed > not_closable > agent_handoff > vendor_unavailable > other;
    # equal priority keeps first-encountered (pair order).
    best_msg = min(
        enumerate(messages),
        key=lambda ikm: (
            _TERMINAL_MESSAGE_PRIORITY.get(ikm[1][0], 50),
            ikm[0],
        ),
    )[1][1]
    return best_msg, final_head, False


def _release_pair_working_agents(
    store: Store,
    tid: str,
    pair: list[Step],
    *,
    note: str,
) -> None:
    """Best-effort: finish any still-working agent row for either pair dimension."""
    from . import main as main_mod

    for step in pair:
        role = str(step.role or "")
        vendor = str(step.vendor or "")
        # PR-reviewer rows are started with round_num=None; a None lookup
        # matches any round for that role/vendor (see _find_working_agent).
        working = main_mod._find_working_agent(
            store, tid, role=role, vendor=vendor, round_num=None
        )
        if working is not None:
            _agent_finish(str(working["id"]), "unavailable", note=note)


def _drive_parallel_pr_pair(
    store: Store,
    tid: str,
    pair: list[Step],
    *,
    head: str | None,
    spec_file: str | None,
    cwd: str,
    snap: dict[str, Any],
    task: dict[str, Any],
    runner: Runner,
    lane_runner: LaneRunner | None,
    round_cap: int,
    error_id: str,
    repo: str,
    session_id: str,
) -> list[RunOutcome]:
    """Prepare both dimensions on this thread, launch concurrently, finish here.

    Store I/O stays on the calling thread. Workers only call launch_agent_plan
    (lane.launch) — never Store methods — so this is safe under store.exclusive's
    threading.RLock.

    Outcomes are always returned in ``pair`` order. Every dimension that
    yields a genuine ``LaneResult`` is finished (gate / checklist / agent row)
    via ``complete_spine_agent_step`` before any deferred launch exception is
    raised; there is no abandon/discard path for successful siblings. Task-level
    continue-vs-message is decided later by the caller across the whole batch;
    the combined rejection-feedback write is done by the caller on the normal
    path, and best-effort here on the exception path so findings already
    collected are not lost. On any unhandled exception, still-working agent
    rows for either dimension are released before re-raising; if an earlier
    dimension already committed a rejection reset that still needs a
    round-start (and no outcome failed the task), that round-start runs
    best-effort before the original exception propagates.
    """
    from . import main as main_mod

    exec_argv = lambda argv, cwd=None, timeout=None: _runner_to_completed(  # noqa: E731
        runner, argv, cwd=cwd, timeout=timeout
    )

    # Visible to the except handler so a committed rejection reset can still
    # receive its deferred round-start when a later dimension raises.
    outcomes: list[RunOutcome] = []
    try:
        # One entry per pair step, in pair order — early outcomes and plans
        # stay interleaved so returned outcomes follow pair order.
        prepared: list[RunOutcome | AgentLaunchPlan] = []
        for step in pair:
            # Re-snapshot so the second prepare sees agents started by the first.
            snap = main_mod._chain_snapshot(store, tid, extra_head=head)
            task = store.row("task", tid) or task
            prepared.append(
                prepare_spine_agent_step(
                    store,
                    tid,
                    step,
                    head=head,
                    spec_file=spec_file,
                    cwd=cwd,
                    snap=snap,
                    task=task,
                    exec_argv=exec_argv,
                )
            )

        plans = [p for p in prepared if isinstance(p, AgentLaunchPlan)]
        launch_results: dict[str, LaneResult | BaseException] = {}
        if plans:
            with ThreadPoolExecutor(max_workers=len(plans)) as pool:
                futures = {
                    pool.submit(
                        launch_agent_plan, plan, runner=lane_runner, tmux=False
                    ): plan
                    for plan in plans
                }
                for fut in as_completed(futures):
                    plan = futures[fut]
                    try:
                        launch_results[plan.step.key] = fut.result()
                    except BaseException as exc:  # noqa: BLE001 — surface to caller
                        launch_results[plan.step.key] = exc

        pending_exception: BaseException | None = None
        # Plan whose complete_spine_agent_step raised (retry-phase); launch-
        # phase exceptions already release their agent row in-loop below.
        pending_exception_plan: AgentLaunchPlan | None = None
        for item in prepared:
            if isinstance(item, RunOutcome):
                outcomes.append(item)
                continue

            plan = item
            payload = launch_results[plan.step.key]
            if isinstance(payload, OSError):
                # Mirror execute_spine_step: release this agent; the except
                # sweep below releases any sibling still working.
                working = main_mod._find_working_agent(
                    store,
                    tid,
                    role=plan.role,
                    vendor=plan.vendor,
                    round_num=plan.round_num,
                )
                if working is not None:
                    _agent_finish(
                        str(working["id"]),
                        "unavailable",
                        note=f"launch failed ({plan.role} {plan.vendor})",
                    )
                if pending_exception is None:
                    pending_exception = payload
                continue
            if isinstance(payload, BaseException):
                if pending_exception is None:
                    pending_exception = payload
                continue
            # Guard retry-phase exceptions the same way as launch-phase ones:
            # keep finishing every remaining genuine LaneResult before raising.
            try:
                outcome = complete_spine_agent_step(
                    store,
                    tid,
                    plan,
                    payload,
                    round_cap=round_cap,
                    tmux=False,
                    runner=lane_runner,
                    exec_argv=exec_argv,
                    defer_round_start=True,
                )
            except BaseException as exc:  # noqa: BLE001 — surface after siblings
                if pending_exception is None:
                    pending_exception = exc
                    pending_exception_plan = plan
                continue
            outcomes.append(outcome)
        if pending_exception is not None:
            if pending_exception_plan is not None:
                working = main_mod._find_working_agent(
                    store,
                    tid,
                    role=pending_exception_plan.role,
                    vendor=pending_exception_plan.vendor,
                    round_num=pending_exception_plan.round_num,
                )
                if working is not None:
                    _agent_finish(
                        str(working["id"]),
                        "unavailable",
                        note=(
                            f"launch failed ({pending_exception_plan.role} "
                            f"{pending_exception_plan.vendor})"
                        ),
                    )
            raise pending_exception
        if _batch_should_round_start(outcomes):
            _round_start(tid)
        return outcomes
    except BaseException:
        _release_pair_working_agents(
            store,
            tid,
            pair,
            note="parallel PR-dimension pair aborted",
        )
        try:
            _write_rejection_feedback(
                store,
                tid,
                outcomes,
                error_id=error_id,
                repo=repo,
                session_id=session_id,
            )
        except (Exception, SystemExit):
            pass
        # Best-effort: keep a committed rejection reset consistent with a new
        # task_round. Never let round_start's failure mask the original
        # exception (e.g. OSError → vendor-cli-unavailable in _drive_one).
        if _batch_should_round_start(outcomes):
            try:
                _round_start(tid)
            except (Exception, SystemExit):
                pass
        raise


def _drive_one(
    store: Store,
    task: dict[str, Any],
    runner: Runner,
    *,
    round_cap: int,
    lane_runner: LaneRunner | None = None,
) -> str:
    from . import main as main_mod

    tid = str(task["id"])
    current_round = int(task.get("current_round") or 0)
    if current_round > round_cap:
        cap_msg = f"round cap {round_cap} reached (current_round={current_round})"
        _check_record(
            tid=tid,
            name="round-cap",
            command=f"round_cap={round_cap}",
            result="fail",
            output=cap_msg,
        )
        return f"error-fix-work {tid} failed ({cap_msg})"
    session_id = str(task.get("session_id") or "")
    payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
    raw_error_id = payload.get("error_id")
    error_id = _nonempty_str(raw_error_id) or ""
    if not error_id and isinstance(raw_error_id, str) and raw_error_id != "":
        # Present but strips to empty (e.g. a stale store row bypassing
        # create-time validation) — fail loudly instead of silently building
        # a garbage "error-fix- " branch head from it.
        return f"error-fix-work {tid} failed (payload.error_id is whitespace-only)"
    repo = _repo_ok(payload.get("repo") or task.get("repo")) or ""
    brief = _error_fix_brief(store, session_id, error_id) or ""
    worktree = Path(store.home) / "error-fix-work" / tid
    # Thread pushed SHA across steps (mirrors cmd_run's extra_head=head).
    head: str | None = None
    steps = 0

    while steps < _MAX_STEPS_PER_TASK:
        steps += 1
        task = store.row("task", tid) or task
        if str(task.get("state") or "") in ("done", "failed"):
            return f"error-fix-work {tid} state={task.get('state')}"
        if not (worktree / ".git").is_dir():
            return f"error-fix-work {tid} worktree-not-ready"
        cwd = str(worktree)

        snap = main_mod._chain_snapshot(store, tid, extra_head=head)
        if not is_error_fix_originated(snap):
            return f"error-fix-work {tid} skip (not error-fix)"
        # Closed sessions must not trigger GitHub/git side effects. Mirror
        # main._require_task_session_active without failing the task — a
        # closed session is a skip for this scan, not a task failure.
        if not snap.get("session_active"):
            return f"error-fix-work {tid} skip (session inactive)"
        snap_head = str(snap.get("head_sha") or "").strip()
        if snap_head and not head:
            head = snap_head

        checklist = snap["checklist"]
        pr_head = f"error-fix-{error_id[:8]}" if error_id else ""
        if (
            error_id
            and repo
            and checklist.get("pushed") == "ja"
            and not _pr_open_row_exists(store, head=pr_head, repo=repo)
        ):
            try:
                error_row = _pr_open_error_row_with_number(
                    store, head=pr_head, repo=repo
                )
                if error_row is not None:
                    # A gh pr create succeeded earlier (result.number recorded) but
                    # a LATER step left the row in error status (e.g. a permanent
                    # gh pr view auth failure). scan_github only drains
                    # store.pending_work(), so this row is invisible to it until
                    # re-pended -- re-pending (instead of inserting a fresh row)
                    # keeps has_prior_number=True on resume, avoiding a duplicate
                    # gh pr create.
                    from .github_act import _mark, scan_github

                    _mark(
                        store,
                        error_row,
                        status="pending",
                        result=error_row.get("result"),
                    )
                    scan_github(store, runner)
                elif _pr_open_pending_row_exists(store, head=pr_head, repo=repo):
                    # Crash between insert and scan left a pending row — resume
                    # it rather than inserting a duplicate.
                    from .github_act import scan_github

                    scan_github(store, runner)
                else:
                    seen = _error_seen(store, session_id, error_id)
                    seen_payload = (
                        seen.get("payload")
                        if isinstance(seen.get("payload"), dict)
                        else {}
                    )
                    fingerprint = _nonempty_str(seen_payload.get("fingerprint")) or ""
                    resolved_ref = _nonempty_str(task.get("ref"))
                    pr_base = (
                        (resolved_ref.removeprefix("origin/") or None)
                        if resolved_ref is not None
                        else None
                    )
                    pr_payload = template_pr_open_payload(
                        session_id=session_id,
                        repo=repo,
                        error_id=error_id,
                        brief=brief,
                        fingerprint=fingerprint,
                        title_suffix=str(task.get("title") or ""),
                        base=pr_base,
                    )
                    insert_pr_open_and_scan(
                        store,
                        session_id=session_id,
                        payload=pr_payload,
                        runner=runner,
                    )
                # Persistent gh pr create failures are almost always external
                # (auth/rate-limit/permissions). Leave the task untouched for the
                # next scan rather than failing it; each cron/knock scan retries.
                if not _pr_open_row_exists(store, head=pr_head, repo=repo):
                    recorded = _pr_open_recorded_number(
                        store, head=pr_head, repo=repo
                    )
                    if recorded is not None:
                        number, status = recorded
                        if status == "pending":
                            return f"error-fix-work {tid} pr.open-pending (retry needed)"
                        return (
                            f"error-fix-work {tid} pr.open-error "
                            f"(PR #{number} recorded; view/auth retry needed)"
                        )
                    return f"error-fix-work {tid} pr.open-error (create failed)"
            except (StoreError, OSError, SystemExit) as exc:
                return f"error-fix-work {tid} pr.open-error ({exc})"
            # Fall through so this scan can continue the spine; next scan
            # skips once the pr.open row exists.

        # Independent of the block above: backfill payload.pr_number whenever a
        # done pr.open row exists for this head, regardless of whether THIS scan
        # created it (or it was created — and left unbackfilled — in an earlier,
        # since-crashed scan). Must not be nested inside the "does pr.open need
        # creating" guard above, since that guard is permanently False once the
        # row is done, which would otherwise permanently skip a missed backfill.
        if (
            error_id
            and repo
            and pr_head
            and _pr_open_row_exists(store, head=pr_head, repo=repo)
        ):
            try:
                pr_number = _pr_open_number(store, head=pr_head, repo=repo)
                pr_base = _pr_open_base(store, head=pr_head, repo=repo)
                task = store.row("task", tid) or task
                dirty = False
                if pr_number is not None:
                    task_payload = (
                        task.get("payload")
                        if isinstance(task.get("payload"), dict)
                        else {}
                    )
                    if task_payload.get("pr_number") != pr_number:
                        task_payload = dict(task_payload)
                        task_payload["pr_number"] = pr_number
                        task["payload"] = task_payload
                        dirty = True
                if pr_base:
                    normalized_pr_base = f"origin/{pr_base}"
                    if normalized_pr_base != task.get("ref"):
                        task["ref"] = normalized_pr_base
                        dirty = True
                if dirty:
                    store.write("task", "update", tid, main_mod._strip(task))
            except (StoreError, OSError, SystemExit) as exc:
                return f"error-fix-work {tid} pr.open-error ({exc})"

        ready = next_steps(str(snap["workflow"]), snap["checklist"], spine_only=True)
        if not ready:
            return _finish_task_done(store, tid, brief=brief)

        step = ready[0]

        if step.key == "spec_written":
            if not error_id or not repo:
                return f"error-fix-work {tid} failed (missing error_id/repo)"
            write_error_fix_spec(
                store,
                tid,
                error_id=error_id,
                session_id=session_id,
                repo=repo,
            )
            evidence = f"auto spec from error.fix brief (error_id={error_id[:8]})"
            try:
                _close_spec_written(store, tid, evidence=evidence)
            except (StoreError, SystemExit) as exc:
                return f"error-fix-work {tid} spec_written-blocked ({exc})"
            # First round (current_round 0 → 1), same as test_run bootstrap.
            main_mod.cmd_round(["start", "--task", tid])
            continue

        if step.key == "contributing_ok":
            try:
                main_mod.cmd_close_step(
                    [
                        "--task",
                        tid,
                        "--key",
                        "contributing_ok",
                        "--source",
                        "script",
                        "--evidence",
                        _contributing_ok_evidence(snap),
                    ]
                )
            except (StoreError, SystemExit) as exc:
                return f"error-fix-work {tid} contributing_ok-blocked ({exc})"
            continue

        # Spec lives under error-fix-specs, never inside the pushed worktree.
        spec_path = Path(store.home) / "error-fix-specs" / tid / ".spec.md"
        if not spec_path.is_file() and error_id and repo:
            write_error_fix_spec(
                store,
                tid,
                error_id=error_id,
                session_id=session_id,
                repo=repo,
            )

        pair = _ready_pr_dimension_pair(ready)
        if pair is not None:
            try:
                outcomes = _drive_parallel_pr_pair(
                    store,
                    tid,
                    pair,
                    head=head,
                    spec_file=str(spec_path) if spec_path.is_file() else None,
                    cwd=cwd,
                    snap=snap,
                    task=task,
                    runner=runner,
                    lane_runner=lane_runner,
                    round_cap=round_cap,
                    error_id=error_id,
                    repo=repo,
                    session_id=session_id,
                )
            except OSError as exc:
                return (
                    f"error-fix-work {tid} vendor-cli-unavailable "
                    f"({type(exc).__name__}: {exc})"
                )
            # Finish writes already happened per dimension; decide continue vs.
            # message once across the whole batch (order-independent).
            msg, head, cont = _aggregate_drive_outcomes(
                store,
                tid,
                outcomes,
                head=head,
                error_id=error_id,
                repo=repo,
                session_id=session_id,
                brief=brief,
            )
            if msg is not None:
                return msg
            if cont:
                continue
            kind = outcomes[-1].kind if outcomes else "empty"
            return f"error-fix-work {tid} stop kind={kind}"

        try:
            outcome = execute_spine_step(
                store,
                tid,
                head=head,
                spec_file=str(spec_path) if spec_path.is_file() else None,
                cwd=cwd,
                tmux=False,
                runner=lane_runner,
                round_cap=round_cap,
                # cwd-aware like main._exec_argv so local_check_pass runs in worktree.
                exec_argv=lambda argv, cwd=None, timeout=None: _runner_to_completed(
                    runner, argv, cwd=cwd, timeout=timeout
                ),
            )
        except OSError as exc:
            # Missing vendor CLI binary: mutate nothing, leave task for next scan.
            return (
                f"error-fix-work {tid} vendor-cli-unavailable "
                f"({type(exc).__name__}: {exc})"
            )

        msg, head, cont = _aggregate_drive_outcomes(
            store,
            tid,
            [outcome],
            head=head,
            error_id=error_id,
            repo=repo,
            session_id=session_id,
            brief=brief,
        )
        if msg is not None:
            return msg
        if cont:
            continue
        return f"error-fix-work {tid} stop kind={outcome.kind}"

    return f"error-fix-work {tid} step-cap"


def _runner_to_completed(
    runner: Runner, argv: list[str], *, cwd: str | None = None,
    timeout: float | None = None,
) -> Completed:
    """Run argv; honor cwd like main._exec_argv so checks use the worktree."""
    limit = 120 if timeout is None else timeout
    try:
        if cwd is not None:
            return run_argv_killing_tree(argv, cwd=cwd, timeout=limit)
        return runner(argv)
    except OSError as exc:
        return Completed(127, "", str(exc))


def drive_error_fix_tasks(
    store: Store,
    runner: Runner,
    *,
    round_cap: int = DEFAULT_ROUND_CAP,
    lane_runner: LaneRunner | None = None,
) -> list[str]:
    """Drive every open error-fix implement task one scan. Return summary lines."""
    with store.exclusive("error-fix-work:" + store.device_id()):
        lines: list[str] = []
        for task in _open_error_fix_tasks(store):
            tid = str(task.get("id") or "")
            try:
                lines.append(
                    _drive_one(
                        store,
                        task,
                        runner,
                        round_cap=round_cap,
                        lane_runner=lane_runner,
                    )
                )
            except (Exception, SystemExit) as exc:
                lines.append(
                    f"error-fix-work {tid} scan-error "
                    f"({type(exc).__name__}: {exc})"
                )
        return lines
