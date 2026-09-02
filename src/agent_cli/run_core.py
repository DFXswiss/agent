"""Shared spine-step executor for `agent run` and the error-fix fixer driver.

Performs ledger writes and lane launches. Does not print, die, or raise
SystemExit — callers map RunOutcome to CLI text / exit codes.
OSError from a missing vendor CLI binary propagates (fixer catches it).
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .chain import NO_AUTO_CLOSE, Step, close_allowed, next_steps
from .lane import LaneResult, count_findings, findings_header_present, launch

DEFAULT_ROUND_CAP = 5
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

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
) -> None:
    from . import main as main_mod

    main_mod.cmd_check(
        [
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
    )


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
    n = count_findings(stdout)
    if n == 0:
        return "pass", None
    return "fail", stdout.strip() or "findings"


def _reset_keys(store: Any, tid: str, keys: tuple[str, ...], *, evidence: str) -> None:
    checklist = {
        str(r["key"]): str(r["status"])
        for r in store.rows("checklist_item")
        if r.get("task_id") == tid
    }
    for key in keys:
        if checklist.get(key) == "ja":
            _checklist_set(tid, key, "nein", evidence=evidence)


def _resolve_gate_head(
    store: Any,
    tid: str,
    head: str | None,
    *,
    cwd: str | None = None,
    exec_argv: Callable[..., Any] | None = None,
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
    store: Any,
    tid: str,
    role: str,
    *,
    round_cap: int,
    evidence: str,
) -> RunOutcome:
    """Reset checklist keys then round-start, or fail on cap (no reset)."""
    task = store.row("task", tid)
    current = int((task or {}).get("current_round") or 0)
    if current >= round_cap:
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
    store: Any,
    tid: str,
    *,
    role: str,
    vendor: str,
    round_num: int | None,
    head: str | None,
    result: LaneResult,
    step: Step,
    cwd: str | None = None,
    exec_argv: Callable[..., Any] | None = None,
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
    store: Any,
    tid: str,
    *,
    role: str,
    vendor: str,
    round_num: int | None,
    head: str | None,
    result: LaneResult,
    step: Step,
    findings_text: str,
    round_cap: int,
    cwd: str | None = None,
    exec_argv: Callable[..., Any] | None = None,
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
    return out


def _lane_retry_then_fail(
    store: Any,
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
    runner: Any,
    first: LaneResult,
    round_cap: int,
    exec_argv: Callable[..., Any] | None = None,
) -> RunOutcome:
    """Re-invoke launch once; on second unparseable/non-pass, fail the task."""
    second = launch(
        role=role,
        vendor=vendor,
        spec_file=spec_file,
        cwd=cwd,
        runner=runner,
        tmux=tmux,
    )
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
    store: Any,
    tid: str,
    *,
    head: str | None = None,
    dry_run: bool = False,
    spec_file: str | None = None,
    cwd: str | None = None,
    tmux: bool = True,
    runner: Any = None,
    round_cap: int = DEFAULT_ROUND_CAP,
    exec_argv: Callable[..., Any] | None = None,
) -> RunOutcome:
    """Execute the single open spine step for tid. No print/die/SystemExit."""
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

        try:
            sha = push_branch(
                cwd=run_cwd,
                runner=lambda argv: exec_argv(argv, cwd=run_cwd),
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

    if step.key == "local_check_pass" and not snap["local_checks"]:
        run_cwd = cwd or os.getcwd()
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
        )
        if result == "fail":
            return RunOutcome(
                kind="local_check_failed",
                key=step.key,
                step=step,
                message="local_check fail",
            )
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
        # OSError propagates to caller (fixer catches; cmd_run surfaces).
        result = launch(
            role=role,
            vendor=vendor,
            spec_file=spec_file,
            cwd=run_cwd,
            runner=runner,
            tmux=tmux,
        )
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
            spec_file=spec_file,
            cwd=run_cwd,
            tmux=tmux,
            runner=runner,
            first=result,
            round_cap=round_cap,
            exec_argv=exec_argv,
        )

    # Script step: close if allowed
    close_ev = evidence if step.key == "mergeable" else "run auto"
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
        evidence if step.key == "mergeable" else f"run auto:{verdict.reason}"
    )
    _close_step(tid=tid, key=step.key, evidence=str(close_evidence), head=head)
    return RunOutcome(
        kind="closed",
        key=step.key,
        step=step,
        head_sha=head,
        close_evidence=close_evidence,
    )
