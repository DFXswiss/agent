from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from agent_cli import order_ops, preconditions
from agent_cli.coordinator_config import WorkerConfig

pytestmark = pytest.mark.no_pg

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    "DESIGN.md",
    "AGENTS.md",
    "README.md",
    "docs/issue-coordinator.md",
    "docs/pull-request-lifecycle.md",
    "docs/ai-accounts.md",
    "docs/conflict-mandate.md",
    "src/agent_cli/skills/pr-review/SKILL.md",
    "src/agent_cli/skills/review-loop/SKILL.md",
    "src/agent_cli/skills/conflicts/SKILL.md",
)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _ws(text: str) -> str:
    return " ".join(text.split())


def _between(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


ORDER_EVENT_ROW = "| `order.event` | `order` | script |"
ORDER_REPLY_ROW = "| `order.reply` | `order` | script"
ORDER_USAGE = "agent order " + "|".join(op.name for op in order_ops.OPERATIONS)


def test_design_numbers_the_orders_section_after_the_conflicts_section() -> None:
    design = _read("DESIGN.md")
    assert design.index("### 19.9 Comment mandate: conflicts") < design.index(
        "### 19.10 Orders and the precondition registry"
    ) < design.index("## 20. ")
    assert "DESIGN.md#199-comment-mandate-conflicts" in _read(
        "docs/conflict-mandate.md"
    )


def test_activity_catalog_lists_both_order_types() -> None:
    catalog = _between(
        _read("DESIGN.md"), "## 14. Activity catalog", "## 15. "
    )
    assert ORDER_EVENT_ROW in catalog
    assert ORDER_REPLY_ROW in catalog


def test_every_order_operation_is_documented() -> None:
    design = _between(_read("DESIGN.md"), "## 16. CLI surface", "## 17. ")
    for operation in order_ops.OPERATIONS:
        assert f"agent order {operation.name}" in design
    assert ORDER_USAGE in _read("README.md")


def test_registry_is_documented() -> None:
    section = _between(
        _read("docs/issue-coordinator.md"),
        "## Precondition registry",
        "## Check lane",
    )
    for key in preconditions.REGISTRY_KEYS:
        assert f"`{key}`" in section
    for name in preconditions.ACTIONS:
        assert f"`{name}`" in section
    for key in preconditions.OVERRIDABLE:
        assert f"`{key}`" in section
    for key in preconditions.SKIPPABLE:
        assert f"`{key}`" in section


def test_worker_keys_are_documented() -> None:
    section = _between(
        _read("docs/issue-coordinator.md"),
        "## Explicit configuration",
        "## Public API",
    )
    fields = {field.name for field in dataclasses.fields(WorkerConfig)}
    for name in (
        "orders",
        "draft_on_accept",
        "park_poll_seconds",
        "lane_retries",
        "max_rounds",
        "max_rounds_by_model",
        "a38_second_review",
        "conflict_remedy",
    ):
        assert name in fields
        assert f"`{name}`" in section


def test_replaced_sentences_are_gone() -> None:
    prose = _ws(_read("docs/issue-coordinator.md"))
    for old in (
        "No round cap.",
        "No empty fake PR",
        "Uncertain lane outcomes refuse a second model start",
        "`uncertain_lane` remains final",
        "Uncertain prior agents refuse a second model start",
        "is a final uncertain-lane stop",
        "terminal uncertain-lane state",
        "takes its second review from people, so do not point one",
    ):
        assert old not in prose


def test_new_texts_name_the_concierge_only() -> None:
    lines = [
        (relative, line)
        for relative in DOCUMENTS
        for line in _read(relative).splitlines()
    ]
    assert not [line for _, line in lines if "liaison" in line.casefold()]
    console = [
        (relative, line)
        for relative, line in lines
        if "console" in line.casefold()
    ]
    assert console == [
        (
            "DESIGN.md",
            "| Website as an operations console for leases, token cost, stuck "
            "coding tasks | Website is the visibility replica (§7). |",
        )
    ]
    design = _between(_read("DESIGN.md"), "### 19.10", "## 20. ")
    assert "concierge" in design
    assert "concierge" in _read("AGENTS.md")


def test_skill_contracts_name_the_model_route() -> None:
    for relative in (
        "src/agent_cli/skills/pr-review/SKILL.md",
        "src/agent_cli/skills/review-loop/SKILL.md",
    ):
        assert 'a38_second_review: "model"' in _read(relative)
