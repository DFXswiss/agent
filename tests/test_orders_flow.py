from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent_cli import github_act
from agent_cli.a38_guard import status_context_enforce
from agent_cli.a38_review import ReviewError, parse_review_block
from agent_cli.allow import CHECKLIST_KEYS
from agent_cli.coordinator import tick
from agent_cli.coordinator_orders import own_order_events
from agent_cli.runtime import Completed
from agent_cli.store import Store
from test_coordinator import _a38_policy_text, _write_coordinator_a38_report
from test_coordinator_support import (
    FakeGh,
    make_order_worker,
    make_session,
    order_lane_runner,
    patch_account_runners,
    patch_command_runner,
    scan_done,
    submit_order,
    write_accounts,
    write_check_lane,
)


REFERENCE_CHECKS = (
    "order_authorized",
    "spec_available",
    "checkout_owned",
    "duplicates_checked",
    "no_live_lane",
    "round_budget",
    "head_signed_clean",
    "pr_mergeable",
)


class A38FlowGh(FakeGh):
    def __call__(self, argv: list[str]) -> Completed:
        normalized = argv
        if normalized and normalized[0] == "env":
            if "gh" in normalized:
                normalized = normalized[normalized.index("gh") :]
            elif "git" in normalized:
                normalized = normalized[normalized.index("git") :]
        joined = " ".join(normalized)
        if "pulls/42/files" in joined:
            return Completed(
                0,
                json.dumps(
                    [[{"filename": "src/app.py", "status": "modified"}]]
                ),
                "",
            )
        if (
            normalized[:2] == ["gh", "api"]
            and normalized[-2:] == ["--jq", ".private"]
        ):
            return Completed(0, "false", "")
        if (
            normalized
            and normalized[0] == "git"
            and "show" in normalized
            and normalized[-1].endswith(":.github/a38.json")
        ):
            return Completed(0, _a38_policy_text(), "")
        return super().__call__(argv)


def _install_a38_suite_fakes(
    monkeypatch: pytest.MonkeyPatch, fake: A38FlowGh
) -> None:
    monkeypatch.setattr(
        "agent_cli.coordinator_a38.run_bounded",
        lambda *_args, **_kwargs: Completed(0, "trusted check passed", ""),
    )

    def run_cli(argv, *_args, **_kwargs):  # noqa: ANN001
        if argv[0] == "run":
            output = Path(argv[argv.index("--output") + 1])
            fake.a38_report_text = _write_coordinator_a38_report(
                output, head=fake.head, base=fake.base
            )
        return Completed(0, "ok", "")

    monkeypatch.setattr("agent_cli.coordinator_a38._run_cli", run_cli)


def _events(store: Store, order_id: str) -> list[dict]:
    return [
        row
        for row in own_order_events(store)
        if row.get("payload", {}).get("order_id") == order_id
    ]


def test_reference_flow_to_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    write_check_lane(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    store.set_meta("github_login", "human-owner")
    worker = make_order_worker(
        tmp_path,
        review_session=None,
        a38=True,
        a38_second_review="model",
    )
    fake = A38FlowGh()
    fake.issues = []
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    monkeypatch.setattr(github_act, "scan_github", scan_done)
    _install_a38_suite_fakes(monkeypatch, fake)

    ordinary_lane = order_lane_runner(fake)
    draft_seen: list[bool] = []

    def lane(argv, stdin=None):  # noqa: ANN001
        joined = " ".join(argv) + "\n" + (stdin or "")
        if "implementer" in joined and "reviewer" not in joined:
            draft_seen.append(fake.pr_created)
        return ordinary_lane(argv, stdin)

    order = submit_order(store, worker)
    phases: list[str] = []
    observations: list[tuple[str, list[str]]] = []
    for _ in range(40):
        task = store.row("task", order["id"])
        c = task["payload"]["coordinator"]
        phase = c["phase"]
        if phase == "implement" and "implementer" not in fake.launched:
            fake.dirty = True
        if phase == "ci":
            fake.pr["statusCheckRollup"] = [
                {
                    "name": "tests",
                    "conclusion": "success",
                    "status": "completed",
                }
            ]
            fake.workflow_runs = [
                {
                    "id": 1,
                    "path": ".github/workflows/ci.yml",
                    "event": "pull_request",
                    "head_sha": fake.head,
                    "status": "completed",
                    "conclusion": "success",
                    "run_attempt": 1,
                }
            ]
        if phase == "await_ready":
            fake.pr["isDraft"] = False
            fake.statuses = [
                {
                    "context": status_context_enforce("develop"),
                    "state": "success",
                    "description": "A38 passed",
                }
            ]
        lines = tick(store, worker, runner=fake, lane_runner=lane)
        observations.append((phase, lines))
        task = store.row("task", order["id"])
        c = task["payload"]["coordinator"]
        if not phases or phases[-1] != c["phase"]:
            phases.append(c["phase"])
        assert c["phase"] not in ("blocked", "ask"), c.get("blocker")
        if c["phase"] == "await_merge":
            break
    else:
        pytest.fail(
            "reference flow did not reach await_merge: "
            f"phases={phases!r}, observations={observations[-8:]!r}"
        )

    tick(store, worker, runner=fake, lane_runner=lane)
    assert fake.launched[:2] == ["implementer", "reviewer"]
    assert len(fake.launched) == 6
    assert set(fake.launched[2:4]) == {
        "pr-reviewer-quality",
        "pr-reviewer-logic",
    }
    assert set(fake.launched[4:6]) == {
        "pr-reviewer-quality",
        "pr-reviewer-logic",
    }
    assert "check" not in fake.launched
    first_pair = [
        row
        for row in store.rows("agent")
        if row.get("stage") == "review-1"
    ]
    second_pair = [
        row
        for row in store.rows("agent")
        if row.get("stage") == "review-2"
    ]
    assert {row.get("vendor") for row in first_pair} == {"grok"}
    assert {row.get("vendor") for row in second_pair} == {"codex"}
    assert {
        row.get("role") for row in first_pair
    } == {"pr-reviewer-quality", "pr-reviewer-logic"}
    assert {
        row.get("role") for row in second_pair
    } == {"pr-reviewer-quality", "pr-reviewer-logic"}

    expected = [
        "checkout",
        "start_draft",
        "implement",
        "inner_review",
        "tests",
        "pr_gates_grok",
        "a38_first_comment",
        "pr_gates_codex",
        "a38_second_comment",
        "ci",
        "readiness",
        "await_ready",
        "await_merge",
    ]
    assert phases == [phase for phase in expected if phase in phases]
    assert {"start_draft", "implement", "tests", "a38_second_comment", "await_ready"} <= set(phases)
    assert phases[-1] == "await_merge"
    assert fake.empty_commits == 1
    assert draft_seen == [True]

    report_activity = next(
        row
        for row in store.rows("activity")
        if row.get("type") == "comment.post"
        and row.get("payload", {}).get("body") == fake.a38_report_text
    )
    assert report_activity["payload"]["exact_body"] is True
    assert fake.pr_comments[0]["body"] == fake.a38_report_text
    review_comments = []
    for comment in fake.pr_comments[1:]:
        try:
            review_comments.append(parse_review_block(comment["body"]))
        except ReviewError:
            continue
    assert len(review_comments) >= 2
    assert [
        {lane["id"] for lane in declaration["lanes"]}
        for declaration in review_comments[:2]
    ] == [{"conformity-a", "logic-a"}, {"conformity-b", "logic-b"}]
    assert review_comments[1]["blocks_merge"] is False
    assert review_comments[1]["defects"] == 0
    assert store.rows("ping") == []
    assert [
        row for row in store.rows("activity") if row.get("type") == "review.post"
    ] == []

    events = _events(store, order["id"])
    assert [row["payload"]["kind"] for row in events] == [
        "accepted",
        "draft-opened",
        "ready",
    ]
    assert [row["payload"]["kind"] for row in events if row["payload"]["wake"]] == ["ready"]

    task = store.row("task", order["id"])
    c = task["payload"]["coordinator"]
    checklist = {
        row["key"]: row["status"]
        for row in store.rows("checklist_item")
        if row.get("task_id") == order["id"]
    }
    for key in CHECKLIST_KEYS["implement"]:
        expected_status = "n_a" if key in ("deviation_declared", "deviation_granted") else "ja"
        assert checklist[key] == expected_status
    for key in REFERENCE_CHECKS:
        assert checklist[key] == "ja"
    assert checklist["review_2_quality"] == "ja"
    assert checklist["review_2_logic"] == "ja"
    assert c["a38"]["second_comment"]["blocks_merge"] is False
    assert c["a38"]["second_review_passes"] == 1
    assert c["evidence"]["ready_head"] == fake.head

    fake.pr["state"] = "MERGED"
    fake.pr["mergedAt"] = "2026-09-01T12:00:00Z"
    fake.pr["mergeCommit"] = {"oid": "d" * 40}
    fake.pr["mergedBy"] = {"login": "human-owner", "type": "User"}
    for _ in range(3):
        tick(store, worker, runner=fake, lane_runner=lane)
        events = _events(store, order["id"])
        if any(row["payload"]["kind"] == "merged" for row in events):
            break
    assert store.row("task", order["id"])["state"] == "done"
    merged = next(row for row in events if row["payload"]["kind"] == "merged")
    assert merged["payload"]["wake"] is False
    assert sum(bool(row["payload"]["wake"]) for row in events) == 1


def test_reference_flow_with_a_duplicate_stops_before_the_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = Store(tmp_path)
    write_accounts(store.home)
    write_check_lane(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    store.set_meta("github_login", "human-owner")
    worker = make_order_worker(
        tmp_path,
        review_session=None,
        a38=True,
        a38_second_review="model",
    )
    fake = A38FlowGh()
    fake.issues = []
    fake.open_pulls = [
        {
            "number": 5,
            "title": "Add a widget",
            "body": "Adds it",
            "head": {"ref": "feature"},
            "user": {"login": "someone"},
        }
    ]
    patch_account_runners(monkeypatch, fake)
    patch_command_runner(monkeypatch, fake)
    monkeypatch.setattr(github_act, "scan_github", scan_done)
    lane = order_lane_runner(
        fake,
        check_output="RESULT: duplicate 5\nREASON: Same goal.\n",
    )
    order = submit_order(store, worker)
    for _ in range(10):
        tick(store, worker, runner=fake, lane_runner=lane)
        task = store.row("task", order["id"])
        if task["payload"]["coordinator"]["phase"] == "ask":
            break
    assert task["payload"]["coordinator"]["phase"] == "ask"
    assert fake.launched == ["check"]
    assert fake.empty_commits == 0
    assert fake.pr_created is False
    wakes = [row for row in _events(store, order["id"]) if row["payload"]["wake"]]
    assert len(wakes) == 1
    assert wakes[0]["payload"]["kind"] == "question"
    assert wakes[0]["payload"]["check"] == "duplicates_checked"
