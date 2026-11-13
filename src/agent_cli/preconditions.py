"""Declarative coordinator preconditions as pure data without store, network, or subprocess access."""

from __future__ import annotations

from dataclasses import dataclass

ORDER = "order"
ISSUE = "github-issue"
CHECK_STATUSES = ("pending", "ja", "nein", "n_a", "unavailable")


@dataclass(frozen=True)
class Check:
    key: str
    fact: str
    resolver: str = ""
    on_pending: str = "wait"
    on_no: str = "block"
    overridable: bool = False
    skippable: bool = False
    override_pending: bool = False
    pending_route: str = ""
    head_bound: bool = False
    n_a_ok: bool = False
    recheck: bool = False
    block_kind: str = ""


@dataclass(frozen=True)
class Requirement:
    check: str
    kinds: tuple[str, ...] = (ORDER,)
    when: str = "always"


@dataclass(frozen=True)
class Action:
    name: str
    phases: tuple[str, ...]
    requires: tuple[Requirement, ...]
    waived_by: str = ""
    waived_route: str = ""


PHASES = (
    "queued",
    "accept",
    "checkout",
    "start_draft",
    "implement",
    "publish_draft",
    "inner_review",
    "tests",
    "pr_gates_grok",
    "a38_first_comment",
    "pr_gates_codex",
    "a38_second_comment",
    "await_second_review",
    "ci",
    "readiness",
    "formal_approve",
    "leave_draft",
    "await_ready",
    "await_merge",
    "ask",
    "blocked",
    "done",
)
WAITING_PHASES = frozenset({"queued", "ask", "blocked", "done"})
ROUTE_ALIASES = frozenset({"second_review"})
WHEN_NAMES = frozenset(
    {"always", "a38", "not_a38", "model_second", "external_second"}
)

_CHECKS = (
    Check("order_active", "task", on_no="fail"),
    Check(
        "session_registered",
        "checklist",
        resolver="session_registered",
        on_pending="resolve",
    ),
    Check("spec_written", "checklist", resolver="spec_written", on_pending="resolve"),
    Check(
        "order_authorized",
        "registry",
        resolver="order_authorized",
        on_pending="resolve",
    ),
    Check(
        "spec_available",
        "registry",
        resolver="spec_available",
        on_pending="resolve",
        recheck=True,
    ),
    Check(
        "checkout_owned",
        "registry",
        resolver="checkout_owned",
        on_pending="resolve",
        recheck=True,
    ),
    Check(
        "duplicates_checked",
        "registry",
        resolver="duplicates_checked",
        on_pending="resolve",
        on_no="question:override",
        overridable=True,
        skippable=True,
    ),
    Check(
        "no_live_lane",
        "registry",
        resolver="no_live_lane",
        on_pending="resolve",
        on_no="retry",
        recheck=True,
        block_kind="lane-failed",
    ),
    Check(
        "round_budget",
        "registry",
        resolver="round_budget",
        on_pending="resolve",
        on_no="question:reply",
        recheck=True,
    ),
    Check(
        "head_signed_clean",
        "registry",
        resolver="head_signed_clean",
        on_pending="resolve",
        head_bound=True,
        recheck=True,
    ),
    Check(
        "pr_mergeable",
        "registry",
        resolver="pr_mergeable",
        on_pending="resolve",
        head_bound=True,
        recheck=True,
        block_kind="conflict",
    ),
    Check("draft_open", "checkpoint", on_pending="route:start_draft"),
    Check("implementer_outcome", "checkpoint"),
    Check(
        "implementer_done",
        "round",
        on_pending="route:implement",
        on_no="route:implement",
    ),
    Check(
        "reviewer_approved",
        "round",
        on_pending="route:inner_review",
        on_no="route:implement",
    ),
    Check(
        "tests_green",
        "checkpoint",
        on_pending="route:tests",
        on_no="route:implement",
        head_bound=True,
    ),
    Check(
        "head_published",
        "checkpoint",
        on_pending="route:publish_draft",
        head_bound=True,
    ),
    Check(
        "report_posted",
        "checkpoint",
        on_pending="route:tests",
        head_bound=True,
    ),
    Check(
        "review_1_quality",
        "gate",
        on_pending="route:pr_gates_grok",
        on_no="route:implement",
        head_bound=True,
    ),
    Check(
        "review_1_logic",
        "gate",
        on_pending="route:pr_gates_grok",
        on_no="route:implement",
        head_bound=True,
    ),
    Check(
        "first_comment_posted",
        "checkpoint",
        on_pending="route:a38_first_comment",
        head_bound=True,
    ),
    Check(
        "review_2_quality",
        "gate",
        on_pending="route:second_review",
        on_no="route:implement",
        head_bound=True,
        n_a_ok=True,
    ),
    Check(
        "review_2_logic",
        "gate",
        on_pending="route:second_review",
        on_no="route:implement",
        head_bound=True,
        n_a_ok=True,
    ),
    Check(
        "review_2_recorded",
        "gate",
        on_pending="route:pr_gates_codex",
        head_bound=True,
    ),
    Check(
        "second_comment_posted",
        "checkpoint",
        on_pending="route:a38_second_comment",
        on_no="route:implement",
        head_bound=True,
    ),
    Check(
        "review_2",
        "registry",
        resolver="review_2",
        on_pending="resolve",
        pending_route="second_review",
        on_no="route:implement",
        overridable=True,
        override_pending=True,
        head_bound=True,
        recheck=True,
    ),
    Check(
        "one_review_permitted",
        "registry",
        resolver="one_review_permitted",
        on_pending="resolve",
        recheck=True,
    ),
    Check(
        "ci_green",
        "checkpoint",
        on_pending="route:ci",
        on_no="route:implement",
        head_bound=True,
    ),
    Check("contributing_ok", "checklist", on_pending="route:readiness"),
    Check(
        "deviation_declared",
        "checklist",
        on_pending="route:readiness",
        n_a_ok=True,
    ),
    Check(
        "deviation_granted",
        "checklist",
        on_pending="route:readiness",
        n_a_ok=True,
    ),
    Check(
        "readiness_on_head",
        "checkpoint",
        on_pending="route:readiness",
        head_bound=True,
    ),
    Check(
        "formal_approved",
        "checkpoint",
        on_pending="route:formal_approve",
        head_bound=True,
    ),
    Check("ready_allowed", "task"),
)
CHECKS = {check.key: check for check in _CHECKS}

R = Requirement
OI = (ORDER, ISSUE)
_ACTIONS = (
    Action(
        "accept",
        ("accept",),
        (
            R("order_active"),
            R("session_registered"),
            R("order_authorized"),
            R("spec_available"),
            R("spec_written"),
        ),
    ),
    Action(
        "checkout",
        ("checkout",),
        (R("order_active"), R("spec_written")),
    ),
    Action(
        "open_draft",
        ("start_draft",),
        (R("order_active"), R("checkout_owned"), R("duplicates_checked")),
    ),
    Action(
        "implement",
        ("implement",),
        (
            R("order_active"),
            R("spec_available"),
            R("draft_open"),
            R("checkout_owned"),
            R("no_live_lane", OI),
            R("round_budget", OI),
        ),
    ),
    Action(
        "commit_push",
        ("publish_draft",),
        (
            R("order_active"),
            R("implementer_outcome", when="phase:implement"),
            R("checkout_owned"),
        ),
    ),
    Action(
        "inner_review",
        ("inner_review",),
        (
            R("order_active"),
            R("draft_open"),
            R("implementer_done"),
            R("checkout_owned"),
            R("no_live_lane", OI),
        ),
    ),
    Action(
        "tests",
        ("tests",),
        (
            R("order_active"),
            R("reviewer_approved"),
            R("head_signed_clean"),
            R("checkout_owned"),
        ),
    ),
    Action(
        "first_review",
        ("pr_gates_grok",),
        (
            R("order_active"),
            R("tests_green"),
            R("head_published"),
            R("report_posted", when="a38"),
            R("head_signed_clean"),
            R("checkout_owned"),
            R("no_live_lane", OI),
        ),
    ),
    Action(
        "first_review_comment",
        ("a38_first_comment",),
        (
            R("order_active"),
            R("review_1_quality"),
            R("review_1_logic"),
            R("report_posted"),
        ),
    ),
    Action(
        "second_review",
        ("pr_gates_codex", "await_second_review"),
        (
            R("order_active"),
            R("review_1_quality"),
            R("review_1_logic"),
            R("first_comment_posted", when="a38"),
            R("head_signed_clean", when="phase:pr_gates_codex"),
            R("checkout_owned", when="phase:pr_gates_codex"),
            R("no_live_lane", OI, when="phase:pr_gates_codex"),
        ),
        waived_by="review_2",
        waived_route="ci",
    ),
    Action(
        "second_review_comment",
        ("a38_second_comment",),
        (
            R("order_active"),
            R("first_comment_posted"),
            R("review_2_recorded"),
        ),
        waived_by="review_2",
        waived_route="ci",
    ),
    Action(
        "ci",
        ("ci",),
        (
            R("order_active"),
            R("draft_open"),
            R("review_2"),
        ),
    ),
    Action(
        "readiness",
        ("readiness",),
        (
            R("order_active"),
            R("tests_green"),
            R("ci_green"),
            R("review_1_quality"),
            R("review_1_logic"),
            R("review_2"),
            R("head_signed_clean"),
            R("pr_mergeable", OI),
        ),
    ),
    Action(
        "ready",
        ("formal_approve", "leave_draft", "await_ready"),
        (
            R("order_active"),
            R("contributing_ok"),
            R("deviation_declared"),
            R("deviation_granted"),
            R("readiness_on_head"),
            R("ci_green"),
            R("ready_allowed"),
            R("formal_approved", when="phase:leave_draft"),
            R("pr_mergeable", OI, when="phase:leave_draft"),
        ),
    ),
    Action(
        "merge_observation",
        ("await_merge",),
        (R("order_active"), R("draft_open")),
    ),
    Action("close_draft", (), (R("draft_open", OI),)),
    Action(
        "waive_second_review",
        (),
        (R("order_active"), R("draft_open"), R("one_review_permitted")),
    ),
)
ACTIONS = {action.name: action for action in _ACTIONS}
PHASE_ACTION = {phase: action.name for action in _ACTIONS for phase in action.phases}
REGISTRY_KEYS = frozenset(check.key for check in _CHECKS if check.fact == "registry")
OVERRIDABLE = frozenset(check.key for check in _CHECKS if check.overridable)
SKIPPABLE = frozenset(check.key for check in _CHECKS if check.skippable)


def action_requiring(key: str, kind: str) -> str | None:
    """Return the first registry action requiring this check for this source kind."""
    for action in _ACTIONS:
        for requirement in action.requires:
            if requirement.check == key and kind in requirement.kinds:
                return action.name
    return None


__all__ = [
    "ORDER",
    "ISSUE",
    "CHECK_STATUSES",
    "Check",
    "Requirement",
    "Action",
    "PHASES",
    "WAITING_PHASES",
    "ROUTE_ALIASES",
    "WHEN_NAMES",
    "CHECKS",
    "ACTIONS",
    "PHASE_ACTION",
    "REGISTRY_KEYS",
    "OVERRIDABLE",
    "SKIPPABLE",
    "action_requiring",
]
