"""Static step chain for the session-store spine.

No Postgres, no network, no subprocess. The graph is the process.
The store only holds state. next_steps / close_allowed decide which
single step is open now and who may close it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .allow import CHECKLIST_KEYS, N_A_ALLOWED

KINDS = ("script", "agent", "human")
# Who may set ja on these keys (source).
HUMAN_KEYS = frozenset(
    {
        "spec_written",
        "deviation_declared",
        "deviation_granted",
    }
)
# All other required keys: only source=script (close-step).


@dataclass(frozen=True)
class Step:
    key: str
    needs: tuple[str, ...]
    kind: str  # script | agent | human
    role: str | None = None
    vendor: str | None = None
    spine: bool = True


def _step(
    key: str,
    needs: tuple[str, ...],
    kind: str,
    *,
    role: str | None = None,
    vendor: str | None = None,
    spine: bool = True,
) -> Step:
    if kind not in KINDS:
        raise ValueError(f"kind {kind}")
    return Step(key=key, needs=needs, kind=kind, role=role, vendor=vendor, spine=spine)


# One source. Order = preferred walk; parallel steps share needs and sit adjacent.
CHAINS: dict[str, tuple[Step, ...]] = {
    "implement": (
        _step("session_registered", (), "script"),
        _step("spec_written", ("session_registered",), "human"),
        _step(
            "implementer_done",
            ("spec_written",),
            "agent",
            role="implementer",
            vendor="grok",
        ),
        _step(
            "reviewer_approved",
            ("implementer_done",),
            "agent",
            role="reviewer",
            vendor="grok",
        ),
        _step("local_check_pass", ("reviewer_approved",), "script"),
        _step("pushed", ("local_check_pass",), "script"),
        _step(
            "grok_pr_quality",
            ("pushed",),
            "agent",
            role="pr-reviewer-quality",
            vendor="grok",
        ),
        _step(
            "grok_pr_logic",
            ("pushed",),
            "agent",
            role="pr-reviewer-logic",
            vendor="grok",
        ),
        _step(
            "codex_pr_quality",
            ("grok_pr_quality", "grok_pr_logic"),
            "agent",
            role="pr-reviewer-quality",
            vendor="codex",
        ),
        _step(
            "codex_pr_logic",
            ("grok_pr_quality", "grok_pr_logic"),
            "agent",
            role="pr-reviewer-logic",
            vendor="codex",
        ),
        _step(
            "contributing_ok",
            ("codex_pr_quality", "codex_pr_logic"),
            "script",
        ),
        _step("deviation_declared", (), "human", spine=False),
        _step("deviation_granted", ("deviation_declared",), "human", spine=False),
    ),
    "review": (
        _step("session_registered", (), "script"),
        _step("contributing_read", ("session_registered",), "human"),
        _step(
            "grok_pr_quality",
            ("contributing_read",),
            "agent",
            role="pr-reviewer-quality",
            vendor="grok",
        ),
        _step(
            "grok_pr_logic",
            ("contributing_read",),
            "agent",
            role="pr-reviewer-logic",
            vendor="grok",
        ),
        _step(
            "codex_pr_quality",
            ("grok_pr_quality", "grok_pr_logic"),
            "agent",
            role="pr-reviewer-quality",
            vendor="codex",
        ),
        _step(
            "codex_pr_logic",
            ("grok_pr_quality", "grok_pr_logic"),
            "agent",
            role="pr-reviewer-logic",
            vendor="codex",
        ),
        _step("coverage_ok", ("codex_pr_quality", "codex_pr_logic"), "script"),
        _step("handbook_ok", ("coverage_ok",), "script"),
        _step("contributing_ok", ("handbook_ok",), "script"),
        _step("deviation_declared", (), "human", spine=False),
        _step("deviation_granted", ("deviation_declared",), "human", spine=False),
    ),
    "resolve-conflicts": (
        _step("session_registered", (), "script"),
        _step(
            "conflicts_resolved",
            ("session_registered",),
            "agent",
            role="implementer",
            vendor="grok",
        ),
        _step(
            "reviewer_approved",
            ("conflicts_resolved",),
            "agent",
            role="reviewer",
            vendor="grok",
        ),
        _step("local_check_pass", ("reviewer_approved",), "script"),
        _step("pushed", ("local_check_pass",), "script"),
        _step(
            "grok_pr_quality",
            ("pushed",),
            "agent",
            role="pr-reviewer-quality",
            vendor="grok",
        ),
        _step(
            "grok_pr_logic",
            ("pushed",),
            "agent",
            role="pr-reviewer-logic",
            vendor="grok",
        ),
        _step(
            "codex_pr_quality",
            ("grok_pr_quality", "grok_pr_logic"),
            "agent",
            role="pr-reviewer-quality",
            vendor="codex",
        ),
        _step(
            "codex_pr_logic",
            ("grok_pr_quality", "grok_pr_logic"),
            "agent",
            role="pr-reviewer-logic",
            vendor="codex",
        ),
        _step("mergeable", ("codex_pr_quality", "codex_pr_logic"), "script"),
    ),
}


def _satisfied(key: str, checklist: dict[str, str]) -> bool:
    st = checklist.get(key)
    if st == "ja":
        return True
    if st == "n_a" and key in N_A_ALLOWED:
        return True
    return False


def steps_for(workflow: str) -> tuple[Step, ...]:
    chain = CHAINS.get(workflow)
    if not chain:
        raise KeyError(f"unknown workflow: {workflow}")
    expected = set(CHECKLIST_KEYS[workflow])
    got = {s.key for s in chain}
    if expected != got:
        raise RuntimeError(f"chain {workflow} differs from CHECKLIST_KEYS: {expected ^ got}")
    return chain


def find_step(workflow: str, key: str) -> Step | None:
    for s in steps_for(workflow):
        if s.key == key:
            return s
    return None


def next_steps(
    workflow: str,
    checklist: dict[str, str],
    *,
    spine_only: bool = True,
) -> list[Step]:
    """Steps whose needs are met and that are themselves still pending."""
    out: list[Step] = []
    for s in steps_for(workflow):
        if spine_only and not s.spine:
            continue
        st = checklist.get(s.key, "pending")
        # nein = this attempt failed; step is open again
        if st not in ("pending", "nein"):
            continue
        if all(_satisfied(n, checklist) for n in s.needs):
            out.append(s)
    return out


def required_source(step: Step) -> str:
    if step.kind == "human" or step.key in HUMAN_KEYS:
        return "human"
    return "script"


def is_error_fix_originated(snapshot: dict[str, Any] | None) -> bool:
    """True when this is a validated error-fix task.

    Requires both payload.error_id and snapshot.error_fix_confirmed (a matching
    error.fix activity in the same session). Payload alone is not enough —
    an error.seen / error.skip without error.fix must not get the
    spec_written script carve-out.
    """
    if not isinstance(snapshot, dict):
        return False
    payload = snapshot.get("payload")
    if not isinstance(payload, dict) or not payload.get("error_id"):
        return False
    return bool(snapshot.get("error_fix_confirmed"))


@dataclass(frozen=True)
class CloseVerdict:
    allowed: bool
    reason: str
    step: Step | None


def close_allowed(
    workflow: str,
    key: str,
    *,
    checklist: dict[str, str],
    source: str,
    evidence: str | None,
    snapshot: dict[str, Any] | None = None,
    status: str = "ja",
) -> CloseVerdict:
    """May this key be set to ja/n_a NOW? Only the current next step."""
    step = find_step(workflow, key)
    if step is None:
        return CloseVerdict(False, f"unknown key {key}", None)
    if not evidence or not str(evidence).strip():
        return CloseVerdict(False, f"{key} requires evidence", step)
    want = required_source(step)
    if source != want:
        # error-fix implement tasks may script-author spec_written (any status)
        # and, when status=="n_a", deviation_declared/deviation_granted too —
        # HUMAN_KEYS and Step.kind stay human so every other task keeps the
        # human-only requirement for all three keys, unchanged.
        script_carveout = source == "script" and is_error_fix_originated(snapshot)
        deviation_na = (
            key in ("deviation_declared", "deviation_granted") and status == "n_a"
        )
        if not (script_carveout and (key == "spec_written" or deviation_na)):
            return CloseVerdict(
                False, f"{key} requires --source {want} (got {source})", step
            )
    ready = next_steps(workflow, checklist, spine_only=False)
    if step not in ready:
        pending = ",".join(s.key for s in next_steps(workflow, checklist, spine_only=True)) or "-"
        return CloseVerdict(
            False,
            f"{key} is not the next step (open: {pending})",
            step,
        )
    extra = _artifact_ok(step, snapshot or {})
    if extra:
        return CloseVerdict(False, extra, step)
    return CloseVerdict(True, "allow", step)


def _latest_agent(snapshot: dict[str, Any], role: str, vendor: str | None) -> dict[str, Any] | None:
    agents = list(snapshot.get("agents") or [])
    hit = None
    for a in agents:
        if a.get("role") != role:
            continue
        if vendor and a.get("vendor") != vendor:
            continue
        hit = a
    return hit


def _latest_gate(
    snapshot: dict[str, Any], stage: str, dimension: str
) -> dict[str, Any] | None:
    """Latest gate for stage/dimension.

    `load_task_dict` now orders gates by payload origin_seq, so last-wins is the
    true latest write. Head preference is kept deliberately: when snapshot.head_sha
    is set, a gate for that head wins over a chronologically later gate for a
    different head — a head-scoped guarantee, not the same as pure "true latest".
    """
    want = str(snapshot.get("head_sha") or "").strip().lower()
    hit = None
    hit_for_head = None
    for g in snapshot.get("gates") or []:
        if g.get("stage") == stage and g.get("dimension") == dimension:
            hit = g
            g_head = str(g.get("head_sha") or "").strip().lower()
            if want and g_head == want:
                hit_for_head = g
    return hit_for_head if hit_for_head is not None else hit


def _artifact_ok(step: Step, snapshot: dict[str, Any]) -> str:
    """Empty string = artifact is enough; otherwise the block reason."""
    if step.kind == "human":
        return ""
    if step.key == "session_registered":
        if snapshot.get("session_active") is False:
            return "session is not active"
        return ""
    if step.role == "implementer":
        ag = _latest_agent(snapshot, "implementer", step.vendor)
        if not ag or ag.get("status") != "done":
            return "no finished implementer agent"
        verd = str(snapshot.get("implementer_verdict") or "")
        if verd == "blocked":
            return "implementer blocked"
        if verd != "done":
            return f"implementer_verdict={verd or 'missing'}"
        return ""
    if step.role == "reviewer":
        ag = _latest_agent(snapshot, "reviewer", step.vendor)
        if not ag or ag.get("status") != "done":
            return "no finished reviewer agent"
        verd = str(snapshot.get("reviewer_verdict") or "")
        if verd != "approved":
            return f"reviewer_verdict={verd or 'missing'}"
        return ""
    if step.role in ("pr-reviewer-quality", "pr-reviewer-logic"):
        dim = "quality" if step.role.endswith("quality") else "logic"
        stage = "grok-pr" if step.vendor == "grok" else "codex-pr"
        g = _latest_gate(snapshot, stage, dim)
        if not g or g.get("verdict") != "approved":
            return f"gate {stage}/{dim} is not approved"
        return ""
    if step.key == "local_check_pass":
        checks = list(snapshot.get("local_checks") or [])
        if not checks:
            return "no local_check recorded"
        want = str(snapshot.get("head_sha") or "").strip().lower()
        if want:
            # Scope to this head so a stale pass/fail for another (or empty) head
            # cannot mask a fresh check — load_task_dict keeps the full history
            # and same-second ran_at ties are otherwise order-unstable.
            for_head = [
                c
                for c in checks
                if str(c.get("head_sha") or "").strip().lower() == want
            ]
            if any(c.get("result") == "fail" for c in for_head):
                return "local_check fail"
            if not any(
                str(c.get("result") or "") in ("pass", "skip") for c in for_head
            ):
                return "no local_check for current head"
            return ""
        # No bound head: last row per name wins (list is oldest→newest).
        latest: dict[str, Any] = {}
        for c in checks:
            name = c.get("name")
            if name is not None:
                latest[str(name)] = c
        latest_list = list(latest.values())
        if any(c.get("result") == "fail" for c in latest_list):
            return "local_check fail"
        if not any(c.get("result") in ("pass", "skip") for c in latest_list):
            return "local_check without pass/skip"
        return ""
    if step.key == "pushed":
        if not str(snapshot.get("head_sha") or "").strip():
            return "head_sha missing (set after push)"
        return ""
    if step.key in ("contributing_ok", "coverage_ok", "handbook_ok", "mergeable"):
        return ""
    return ""


# Script steps that `run` must not auto-set to ja (real check required).
NO_AUTO_CLOSE = frozenset(
    {"contributing_ok", "coverage_ok", "handbook_ok"}
)


def handoff_prompt(step: Step, *, task_id: str, session_id: str | None) -> str:
    """Single-step brief. The agent sees only THIS step."""
    who = f"{step.vendor or '-'} {step.role or step.kind}"
    return (
        f"Only this step. Nothing else. Do not claim done.\n"
        f"task={task_id} session={session_id or '-'} key={step.key} who={who}\n"
        f"Then stop. close-step is for the script, not you.\n"
    )


def to_json(step: Step) -> dict[str, Any]:
    return {
        "key": step.key,
        "needs": list(step.needs),
        "kind": step.kind,
        "role": step.role,
        "vendor": step.vendor,
        "source": required_source(step),
        "spine": step.spine,
    }
