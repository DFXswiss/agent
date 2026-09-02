"""Automated fixer driver for error-fix implement tasks.

Drains open tasks with payload.error_id from spec_written through a draft
pr.open, the PR gates, and task state done using script control flow and
lane.launch() only — no Claude session. A human still merges the PR.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .chain import close_allowed, is_error_fix_originated, next_steps
from .error_fix_act import _error_seen, _nonempty_str, _repo_ok
from .lane import Runner as LaneRunner, extract_findings_text
from .runtime import Completed
from .run_core import DEFAULT_ROUND_CAP, RunOutcome, execute_spine_step
from .store import Store, StoreError

Runner = Callable[[list[str]], Completed]

# Bound the per-task step loop (rounds × spine length, with headroom).
_MAX_STEPS_PER_TASK = 40

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


def _fence_marker(text: str) -> str:
    """Backtick fence one longer than the longest run inside text (min 3)."""
    longest = 0
    run = 0
    for ch in text:
        if ch == "`":
            run += 1
            if run > longest:
                longest = run
        else:
            run = 0
    return "`" * max(3, longest + 1)


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
    """Write a five-part spec under $AGENT_HOME/error-fix-work/<task_id>/.spec.md."""
    seen = _error_seen(store, session_id, error_id)
    seen_payload = seen.get("payload") if isinstance(seen.get("payload"), dict) else {}
    brief = _error_fix_brief(store, session_id, error_id) or ""
    fingerprint = _nonempty_str(seen_payload.get("fingerprint")) or ""
    service = _nonempty_str(seen_payload.get("service")) or ""
    environment = _nonempty_str(seen_payload.get("environment")) or ""
    class_name = _nonempty_str(seen_payload.get("class")) or ""
    # Never feed raw log excerpt fields into the spec (DESIGN.md §19.2).
    parent = Path(store.home) / "error-fix-work" / tid
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
        f"- Open a draft pull request only; a human merges.\n\n"
        f"# Verification\n\n"
        f"- Run the repository's usual local check (typically `pytest -q`).\n"
        f"- Confirm the failure mode described by the brief is addressed.\n\n"
        f"# Definition of Done\n\n"
        f"- Spec implemented and inner reviewer approved.\n"
        f"- Local checks pass; branch pushed; draft PR opened.\n"
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
    en = (
        f"Automated error-fix for `{fingerprint or short}` in `{repo}`. "
        f"Draft only; a human merges. "
        f"Brief: {brief_summary[:200] if brief_summary else 'see task spec'}."
    )
    de = (
        f"Automatischer error-fix für `{fingerprint or short}` in `{repo}`. "
        f"Nur Entwurf; ein Mensch merged. "
        f"Brief: {brief_summary[:200] if brief_summary else 'siehe Task-Spec'}."
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
    }


def _pr_open_row_exists(store: Store, *, head: str) -> bool:
    """True when a successful pr.open already exists for this branch head.

    Only `done` skips the insert/resume path entirely. A `pending` row is
    resumed via scan_github (no re-insert); an `error` row triggers a fresh
    insert_pr_open_and_scan. A real insert_pr_open_and_scan leaves `done` or
    `error` synchronously via scan_github.
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
        if isinstance(payload, dict) and payload.get("head") == head:
            return True
    return False


def _pr_open_pending_row_exists(store: Store, *, head: str) -> bool:
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
        if isinstance(payload, dict) and payload.get("head") == head:
            return True
    return False


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
        snap_head = str(snap.get("head_sha") or "").strip()
        if snap_head and not head:
            head = snap_head

        checklist = snap["checklist"]
        if (
            error_id
            and repo
            and checklist.get("pushed") == "ja"
            and not _pr_open_row_exists(store, head=f"error-fix-{error_id[:8]}")
        ):
            try:
                pr_head = f"error-fix-{error_id[:8]}"
                if _pr_open_pending_row_exists(store, head=pr_head):
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
                    pr_payload = template_pr_open_payload(
                        session_id=session_id,
                        repo=repo,
                        error_id=error_id,
                        brief=brief,
                        fingerprint=fingerprint,
                        title_suffix=str(task.get("title") or ""),
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
                if not _pr_open_row_exists(store, head=pr_head):
                    return f"error-fix-work {tid} pr.open-error (create failed)"
            except (StoreError, OSError, SystemExit) as exc:
                return f"error-fix-work {tid} pr.open-error ({exc})"
            # Fall through so this scan can continue the spine; next scan
            # skips once the pr.open row exists.

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

        spec_path = worktree / ".spec.md"
        if not spec_path.is_file() and error_id and repo:
            write_error_fix_spec(
                store,
                tid,
                error_id=error_id,
                session_id=session_id,
                repo=repo,
            )

        try:
            outcome: RunOutcome = execute_spine_step(
                store,
                tid,
                head=head,
                spec_file=str(spec_path) if spec_path.is_file() else None,
                cwd=cwd,
                tmux=False,
                runner=lane_runner,
                round_cap=round_cap,
                # cwd-aware like main._exec_argv so local_check_pass runs in worktree.
                exec_argv=lambda argv, cwd=None: _runner_to_completed(
                    runner, argv, cwd=cwd
                ),
            )
        except OSError as exc:
            # Missing vendor CLI binary: mutate nothing, leave task for next scan.
            return (
                f"error-fix-work {tid} vendor-cli-unavailable "
                f"({type(exc).__name__}: {exc})"
            )

        if outcome.head_sha:
            head = outcome.head_sha

        if outcome.kind == "idle":
            return _finish_task_done(store, tid, brief=brief)

        if outcome.kind == "human_required":
            return f"error-fix-work {tid} human-required key={outcome.key}"

        if outcome.kind == "failed":
            return (
                f"error-fix-work {tid} failed "
                f"({outcome.message or outcome.reason or 'failed'})"
            )

        if outcome.kind == "local_check_failed":
            return f"error-fix-work {tid} failed (local_check)"

        if outcome.kind == "agent_handoff":
            return f"error-fix-work {tid} blocked (agent handoff key={outcome.key})"

        if outcome.kind == "not_closable":
            return (
                f"error-fix-work {tid} not-closable "
                f"key={outcome.key} ({outcome.reason})"
            )

        if outcome.kind == "vendor_unavailable":
            return (
                f"error-fix-work {tid} vendor-cli-unavailable "
                f"({outcome.reason or outcome.message or 'lane unavailable'})"
            )

        if outcome.kind == "rejected_new_round":
            # PR-gate rejection resets `pushed` (see _PR_REJECT_RESET_KEYS) and
            # expects a new commit — drop the stale head so the next push is
            # not compared against the pre-rejection sha. Inner reviewer
            # rejection keeps key="reviewer_approved" and does not reset pushed.
            if outcome.key != "reviewer_approved":
                head = None
            if error_id and repo and outcome.rejection_findings:
                write_error_fix_spec(
                    store,
                    tid,
                    error_id=error_id,
                    session_id=session_id,
                    repo=repo,
                    rejection_feedback=outcome.rejection_findings,
                )
            continue

        if outcome.kind in ("closed", "agent_closed"):
            continue

        return f"error-fix-work {tid} stop kind={outcome.kind}"

    return f"error-fix-work {tid} step-cap"


def _runner_to_completed(
    runner: Runner, argv: list[str], *, cwd: str | None = None
) -> Completed:
    """Run argv; honor cwd like main._exec_argv so checks use the worktree."""
    import subprocess

    try:
        if cwd is not None:
            proc = subprocess.run(  # noqa: S603
                argv, cwd=cwd, capture_output=True, text=True, check=False
            )
            return Completed(proc.returncode, proc.stdout or "", proc.stderr or "")
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
