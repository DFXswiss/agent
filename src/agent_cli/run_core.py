"""Shared spine-step executor for `agent run` and the error-fix fixer driver.

Performs ledger writes and lane launches. Delegates ledger mutations to
`main.cmd_*` helpers, which may print and raise SystemExit — callers must
catch SystemExit (and map RunOutcome) themselves.
OSError from a missing vendor CLI binary propagates (fixer catches it).
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .chain import NO_AUTO_CLOSE, Step, close_allowed, is_error_fix_originated, next_steps
from .git_act import _SHA_RE
from .lane import (
    LaneResult,
    count_findings,
    findings_header_present,
    has_single_terminal_report,
    launch,
    Runner as LaneRunner,
)
from .store import Store

DEFAULT_ROUND_CAP = 5
_REVIEW_ROLES = frozenset({"reviewer", "pr-reviewer-quality", "pr-reviewer-logic"})
_BASE_CANDIDATES = (
    "origin/develop",
    "origin/main",
    "origin/master",
    "develop",
    "main",
    "master",
)
_REVIEW_OUTPUT_CONTRACT = (
    "STATUS: complete | partial | timeout | unavailable\n"
    "REASON: [...]\n"
    "SCOPE: [...]\n"
    "DIMENSION: [...]\n"
    "FINDINGS: none\n"
    "NOT-VERIFIABLE: [...]\n"
    "GAPS: [...]"
)
ExecArgv = Callable[..., Any]


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


class EmptyReviewDiffError(Exception):
    """Raised by build_review_spec_file when the collected diff is empty."""


class ReviewDiffUnavailableError(Exception):
    """Raised when a git probe failed while collecting the review diff."""


# Checklist keys reset when a PR-reviewer dimension is rejected (new head).
_PR_REJECT_RESET_KEYS = (
    "implementer_done",
    "reviewer_approved",
    "local_check_pass",
    "pushed",
    "grok_pr_quality",
    "grok_pr_logic",
    "codex_pr_quality",
    "codex_pr_logic",
)

# Inner reviewer rejection reopens the implementer→reviewer cycle.
_REVIEWER_REJECT_RESET_KEYS = ("implementer_done", "reviewer_approved")


@dataclass
class RunOutcome:
    """Result of one spine-step attempt."""

    kind: str
    # idle | human_required | not_closable | dry_run | closed | agent_handoff |
    # agent_closed | rejected_new_round | failed | local_check_failed | vendor_unavailable
    key: str | None = None
    reason: str | None = None
    step: Step | None = None
    head_sha: str | None = None
    lane_result: LaneResult | None = None
    lane_results: list[LaneResult] = field(default_factory=list)
    close_evidence: str | None = None
    verdict: str | None = None  # approved|rejected|done|… when an agent finished
    message: str | None = None
    rejection_findings: str | None = None


def _checklist_set(tid: str, key: str, status: str, *, evidence: str | None = None) -> None:
    from . import main as main_mod

    args = [
        "set",
        "--task",
        tid,
        "--key",
        key,
        "--status",
        status,
        "--source",
        "script",
    ]
    if evidence is not None:
        args.extend(["--evidence", evidence])
    main_mod.cmd_checklist(args)


def _round_start(tid: str) -> None:
    from . import main as main_mod

    main_mod.cmd_round(["start", "--task", tid])


def _agent_start(
    *,
    session_id: str,
    tid: str,
    role: str,
    vendor: str,
    round_num: int | None,
) -> None:
    from . import main as main_mod

    args = [
        "start",
        "--session",
        session_id,
        "--task",
        tid,
        "--role",
        role,
        "--vendor",
        vendor,
    ]
    if round_num is not None:
        args.extend(["--round", str(round_num)])
    main_mod.cmd_agent(args)


def _agent_finish(agent_id: str, verdict: str, *, note: str | None = None) -> None:
    from . import main as main_mod

    args = ["finish", "--id", agent_id, "--verdict", verdict]
    if note is not None:
        args.extend(["--note", note])
    main_mod.cmd_agent(args)


def _gate_record(
    *,
    tid: str,
    stage: str,
    dimension: str,
    vendor: str,
    verdict: str,
    head: str,
    agent_id: str,
    evidence: str | None = None,
) -> None:
    from . import main as main_mod

    args = [
        "record",
        "--task",
        tid,
        "--stage",
        stage,
        "--dimension",
        dimension,
        "--vendor",
        vendor,
        "--verdict",
        verdict,
        "--head",
        head,
        "--agent",
        agent_id,
    ]
    if evidence is not None:
        args.extend(["--evidence", evidence])
    main_mod.cmd_gate(args)


def _check_record(
    *,
    tid: str,
    name: str,
    command: str,
    result: str,
    output: str,
    head: str | None = None,
) -> None:
    from . import main as main_mod

    args = [
        "record",
        "--task",
        tid,
        "--name",
        name,
        "--command",
        command,
        "--result",
        result,
        "--output",
        output,
    ]
    if head:
        args.extend(["--head", head])
    main_mod.cmd_check(args)


def _close_step(
    *,
    tid: str,
    key: str,
    evidence: str,
    head: str | None = None,
    source: str = "script",
) -> None:
    from . import main as main_mod

    args = [
        "--task",
        tid,
        "--key",
        key,
        "--source",
        source,
        "--evidence",
        evidence,
    ]
    if head:
        args.extend(["--head", head])
    main_mod.cmd_close_step(args)


def _interpret_lane(
    role: str, result: LaneResult
) -> tuple[str, str | None]:
    """Return (decision, findings_text) from one LaneResult.

    decision: pass | fail | retry
    findings_text: non-None when fail (for gate evidence).
    Pass/fail/retry are derived from the same parsed result (no dual compute).
    """
    if role == "implementer":
        if result.status == "complete":
            return "pass", None
        return "retry", None
    # reviewer / pr-reviewer-*
    if result.status != "complete":
        return "retry", None
    stdout = result.stdout or ""
    # No FINDINGS: header → unparseable (retry), not an automatic pass.
    if not findings_header_present(stdout):
        return "retry", None
    # Multiple STATUS:/FINDINGS: blocks (e.g. quoted example + real report)
    # must not be parsed as a false pass via last-STATUS / first-FINDINGS.
    if not has_single_terminal_report(stdout):
        return "retry", None
    n = count_findings(stdout)
    if n == 0:
        return "pass", None
    return "fail", stdout.strip() or "findings"


def _collect_review_diff(
    cwd: str, exec_argv: ExecArgv
) -> tuple[str, list[str], bool]:
    """Materialize unified diff + changed paths against a base branch.

    The third return value is True only when every diff-producing git probe
    that actually ran exited 0. The base-ref rev-parse search is excluded —
    a missing candidate ref is expected control flow, not a probe failure.
    When *no* candidate resolves at all, that counts as a probe failure
    (not expected control flow), so probes_ok becomes False.
    """
    base_ref: str | None = None
    for candidate in _BASE_CANDIDATES:
        completed = exec_argv(["git", "rev-parse", "--verify", candidate], cwd=cwd)
        if int(getattr(completed, "returncode", 1)) == 0:
            base_ref = candidate
            break
    chunks: list[str] = []
    paths: list[str] = []
    probes_ok = True
    if base_ref is None:
        probes_ok = False
    if base_ref is not None:
        mb = exec_argv(["git", "merge-base", "HEAD", base_ref], cwd=cwd)
        mb_rc = int(getattr(mb, "returncode", 1))
        base_sha = str(getattr(mb, "stdout", "") or "").strip()
        if mb_rc != 0 or not base_sha:
            probes_ok = False
        if mb_rc == 0 and base_sha:
            range_spec = f"{base_sha}...HEAD"
            diff = exec_argv(["git", "diff", range_spec], cwd=cwd)
            if int(getattr(diff, "returncode", 1)) != 0:
                probes_ok = False
            else:
                text = str(getattr(diff, "stdout", "") or "")
                if text.strip():
                    chunks.append(text)
            names = exec_argv(["git", "diff", "--name-only", range_spec], cwd=cwd)
            if int(getattr(names, "returncode", 1)) != 0:
                probes_ok = False
            else:
                paths.extend(
                    p.strip()
                    for p in str(getattr(names, "stdout", "") or "").splitlines()
                    if p.strip()
                )
    for argv_extra in (["HEAD"], ["--cached"]):
        diff = exec_argv(["git", "diff", *argv_extra], cwd=cwd)
        if int(getattr(diff, "returncode", 1)) != 0:
            probes_ok = False
        else:
            text = str(getattr(diff, "stdout", "") or "")
            if text.strip():
                chunks.append(text)
        names = exec_argv(["git", "diff", "--name-only", *argv_extra], cwd=cwd)
        if int(getattr(names, "returncode", 1)) != 0:
            probes_ok = False
        else:
            paths.extend(
                p.strip()
                for p in str(getattr(names, "stdout", "") or "").splitlines()
                if p.strip()
            )
    # Preserve order, drop dupes.
    seen: set[str] = set()
    unique_paths: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)
    return "\n".join(chunks), unique_paths, probes_ok


def build_review_spec_file(
    store: Store,
    tid: str,
    *,
    role: str,
    round_num: int | None,
    implement_spec_file: str | None,
    cwd: str,
    exec_argv: ExecArgv,
) -> str:
    """Write a four-part review prompt under $AGENT_HOME/review-work/<task_id>/; return its path."""
    diff_text, changed_paths, probes_ok = _collect_review_diff(cwd, exec_argv)
    if not probes_ok:
        raise ReviewDiffUnavailableError(
            "git probe failed while collecting the review diff"
        )
    if not diff_text.strip():
        raise EmptyReviewDiffError("empty review diff")
    parent = Path(store.home) / "review-work" / tid
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    round_bit = round_num if round_num is not None else 0
    diff_path = parent / f"review-{role}-round{round_bit}.diff"
    spec_path = parent / f"review-{role}-round{round_bit}.md"
    diff_path.write_text(diff_text, encoding="utf-8")
    abs_diff = str(diff_path.resolve())
    paths_line = ", ".join(changed_paths) if changed_paths else "(none)"

    if role == "reviewer":
        dimension = "does this diff fulfill the spec below?"
        impl_body = ""
        if implement_spec_file:
            try:
                impl_body = Path(implement_spec_file).read_text(encoding="utf-8")
            except OSError:
                impl_body = ""
        context = (
            "Original implementer spec (what was asked for):\n\n"
            f"{impl_body.strip() or '(implementer spec unavailable)'}\n"
        )
    elif role == "pr-reviewer-quality":
        dimension = "conformance/quality only (not logic/correctness)"
        context = (
            "Read CONTRIBUTING.md in the repository first. "
            "Judge conformance and quality against that file and repo conventions only.\n"
        )
    else:
        dimension = "logic/correctness only (not conformance/quality)"
        context = (
            "Read CONTRIBUTING.md in the repository first for project context, "
            "then judge logic and correctness of the diff only.\n"
        )

    fence = _fence_marker(diff_text)
    body = (
        f"# Scope\n\n"
        f"Read the unified diff via the Read tool from this absolute path:\n"
        f"`{abs_diff}`\n\n"
        f"Changed paths: {paths_line}\n\n"
        f"Unified diff (also embedded for convenience; the Read path is required):\n\n"
        f"{fence}diff\n{diff_text}\n{fence}\n\n"
        f"# Dimension\n\n"
        f"{dimension}\n\n"
        f"# Context\n\n"
        f"{context}\n"
        f"# Output contract\n\n"
        f"End with exactly one terminal report in this shape (verbatim headers):\n\n"
        f"```\n{_REVIEW_OUTPUT_CONTRACT}\n```\n\n"
        f"`FINDINGS: 0` (or `none`) is a valid, expected result when "
        f"`STATUS: complete` and nothing is wrong.\n\n"
        f"Do not execute software — no tests, builds, package managers, shells, "
        f"or project scripts. Read/Grep/Glob only. Cite every finding with "
        f"`file:line`. If a judgment needs a test run, put the "
        f"command under NOT-VERIFIABLE instead of running it.\n"
    )
    spec_path.write_text(body, encoding="utf-8")
    return str(spec_path)


def _reset_keys(store: Store, tid: str, keys: tuple[str, ...], *, evidence: str) -> None:
    checklist = {
        str(r["key"]): str(r["status"])
        for r in store.rows("checklist_item")
        if r.get("task_id") == tid
    }
    for key in keys:
        if checklist.get(key) == "ja":
            _checklist_set(tid, key, "nein", evidence=evidence)


def _resolve_gate_head(
    store: Store,
    tid: str,
    head: str | None,
    *,
    cwd: str | None = None,
    exec_argv: ExecArgv | None = None,
) -> str:
    """Resolve a git SHA for gate record: explicit head, pushed evidence, or HEAD."""
    if head and _SHA_RE.fullmatch(head.lower()):
        return head.lower()
    from . import main as main_mod

    snap = main_mod._chain_snapshot(store, tid, extra_head=head)
    snap_head = str(snap.get("head_sha") or "").strip().lower()
    if snap_head and _SHA_RE.fullmatch(snap_head):
        return snap_head
    for row in store.rows("checklist_item"):
        if row.get("task_id") != tid or row.get("key") != "pushed":
            continue
        if row.get("status") != "ja":
            continue
        ev = str(row.get("evidence") or "").strip().lower()
        # evidence may be the bare sha or "run auto:<sha>" / "pushed <sha>"
        for token in ev.replace(":", " ").split():
            if _SHA_RE.fullmatch(token):
                return token
    if cwd and exec_argv is not None:
        completed = exec_argv(["git", "rev-parse", "HEAD"], cwd=cwd)
        sha = str(getattr(completed, "stdout", "") or "").strip().lower()
        if completed.returncode == 0 and _SHA_RE.fullmatch(sha):
            return sha
    return ""


def _apply_rejection_resets(
    store: Store,
    tid: str,
    role: str,
    *,
    round_cap: int | None,
    evidence: str,
) -> RunOutcome:
    """Reset checklist keys then round-start, or fail on cap (no reset)."""
    task = store.row("task", tid)
    current = int((task or {}).get("current_round") or 0)
    if round_cap is not None and current >= round_cap:
        cap_msg = f"round cap {round_cap} reached (current_round={current})"
        # Persist reason in the ledger (check fail also sets task state failed).
        _check_record(
            tid=tid,
            name="round-cap",
            command=f"round_cap={round_cap}",
            result="fail",
            output=cap_msg,
        )
        return RunOutcome(
            kind="failed",
            reason=cap_msg,
            message=cap_msg,
        )
    if role == "reviewer":
        _reset_keys(store, tid, _REVIEWER_REJECT_RESET_KEYS, evidence=evidence)
    else:
        _reset_keys(store, tid, _PR_REJECT_RESET_KEYS, evidence=evidence)
    _round_start(tid)
    return RunOutcome(
        kind="rejected_new_round",
        key="reviewer_approved" if role == "reviewer" else None,
        reason=f"{role} rejected",
        verdict="rejected",
        message=f"{role} rejected; new round started",
    )


def _finish_agent_pass(
    store: Store,
    tid: str,
    *,
    role: str,
    vendor: str,
    round_num: int | None,
    head: str | None,
    result: LaneResult,
    step: Step,
    cwd: str | None = None,
    exec_argv: ExecArgv | None = None,
) -> RunOutcome:
    from . import main as main_mod

    working = main_mod._find_working_agent(
        store, tid, role=role, vendor=vendor, round_num=round_num
    )
    if working is None:
        return RunOutcome(
            kind="failed",
            key=step.key,
            reason="working agent not found after lane",
            lane_result=result,
            message="working agent not found after lane",
        )
    agent_id = str(working["id"])
    if role == "implementer":
        _agent_finish(agent_id, "done", note="lane STATUS=complete")
        verd = "done"
    else:
        _agent_finish(agent_id, "approved", note="lane STATUS=complete findings=0")
        verd = "approved"
        if role in ("pr-reviewer-quality", "pr-reviewer-logic"):
            dim = "quality" if role.endswith("quality") else "logic"
            stage = "grok-pr" if vendor == "grok" else "codex-pr"
            gate_head = _resolve_gate_head(
                store, tid, head, cwd=cwd, exec_argv=exec_argv
            )
            if not gate_head:
                return RunOutcome(
                    kind="failed",
                    key=step.key,
                    reason="head_sha missing for gate record",
                    lane_result=result,
                    message="head_sha missing for gate record",
                )
            _gate_record(
                tid=tid,
                stage=stage,
                dimension=dim,
                vendor=vendor,
                verdict="approved",
                head=gate_head,
                agent_id=agent_id,
            )
            head = gate_head
    snap = main_mod._chain_snapshot(store, tid, extra_head=head)
    wf = str(snap["workflow"])
    verdict = close_allowed(
        wf,
        step.key,
        checklist=snap["checklist"],
        source="script",
        evidence="run auto",
        snapshot=snap,
    )
    if not verdict.allowed:
        return RunOutcome(
            kind="not_closable",
            key=step.key,
            reason=verdict.reason,
            step=step,
            lane_result=result,
            head_sha=head,
            message=verdict.reason,
        )
    evidence = f"run auto:{verdict.reason}"
    _close_step(tid=tid, key=step.key, evidence=evidence, head=head)
    return RunOutcome(
        kind="agent_closed",
        key=step.key,
        step=step,
        lane_result=result,
        head_sha=head,
        close_evidence=evidence,
        verdict=verd,
    )


def _finish_agent_fail(
    store: Store,
    tid: str,
    *,
    role: str,
    vendor: str,
    round_num: int | None,
    head: str | None,
    result: LaneResult,
    step: Step,
    findings_text: str,
    round_cap: int | None,
    cwd: str | None = None,
    exec_argv: ExecArgv | None = None,
) -> RunOutcome:
    from . import main as main_mod

    working = main_mod._find_working_agent(
        store, tid, role=role, vendor=vendor, round_num=round_num
    )
    if working is None:
        return RunOutcome(
            kind="failed",
            key=step.key,
            reason="working agent not found after lane",
            lane_result=result,
            message="working agent not found after lane",
        )
    agent_id = str(working["id"])
    evidence = findings_text[:8000] or "findings"
    _agent_finish(agent_id, "rejected", note="lane findings")
    if role in ("pr-reviewer-quality", "pr-reviewer-logic"):
        dim = "quality" if role.endswith("quality") else "logic"
        stage = "grok-pr" if vendor == "grok" else "codex-pr"
        gate_head = _resolve_gate_head(
            store, tid, head, cwd=cwd, exec_argv=exec_argv
        )
        if not gate_head:
            return RunOutcome(
                kind="failed",
                key=step.key,
                reason="head_sha missing for gate record",
                lane_result=result,
                message="head_sha missing for gate record",
            )
        _gate_record(
            tid=tid,
            stage=stage,
            dimension=dim,
            vendor=vendor,
            verdict="rejected",
            head=gate_head,
            agent_id=agent_id,
            evidence=evidence,
        )
    out = _apply_rejection_resets(
        store, tid, role, round_cap=round_cap, evidence=evidence
    )
    out.lane_result = result
    out.key = step.key
    if out.kind == "rejected_new_round":
        out.rejection_findings = evidence
    return out


def _lane_retry_then_fail(
    store: Store,
    tid: str,
    *,
    role: str,
    vendor: str,
    round_num: int | None,
    head: str | None,
    step: Step,
    spec_file: str,
    cwd: str,
    tmux: bool,
    runner: LaneRunner | None,
    first: LaneResult,
    round_cap: int | None,
    exec_argv: ExecArgv | None = None,
) -> RunOutcome:
    """Re-invoke launch once; on second unparseable/non-pass, fail the task."""
    try:
        second = launch(
            role=role,
            vendor=vendor,
            spec_file=spec_file,
            cwd=cwd,
            runner=runner,
            tmux=tmux,
        )
    except OSError:
        from . import main as main_mod

        working = main_mod._find_working_agent(
            store, tid, role=role, vendor=vendor, round_num=round_num
        )
        if working is not None:
            _agent_finish(
                str(working["id"]),
                "unavailable",
                note=f"launch failed ({role} {vendor})",
            )
        raise
    decision2, findings2 = _interpret_lane(role, second)
    if decision2 == "pass":
        return _finish_agent_pass(
            store,
            tid,
            role=role,
            vendor=vendor,
            round_num=round_num,
            head=head,
            result=second,
            step=step,
            cwd=cwd,
            exec_argv=exec_argv,
        )
    if decision2 == "fail" and findings2 is not None:
        out = _finish_agent_fail(
            store,
            tid,
            role=role,
            vendor=vendor,
            round_num=round_num,
            head=head,
            result=second,
            step=step,
            findings_text=findings2,
            round_cap=round_cap,
            cwd=cwd,
            exec_argv=exec_argv,
        )
        out.lane_results = [first, second]
        return out
    # A missing/misconfigured vendor CLI surfaces as LaneResult(status="unavailable"),
    # not OSError (env execs fine, only the target binary fails) — an external, fixable
    # problem. Leave the task untouched for retry; only genuinely unparseable/ambiguous
    # output (status != "unavailable") still fails the task per the mechanical rule below.
    if second.status == "unavailable":
        # Release the working agent without a task-state transition so a later
        # scan / manual round-start is not blocked by a stuck "working" row.
        from . import main as main_mod

        working = main_mod._find_working_agent(
            store, tid, role=role, vendor=vendor, round_num=round_num
        )
        if working is not None:
            _agent_finish(
                str(working["id"]),
                "unavailable",
                note=f"vendor CLI unavailable ({vendor} {role})",
            )
        return RunOutcome(
            kind="vendor_unavailable",
            key=step.key,
            reason=f"vendor CLI unavailable ({vendor} {role})",
            lane_result=second,
            lane_results=[first, second],
            message=f"vendor CLI unavailable ({vendor} {role})",
        )

    # Still genuinely unparseable / ambiguous → fail task. Also release the
    # still-working agent so `agent round start` isn't blocked for a later
    # manual recovery attempt.
    from . import main as main_mod

    working = main_mod._find_working_agent(
        store, tid, role=role, vendor=vendor, round_num=round_num
    )
    if working is not None:
        verdict = "blocked" if role == "implementer" else "rejected"
        _agent_finish(
            str(working["id"]),
            verdict,
            note=f"lane retry exhausted (status={second.status})",
        )

    combined = (
        f"--- attempt 1 STATUS={first.status} ---\n{(first.stdout or '')}\n"
        f"--- attempt 2 STATUS={second.status} ---\n{(second.stdout or '')}"
    )[:8000]
    _check_record(
        tid=tid,
        name=f"{role}-{vendor}",
        command=f"lane {role} {vendor}",
        result="fail",
        output=combined or "(no output)",
    )
    # check record with fail already sets task state failed
    return RunOutcome(
        kind="failed",
        key=step.key,
        reason="lane retry exhausted",
        lane_result=second,
        lane_results=[first, second],
        message="lane retry exhausted",
    )


def execute_spine_step(
    store: Store,
    tid: str,
    *,
    head: str | None = None,
    dry_run: bool = False,
    spec_file: str | None = None,
    cwd: str | None = None,
    tmux: bool = True,
    runner: LaneRunner | None = None,
    round_cap: int | None = None,
    exec_argv: ExecArgv | None = None,
) -> RunOutcome:
    """Execute the single open spine step for tid.

    `round_cap=None` means unbounded (interactive `agent run`). The fixer
    passes an explicit int (DEFAULT_ROUND_CAP).
    """
    from . import main as main_mod

    if exec_argv is None:
        exec_argv = main_mod._exec_argv

    task = store.row("task", tid)
    if task is None:
        return RunOutcome(kind="failed", reason=f"unknown task: {tid}")

    snap = main_mod._chain_snapshot(store, tid, extra_head=head)
    wf = str(snap["workflow"])
    ready = next_steps(wf, snap["checklist"], spine_only=True)
    if not ready:
        return RunOutcome(kind="idle")

    step = ready[0]
    if dry_run:
        return RunOutcome(
            kind="dry_run",
            key=step.key,
            step=step,
            head_sha=head,
        )

    if step.kind == "human":
        # error-fix carve-out for spec_written is handled by the fixer before
        # calling this function; interactive run still treats it as human.
        return RunOutcome(
            kind="human_required",
            key=step.key,
            step=step,
            reason=f"human must close {step.key}",
        )

    if step.key == "pushed":
        run_cwd = cwd or os.getcwd()
        from .git_act import GitActError, push_branch

        from .error_fix_act import _nonempty_str, _repo_ok

        payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
        raw_error_id = payload.get("error_id")
        error_id = _nonempty_str(raw_error_id) or ""
        if not error_id and isinstance(raw_error_id, str) and raw_error_id != "":
            # Present but strips to empty (e.g. stale pre-round-24 store row
            # with a whitespace-only error_id — creation-time validation now
            # rejects this for new tasks). Fail loudly instead of silently
            # downgrading to expected_branch=None, which would skip the push
            # identity check entirely as if error_id were absent.
            return RunOutcome(
                kind="failed",
                key=step.key,
                step=step,
                reason="task payload.error_id is whitespace-only",
                message="task payload.error_id is whitespace-only",
            )
        if error_id and not is_error_fix_originated(snap):
            # payload.error_id alone is not enough — same gate as chain.py's
            # script carve-outs. Falling through to expected_branch=None would
            # skip the push identity check as if this were an ordinary task.
            return RunOutcome(
                kind="failed",
                key=step.key,
                step=step,
                reason="task payload.error_id is set but error_fix_confirmed is False",
                message="task payload.error_id is set but error_fix_confirmed is False",
            )
        # Payload wins when both are valid but differ: _drive_one (which
        # creates the PR) resolves repo the same way. A genuine
        # two-valid-but-different-values divergence is not fail-closed here.
        resolved_repo = _repo_ok(payload.get("repo") or task.get("repo"))
        if error_id and resolved_repo is None:
            # Missing/malformed/stale payload.repo and task.repo would pass
            # expected_repo=None and skip the push-destination allowlist
            # check entirely while still pushing under the
            # expected_branch-only identity check.
            return RunOutcome(
                kind="failed",
                key=step.key,
                step=step,
                reason="task repo could not be resolved for the push-destination check",
                message="task repo could not be resolved for the push-destination check",
            )
        # error_id non-empty here implies is_error_fix_originated (gated above).
        expected_branch = f"error-fix-{error_id[:8]}" if error_id else None
        try:
            sha = push_branch(
                cwd=run_cwd,
                runner=lambda argv: exec_argv(argv, cwd=run_cwd),
                expected_branch=expected_branch,
                expected_repo=resolved_repo,
            )
        except GitActError as exc:
            return RunOutcome(
                kind="failed",
                key=step.key,
                step=step,
                reason=str(exc),
                message=str(exc),
            )
        if head is not None:
            want = head.lower()
            if want != sha and not (
                7 <= len(want) < len(sha) and sha.startswith(want)
            ):
                return RunOutcome(
                    kind="failed",
                    key=step.key,
                    step=step,
                    reason=f"--head {head} does not match pushed sha {sha}",
                    message=f"--head {head} does not match pushed sha {sha}",
                )
        head = sha
        snap = main_mod._chain_snapshot(store, tid, extra_head=head)

    evidence: str | None = None
    if step.key == "pushed":
        evidence = f"pushed {head}"
    if step.key == "mergeable":
        run_cwd = cwd or os.getcwd()
        from .git_act import GitActError, measure_mergeable

        try:
            expected = str(snap.get("head_sha") or head or "").strip() or None
            evidence = measure_mergeable(
                cwd=run_cwd,
                runner=lambda argv: exec_argv(argv, cwd=run_cwd),
                expected_head=expected,
            )
        except GitActError as exc:
            return RunOutcome(
                kind="failed",
                key=step.key,
                step=step,
                reason=str(exc),
                message=str(exc),
            )

    if step.key in NO_AUTO_CLOSE:
        return RunOutcome(
            kind="not_closable",
            key=step.key,
            step=step,
            reason=f"{step.key} is not auto-closable",
            head_sha=head,
        )

    if step.key == "local_check_pass":
        run_cwd = cwd or os.getcwd()
        # Bind validity to the worktree HEAD (not merely "any prior pass row").
        check_head = ""
        completed_head = exec_argv(["git", "rev-parse", "HEAD"], cwd=run_cwd)
        sha = str(getattr(completed_head, "stdout", "") or "").strip().lower()
        if int(getattr(completed_head, "returncode", 1)) == 0 and _SHA_RE.fullmatch(
            sha
        ):
            check_head = sha
        if not check_head:
            check_head = _resolve_gate_head(
                store, tid, head, cwd=run_cwd, exec_argv=exec_argv
            )
        has_fresh = False
        if check_head:
            latest_local: dict | None = None
            for c in snap.get("local_checks") or []:
                if not isinstance(c, dict):
                    continue
                if str(c.get("name") or "") != "local":
                    continue
                row_head = str(c.get("head_sha") or "").strip().lower()
                if row_head != check_head:
                    continue
                latest_local = c  # oldest→newest; last one wins
            if latest_local is not None and str(latest_local.get("result") or "") in (
                "pass",
                "skip",
            ):
                has_fresh = True
        if not has_fresh:
            env_cmd = os.environ.get("AGENT_CHECK_COMMAND")
            if env_cmd is None:
                command = "pytest -q"
            elif env_cmd == "":
                return RunOutcome(
                    kind="failed",
                    key=step.key,
                    step=step,
                    reason="AGENT_CHECK_COMMAND is set but empty",
                    message="AGENT_CHECK_COMMAND is set but empty",
                )
            else:
                command = env_cmd
            argv = shlex.split(command)
            if not argv:
                return RunOutcome(
                    kind="failed",
                    key=step.key,
                    step=step,
                    reason="check command is empty",
                    message="check command is empty",
                )
            completed = exec_argv(argv, cwd=run_cwd)
            result = "pass" if completed.returncode == 0 else "fail"
            output = ((completed.stdout or "") + (completed.stderr or ""))[:8000]
            _check_record(
                tid=tid,
                name="local",
                command=command,
                result=result,
                output=output or "(no output)",
                head=check_head or None,
            )
            if result == "fail":
                return RunOutcome(
                    kind="local_check_failed",
                    key=step.key,
                    step=step,
                    message="local_check fail",
                )
            if check_head:
                head = check_head
            snap = main_mod._chain_snapshot(store, tid, extra_head=head)

    if step.kind == "agent":
        already = close_allowed(
            wf,
            step.key,
            checklist=snap["checklist"],
            source="script",
            evidence="run auto",
            snapshot=snap,
        )
        if already.allowed:
            close_evidence = f"run auto:{already.reason}"
            _close_step(tid=tid, key=step.key, evidence=close_evidence, head=head)
            return RunOutcome(
                kind="closed",
                key=step.key,
                step=step,
                head_sha=head,
                close_evidence=close_evidence,
            )
        if spec_file is None:
            return RunOutcome(
                kind="agent_handoff",
                key=step.key,
                step=step,
                head_sha=head,
                reason="agent step needs --spec-file or finished artifact",
            )
        spec_path = Path(spec_file)
        if not spec_path.is_file():
            return RunOutcome(
                kind="failed",
                key=step.key,
                step=step,
                reason=f"spec-file not found: {spec_file}",
                message=f"spec-file not found: {spec_file}",
            )
        if not spec_path.read_text(encoding="utf-8").strip():
            return RunOutcome(
                kind="failed",
                key=step.key,
                step=step,
                reason=f"spec-file is empty: {spec_file}",
                message=f"spec-file is empty: {spec_file}",
            )
        run_cwd = cwd or os.getcwd()
        role = str(step.role or "")
        vendor = str(step.vendor or "")
        session_id = str(snap.get("session_id") or "")
        # Re-read task: round may have changed
        task = store.row("task", tid) or task
        current_round = int(task.get("current_round") or 0)
        round_num: int | None = None
        if role in ("implementer", "reviewer"):
            round_num = current_round
        working = main_mod._find_working_agent(
            store, tid, role=role, vendor=vendor, round_num=round_num
        )
        if working is None:
            _agent_start(
                session_id=session_id,
                tid=tid,
                role=role,
                vendor=vendor,
                round_num=round_num,
            )
        launch_spec = spec_file
        if role in _REVIEW_ROLES:
            try:
                launch_spec = build_review_spec_file(
                    store,
                    tid,
                    role=role,
                    round_num=round_num,
                    implement_spec_file=spec_file,
                    cwd=run_cwd,
                    exec_argv=exec_argv,
                )
            except EmptyReviewDiffError as exc:
                working = main_mod._find_working_agent(
                    store, tid, role=role, vendor=vendor, round_num=round_num
                )
                if working is not None:
                    _agent_finish(str(working["id"]), "unavailable", note=str(exc))
                _check_record(
                    tid=tid,
                    name="empty-review-diff",
                    command=f"role={role} vendor={vendor}",
                    result="fail",
                    output=str(exc),
                )
                return RunOutcome(
                    kind="failed",
                    key=step.key,
                    step=step,
                    reason=str(exc),
                    message=str(exc),
                )
            except ReviewDiffUnavailableError as exc:
                # External/transient git failure — leave task untouched for retry
                # (same shape as vendor_unavailable in _lane_retry_then_fail).
                working = main_mod._find_working_agent(
                    store, tid, role=role, vendor=vendor, round_num=round_num
                )
                if working is not None:
                    _agent_finish(str(working["id"]), "unavailable", note=str(exc))
                return RunOutcome(
                    kind="vendor_unavailable",
                    key=step.key,
                    reason=str(exc),
                    message=str(exc),
                )
            except OSError:
                working = main_mod._find_working_agent(
                    store, tid, role=role, vendor=vendor, round_num=round_num
                )
                if working is not None:
                    _agent_finish(
                        str(working["id"]),
                        "unavailable",
                        note=f"review-spec write failed ({role} {vendor})",
                    )
                raise
        # OSError propagates to caller (fixer catches; cmd_run surfaces).
        try:
            result = launch(
                role=role,
                vendor=vendor,
                spec_file=launch_spec,
                cwd=run_cwd,
                runner=runner,
                tmux=tmux,
            )
        except OSError:
            working = main_mod._find_working_agent(
                store, tid, role=role, vendor=vendor, round_num=round_num
            )
            if working is not None:
                _agent_finish(
                    str(working["id"]),
                    "unavailable",
                    note=f"launch failed ({role} {vendor})",
                )
            raise

        decision, findings_text = _interpret_lane(role, result)
        if decision == "pass":
            out = _finish_agent_pass(
                store,
                tid,
                role=role,
                vendor=vendor,
                round_num=round_num,
                head=head,
                result=result,
                step=step,
                cwd=run_cwd,
                exec_argv=exec_argv,
            )
            out.lane_results = [result]
            return out
        if decision == "fail" and findings_text is not None:
            out = _finish_agent_fail(
                store,
                tid,
                role=role,
                vendor=vendor,
                round_num=round_num,
                head=head,
                result=result,
                step=step,
                findings_text=findings_text,
                round_cap=round_cap,
                cwd=run_cwd,
                exec_argv=exec_argv,
            )
            out.lane_results = [result]
            return out
        # retry once
        return _lane_retry_then_fail(
            store,
            tid,
            role=role,
            vendor=vendor,
            round_num=round_num,
            head=head,
            step=step,
            spec_file=launch_spec,
            cwd=run_cwd,
            tmux=tmux,
            runner=runner,
            first=result,
            round_cap=round_cap,
            exec_argv=exec_argv,
        )

    # Script step: close if allowed
    close_ev = evidence if step.key in ("mergeable", "pushed") else "run auto"
    verdict = close_allowed(
        wf,
        step.key,
        checklist=snap["checklist"],
        source="script",
        evidence=close_ev,
        snapshot=snap,
    )
    if not verdict.allowed:
        return RunOutcome(
            kind="not_closable",
            key=step.key,
            step=step,
            reason=verdict.reason,
            head_sha=head,
            message=verdict.reason,
        )
    close_evidence = (
        evidence if step.key in ("mergeable", "pushed") else f"run auto:{verdict.reason}"
    )
    _close_step(tid=tid, key=step.key, evidence=str(close_evidence), head=head)
    return RunOutcome(
        kind="closed",
        key=step.key,
        step=step,
        head_sha=head,
        close_evidence=close_evidence,
    )
