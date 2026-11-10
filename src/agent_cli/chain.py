"""Static step chain for the session-store spine.

No Postgres, no network, no subprocess. The graph is the process.
The store only holds state. next_steps / close_allowed decide which
single step is open now and who may close it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .gate_order import latest_ordered_gate_rows
from .allow import (
    CHECKLIST_KEYS,
    GATE_UNAVAILABLE_KEYS,
    N_A_ALLOWED,
    SECOND_REVIEW_KEYS,
    SECOND_REVIEW_NOT_POSTED,
    external_second_review_evidence_ok,
    second_review_na,
)
from .model_slots import (
    SLOT_STAGES,
    ModelSlots,
    canonical_checklist,
    canonical_key,
    canonical_stage,
    implementer_from_snapshot,
    models_from_snapshot,
    parse_model_slots,
)
from .pr_guard_config import one_review_for

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

LATE_INNER_STATES = frozenset(
    {
        "local-check",
        "pushing",
        "pr-review",
        "gate-blocked",
    }
)
LATE_INNER_KEYS = frozenset({"implementer_done", "conflicts_resolved", "reviewer_approved"})
CLOSE_STATUSES = ("ja", "n_a", "unavailable")


@dataclass(frozen=True)
class Step:
    key: str
    needs: tuple[str, ...]
    kind: str  # script | agent | human
    role: str | None = None
    # Review slot (first|second) associated with this agent step. Implementer
    # steps may resolve a separate task pin; provider names never live here.
    slot: str | None = None
    spine: bool = True


def _step(
    key: str,
    needs: tuple[str, ...],
    kind: str,
    *,
    role: str | None = None,
    slot: str | None = None,
    spine: bool = True,
) -> Step:
    if kind not in KINDS:
        raise ValueError(f"kind {kind}")
    if slot is not None and slot not in SLOT_STAGES:
        raise ValueError(f"slot {slot}")
    return Step(key=key, needs=needs, kind=kind, role=role, slot=slot, spine=spine)


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
            slot="first",
        ),
        _step(
            "reviewer_approved",
            ("implementer_done",),
            "agent",
            role="reviewer",
            slot="first",
        ),
        _step("local_check_pass", ("reviewer_approved",), "script"),
        _step("pushed", ("local_check_pass",), "script"),
        _step(
            "review_1_quality",
            ("pushed",),
            "agent",
            role="pr-reviewer-quality",
            slot="first",
        ),
        _step(
            "review_1_logic",
            ("pushed",),
            "agent",
            role="pr-reviewer-logic",
            slot="first",
        ),
        _step(
            "review_2_quality",
            ("review_1_quality", "review_1_logic"),
            "agent",
            role="pr-reviewer-quality",
            slot="second",
        ),
        _step(
            "review_2_logic",
            ("review_1_quality", "review_1_logic"),
            "agent",
            role="pr-reviewer-logic",
            slot="second",
        ),
        _step(
            "contributing_ok",
            ("review_2_quality", "review_2_logic"),
            "script",
        ),
        _step("deviation_declared", (), "human", spine=False),
        _step("deviation_granted", ("deviation_declared",), "human", spine=False),
    ),
    "review": (
        _step("session_registered", (), "script"),
        _step("contributing_read", ("session_registered",), "human"),
        _step(
            "review_1_quality",
            ("contributing_read",),
            "agent",
            role="pr-reviewer-quality",
            slot="first",
        ),
        _step(
            "review_1_logic",
            ("contributing_read",),
            "agent",
            role="pr-reviewer-logic",
            slot="first",
        ),
        _step(
            "review_2_quality",
            ("review_1_quality", "review_1_logic"),
            "agent",
            role="pr-reviewer-quality",
            slot="second",
        ),
        _step(
            "review_2_logic",
            ("review_1_quality", "review_1_logic"),
            "agent",
            role="pr-reviewer-logic",
            slot="second",
        ),
        _step("coverage_ok", ("review_2_quality", "review_2_logic"), "script"),
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
            slot="first",
        ),
        _step(
            "reviewer_approved",
            ("conflicts_resolved",),
            "agent",
            role="reviewer",
            slot="first",
        ),
        _step("local_check_pass", ("reviewer_approved",), "script"),
        _step("pushed", ("local_check_pass",), "script"),
        _step(
            "review_1_quality",
            ("pushed",),
            "agent",
            role="pr-reviewer-quality",
            slot="first",
        ),
        _step(
            "review_1_logic",
            ("pushed",),
            "agent",
            role="pr-reviewer-logic",
            slot="first",
        ),
        _step(
            "review_2_quality",
            ("review_1_quality", "review_1_logic"),
            "agent",
            role="pr-reviewer-quality",
            slot="second",
        ),
        _step(
            "review_2_logic",
            ("review_1_quality", "review_1_logic"),
            "agent",
            role="pr-reviewer-logic",
            slot="second",
        ),
        _step("mergeable", ("review_2_quality", "review_2_logic"), "script"),
    ),
}


def _satisfied(key: str, checklist: dict[str, str]) -> bool:
    key = canonical_key(key)
    checklist = canonical_checklist(checklist)
    st = checklist.get(key)
    if st == "ja":
        return True
    if st == "n_a" and key in N_A_ALLOWED:
        return True
    if st == "n_a" and key in SECOND_REVIEW_KEYS:
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
    key = canonical_key(key)
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
    checklist = canonical_checklist(checklist)
    out: list[Step] = []
    for s in steps_for(workflow):
        if spine_only and not s.spine:
            continue
        st = checklist.get(s.key, "pending")
        # nein = this attempt failed; unavailable = retry the same key.
        # neither is a satisfied need for later steps.
        if st not in ("pending", "nein", "unavailable"):
            continue
        if all(_satisfied(n, checklist) for n in s.needs):
            out.append(s)
    return out


def required_source(step: Step) -> str:
    if step.kind == "human" or step.key in HUMAN_KEYS:
        return "human"
    return "script"


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
    """May this key be set to ja/n_a/unavailable NOW? Only the current next step."""
    key = canonical_key(key)
    checklist = canonical_checklist(checklist)
    step = find_step(workflow, key)
    if step is None:
        return CloseVerdict(False, f"unknown key {key}", None)
    if status not in CLOSE_STATUSES:
        return CloseVerdict(False, "close-step --status must be ja|n_a|unavailable", step)
    if status == "unavailable" and key not in GATE_UNAVAILABLE_KEYS:
        return CloseVerdict(False, f"unavailable is not allowed for {key}", step)
    skip_empty = status == "n_a" and key in SECOND_REVIEW_KEYS
    if not skip_empty and (not evidence or not str(evidence).strip()):
        return CloseVerdict(False, f"{key} requires evidence", step)
    want = required_source(step)
    if source != want:
        return CloseVerdict(False, f"{key} requires --source {want} (got {source})", step)
    # A posted second review replaces n_a with ja. n_a already satisfied the
    # chain, so the key is not a next step. The lane still starts. After ja,
    # both second-stage gates are required again. n_a alone does not grant them.
    promote_second = (
        status == "ja"
        and key in SECOND_REVIEW_KEYS
        and checklist.get(key) == "n_a"
    )
    ready = next_steps(workflow, checklist, spine_only=False)
    if step not in ready and not promote_second:
        pending = ",".join(s.key for s in next_steps(workflow, checklist, spine_only=True)) or "-"
        return CloseVerdict(
            False,
            f"{key} is not the next step (open: {pending})",
            step,
        )
    if status == "n_a" and key in SECOND_REVIEW_KEYS:
        if not second_review_na(key, evidence):
            return CloseVerdict(
                False,
                f"{key} evidence must be {SECOND_REVIEW_NOT_POSTED}",
                step,
            )
        # Two reviews are required. This evidence is allowed only when the
        # opener is listed in one_review_authors on the trusted default-branch
        # file. A missing file, a missing key, or an empty list keeps two
        # reviews required. An entry only on the pull request head does not count.
        snap = snapshot or {}
        trusted = snap.get("trusted_pr_guard")
        opener = snap.get("pull_opener")
        if not one_review_for(
            trusted if isinstance(trusted, Mapping) else None,
            opener if isinstance(opener, str) else "",
        ):
            return CloseVerdict(
                False,
                f"{key} n_a requires one_review_authors in the trusted .github/pr-guard.json on the default branch. A missing file, a missing key, or an empty list keeps two reviews required. An entry that exists only on the pull request head does not count.",
                step,
            )
        return CloseVerdict(True, "allow", step)
    snap = snapshot or {}
    if (
        status == "ja"
        and key in SECOND_REVIEW_KEYS
        and models_from_snapshot(snap).second is None
    ):
        if not external_second_review_evidence_ok(snap, evidence):
            return CloseVerdict(
                False,
                f"{key} requires coordinator A38 evidence at the approved review-1 head",
                step,
            )
        return CloseVerdict(True, "allow", step)
    if status == "unavailable":
        extra = _unavailable_gate_ok(step, snap)
    elif _late_inner_skip(key, status, evidence, snap):
        extra = ""
    else:
        extra = _artifact_ok(step, snap)
    if extra:
        return CloseVerdict(False, extra, step)
    return CloseVerdict(True, "allow", step)


def _late_inner_skip(
    key: str, status: str, evidence: str | None, snapshot: dict[str, Any]
) -> bool:
    """Skip the finished-agent artefact when stamping the inner loop late."""
    if status != "ja" or key not in LATE_INNER_KEYS:
        return False
    if not evidence or not str(evidence).strip():
        return False
    return str(snapshot.get("state") or "") in LATE_INNER_STATES


def _unavailable_gate_ok(step: Step, snapshot: dict[str, Any]) -> str:
    """Empty string = latest gate is unavailable; otherwise the block reason."""
    if step.role not in ("pr-reviewer-quality", "pr-reviewer-logic"):
        return f"unavailable is not allowed for {step.key}"
    dim = "quality" if step.role.endswith("quality") else "logic"
    stage = SLOT_STAGES.get(str(step.slot))
    if stage is None:
        return f"{step.key} has no model slot"
    g = _latest_gate(snapshot, stage, dim)
    if not g or g.get("verdict") != "unavailable":
        return f"gate {stage}/{dim} is not unavailable"
    return ""


def _latest_agent(snapshot: dict[str, Any], role: str, slot: str | None) -> dict[str, Any] | None:
    """Newest agent row for this role, restricted to the slot's configured provider."""
    # Recorded-row read: legacy tasks need their historical provider mapping.
    vendor = models_from_snapshot(snapshot).for_slot(slot) if slot else None
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
    return latest_ordered_gate_rows(list(snapshot.get("gates") or [])).get(
        (canonical_stage(stage), dimension)
    )


def _artifact_ok(step: Step, snapshot: dict[str, Any]) -> str:
    """Empty string = artifact is enough; otherwise the block reason."""
    if step.kind == "human":
        return ""
    if step.key == "session_registered":
        if snapshot.get("session_active") is False:
            return "session is not active"
        return ""
    if step.role == "implementer":
        # A pinned task closes only with its configured implementer, like the start
        # check. A task created before model slots carries no pin and may hold a
        # codex implementer row; it closes with a row of either slot's provider.
        # Recorded-row read: this closes only from agents already on the ledger.
        models = models_from_snapshot(snapshot)
        implementer = implementer_from_snapshot(snapshot)
        pinned = snapshot.get("models_pinned", True)
        ag = None
        for a in snapshot.get("agents") or []:
            vendor = str(a.get("vendor") or "")
            if a.get("role") != "implementer":
                continue
            if vendor == implementer or (not pinned and models.slot_of(vendor)):
                ag = a
        if not ag or ag.get("status") != "done":
            return "no finished implementer agent"
        verd = str(snapshot.get("implementer_verdict") or "")
        if verd == "blocked":
            return "implementer blocked"
        if verd != "done":
            return f"implementer_verdict={verd or 'missing'}"
        return ""
    if step.role == "reviewer":
        ag = _latest_agent(snapshot, "reviewer", step.slot)
        if not ag or ag.get("status") != "done":
            return "no finished reviewer agent"
        verd = str(snapshot.get("reviewer_verdict") or "")
        if verd != "approved":
            return f"reviewer_verdict={verd or 'missing'}"
        return ""
    if step.role in ("pr-reviewer-quality", "pr-reviewer-logic"):
        dim = "quality" if step.role.endswith("quality") else "logic"
        stage = SLOT_STAGES.get(str(step.slot))
        if stage is None:
            return f"{step.key} has no model slot"
        g = _latest_gate(snapshot, stage, dim)
        if not g or g.get("verdict") != "approved":
            return f"gate {stage}/{dim} is not approved"
        return ""
    if step.key == "local_check_pass":
        checks = list(snapshot.get("local_checks") or [])
        if not checks:
            return "no local_check recorded"
        if any(c.get("result") == "fail" for c in checks):
            return "local_check fail"
        if not any(c.get("result") in ("pass", "skip") for c in checks):
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


def _selection_snapshot(
    models: ModelSlots | None, snapshot: Mapping[str, Any] | None
) -> Mapping[str, Any]:
    if snapshot is not None:
        return snapshot
    if models is None:
        # New handoff rendering has no recorded row to read; require configuration.
        parse_model_slots(None)
        raise AssertionError("unreachable")
    return {"models": models.as_dict()}


def step_vendor(
    step: Step,
    models: ModelSlots | None,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> str | None:
    """Provider pinned for this step; None when the step has no provider selection."""
    if step.slot is None:
        return None
    if step.role == "implementer":
        return implementer_from_snapshot(_selection_snapshot(models, snapshot))
    if models is None:
        # A provider-bearing step cannot invent a slot assignment.
        parse_model_slots(None)
        raise AssertionError("unreachable")
    return models.for_slot(step.slot)


def step_vendor_label(
    step: Step,
    models: ModelSlots | None,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> str:
    """Provider name for a pinned agent step, ``-`` otherwise."""
    if step.kind != "agent" or step.slot is None:
        return "-"
    return step_vendor(step, models, snapshot=snapshot) or "external"


def handoff_prompt(
    step: Step,
    *,
    task_id: str,
    session_id: str | None,
    models: ModelSlots | None = None,
    snapshot: Mapping[str, Any] | None = None,
) -> str:
    """Single-step brief. The agent sees only THIS step."""
    who = f"{step_vendor_label(step, models, snapshot=snapshot)} {step.role or step.kind}"
    return (
        f"Only this step. Nothing else. Do not claim done.\n"
        f"task={task_id} session={session_id or '-'} key={step.key} who={who}\n"
        f"Then stop. close-step is for the script, not you.\n"
    )


def to_json(
    step: Step,
    models: ModelSlots | None = None,
    *,
    snapshot: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "key": step.key,
        "needs": list(step.needs),
        "kind": step.kind,
        "role": step.role,
        "slot": step.slot,
        "vendor": step_vendor(step, models, snapshot=snapshot),
        "source": required_source(step),
        "spine": step.spine,
    }
