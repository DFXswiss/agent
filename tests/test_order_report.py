from __future__ import annotations

import json
import uuid
from dataclasses import replace

import pytest

from agent_cli import order_ops
from agent_cli.a38_review import COMPLIANCE_PROMPT, REVIEW_BEGIN, REVIEW_END
from agent_cli.coordinator_a38 import _render_review_comment, validate_second_review_payload
from agent_cli.coordinator_common import coord, save_task
from agent_cli.coordinator_lanes import write_review_diff
from agent_cli.local_ci import SCHEMA_ID, LocalCiReport, LocalCiRun, render_block
from agent_cli.order_report import (
    REPORT_A38_RUNS,
    REPORT_COMMENTS,
    REPORT_REVIEW_RUNS,
    REPORT_TEXT_BOUND,
    REPORT_VISIBLE_BOUND,
    build_order_report,
    ci_view,
    render_order_report,
    review_comments,
    review_runs,
    visible_text,
)
from agent_cli.store import Store
from test_coordinator_support import (
    FakeGh,
    make_order_worker,
    make_session,
    patch_account_runners,
    seed_order_task,
    write_accounts,
)


HEAD = "a" * 40
OLD = "c" * 40
BASE = "b" * 40


def env(tmp_path, monkeypatch):
    store = Store(tmp_path)
    write_accounts(store.home)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    worker = make_order_worker(tmp_path)
    fake = FakeGh()
    patch_account_runners(monkeypatch, fake)
    return store, worker, fake


def ready_order(store, worker, **checkpoint):
    values = {"pr_number": 42, "head_sha": HEAD, "base_sha": BASE}
    values.update(checkpoint)
    return seed_order_task(
        store,
        worker,
        str(uuid.uuid4()),
        phase="ci",
        **values,
    )


def add_lane(
    store,
    task,
    lane_id,
    *,
    role,
    started,
    finished,
    round=None,
    head=HEAD,
    stage=None,
    dimension=None,
    vendor="grok",
    model="grok-model",
    note=None,
):
    if role == "reviewer":
        stage = stage or "inner-review"
        dimension = dimension or "inner"
    store.write(
        "agent",
        "insert",
        lane_id,
        {
            "id": lane_id,
            "session_id": task["session_id"],
            "task_id": task["id"],
            "round": round,
            "role": role,
            "vendor": vendor,
            "provider": vendor,
            "model": model,
            "prompt": "spec",
            "head_sha": head,
            "dimension": dimension,
            "stage": stage,
            "status": "done",
            "started_at": started,
            "finished_at": finished,
            "note": note,
        },
    )


def add_gate(
    store,
    task,
    gate_id,
    *,
    lane_id,
    stage,
    dimension,
    verdict,
    head=HEAD,
    vendor="grok",
):
    store.write(
        "review_gate",
        "insert",
        gate_id,
        {
            "id": gate_id,
            "task_id": task["id"],
            "stage": stage,
            "dimension": dimension,
            "vendor": vendor,
            "verdict": verdict,
            "evidence": None,
            "head_sha": head,
            "agent_id": lane_id,
            "recorded_at": "2026-01-01T00:00:00.000000Z",
        },
    )


def add_round(store, task, number, *, verdict, finished):
    row_id = f"{task['id']}-r{number}"
    store.write(
        "task_round",
        "insert",
        row_id,
        {
            "id": row_id,
            "task_id": task["id"],
            "round": number,
            "implementer_verdict": "done",
            "reviewer_verdict": verdict,
            "started_at": "2026-01-01T00:00:00Z",
            "finished_at": finished,
        },
    )


def add_activity(
    store,
    task,
    activity_id,
    *,
    typ,
    body,
    status="done",
    url="https://github.com/example/project/pull/42#c",
    number=42,
    **payload,
):
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": task["session_id"],
            "type": typ,
            "execution_status": status,
            "payload": {
                "repo": "example/project",
                "number": number,
                "body": body,
                **payload,
            },
            "result": {
                "repo": "example/project",
                "number": number,
                "url": url,
            },
        },
    )


def review_body(
    first_lane,
    *,
    head=HEAD,
    passes=1,
    defects=0,
    visible="EN:\nFirst review with model-x",
    **extra,
):
    declaration = {
        "schema": "a38-review/v2",
        "head": head,
        "passes": passes,
        "defects": defects,
        "lanes": [{"id": first_lane}],
        **extra,
    }
    return (
        visible
        + "\n\n<details>\n"
        + REVIEW_BEGIN
        + "\n```json\n"
        + json.dumps(declaration)
        + "\n```\n"
        + REVIEW_END
        + "\n</details>"
    )


def gate_body(text="logic defect"):
    return f"**grok logic — rejected** at `{HEAD[:7]}`\n\n{text}"


def second_review_body():
    lanes = [
        {
            "id": lane_id,
            "provider": "OpenAI",
            "model": "Reviewer Model 2",
            "model_number": "reviewer-model-2",
            "prompt": COMPLIANCE_PROMPT,
            "status": "complete",
            "result": "pass",
        }
        for lane_id in ("conformity-b", "logic-b")
    ]
    payload = validate_second_review_payload(
        {
            "kind": "a38.second-review/v1",
            "task_id": "11111111-1111-4111-8111-111111111111",
            "head": HEAD,
            "defects": 0,
            "blocks_merge": False,
            "findings": "",
            "lanes": lanes,
        }
    )
    declaration = {
        "schema": "a38-review/v2",
        "head": HEAD,
        "passes": 1,
        "defects": 0,
        "set_aside": "none",
        "blocks_merge": False,
        "lanes": payload["lanes"],
    }
    return _render_review_comment(
        {
            "change_summary_en": "The change is complete.",
            "change_summary_de": "Die Änderung ist vollständig.",
        },
        declaration,
        rules_bound=True,
        reviewer="second-reviewer",
    )


def registry_row(store, task, key, *, skip=None, override=None):
    row_id = f"{task['id']}-{key}"
    store.write(
        "checklist_item",
        "insert",
        row_id,
        {
            "id": row_id,
            "task_id": task["id"],
            "key": key,
            "status": "ja",
            "evidence": "recorded",
            "source": "human",
            "deviation_declared": False,
            "deviation_granted": False,
            "granted_by": "human-owner",
            "updated_at": "2026-01-01T00:00:00Z",
            "registry": True,
            "resolver": key,
            "head_sha": None,
            "override": override,
            "skip": skip,
        },
    )


def a38_body(*results):
    runs = tuple(
        LocalCiRun(
            id=run_id,
            name=f"Job {run_id}",
            command="check",
            result=result,
            exit_code=exit_code,
            duration_s=1.0,
            timeout_s=60.0,
        )
        for run_id, result, exit_code in results
    )
    return render_block(
        LocalCiReport(
            schema=SCHEMA_ID,
            repo="example/project",
            head=HEAD,
            private=True,
            recorded_at="2026-01-01T00:00:00Z",
            required=tuple(run.id for run in runs),
            runs=runs,
        )
    )


def a38_order(store, worker, **a38):
    return ready_order(store, worker, a38={"enabled": True, **a38})


def issue_like(store, worker):
    return seed_order_task(
        store,
        worker,
        str(uuid.uuid4()),
        phase="inner_review",
        source={
            "repo": "example/project",
            "number": 7,
            "title": "Fix widget",
            "publication_repo": "example/project",
            "base": "develop",
        },
        worktree=str(worker.workspace_root / "issue"),
        base_sha=BASE,
        head_sha=HEAD,
    )


@pytest.mark.no_pg
def test_visible_text() -> None:
    assert visible_text("EN:\n  First review with m  \n\n<details>\nhidden") == (
        "EN:\nFirst review with m"
    )
    assert len(visible_text("x" * 2000)) <= REPORT_VISIBLE_BOUND
    assert visible_text(None) == ""


def test_inner_review_runs_take_the_round_verdict(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = ready_order(store, worker)
    add_round(store, task, 1, verdict="rejected", finished="2026-01-01T00:10:00Z")
    add_round(store, task, 2, verdict="approved", finished="2026-01-01T00:30:00Z")
    add_lane(
        store, task, "inner-1", role="reviewer", round=1,
        started="2026-01-01T00:00:00Z", finished="2026-01-01T00:09:00Z",
        note="STATUS: complete\nRESULT: approved",
    )
    add_lane(
        store, task, "inner-2", role="reviewer", round=2,
        started="2026-01-01T00:20:00Z", finished="2026-01-01T00:29:00Z",
        note="STATUS: complete\nRESULT: rejected",
    )
    runs = review_runs(store, task)
    assert [(run["round"], run["verdict"], run["defects"]) for run in runs] == [
        (1, "rejected", 1),
        (2, "approved", 0),
    ]
    assert [run["lanes"] for run in runs] == ["inner=rejected", "inner=approved"]
    assert all(
        run["stage"] == "inner"
        and run["head"] == HEAD
        and run["provider"] == "grok"
        and run["model"] == "grok-model"
        for run in runs
    )
    store.close()


def test_inner_lane_without_a_recorded_verdict_is_incomplete(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = ready_order(store, worker)
    add_round(store, task, 1, verdict=None, finished="2026-01-01T00:10:00Z")
    add_round(store, task, 2, verdict="approved", finished="2026-01-01T00:30:00Z")
    add_round(store, task, 3, verdict="approved", finished="2026-01-01T00:10:00Z")
    for lane_id, number, finished in (
        ("a", 1, "2026-01-01T00:01:00Z"),
        ("b", 1, "2026-01-01T00:02:00Z"),
        ("c", 2, "2026-01-01T00:20:00Z"),
        ("d", 2, "2026-01-01T00:29:00Z"),
        ("e", 3, "2026-01-01T00:31:00Z"),
        ("f", 4, "2026-01-01T00:32:00Z"),
    ):
        add_lane(
            store, task, lane_id, role="reviewer", round=number,
            started=finished, finished=finished,
        )
    runs = review_runs(store, task)
    assert [run["verdict"] for run in runs] == [
        "incomplete", "incomplete", "incomplete", "approved", "incomplete", "incomplete"
    ]
    assert all(run["defects"] == 0 for run in runs)
    store.close()


def _pair_lane(store, task, lane_id, dimension, at, *, head=HEAD, stage="review-1", verdict="approved"):
    add_lane(
        store, task, lane_id, role=f"pr-reviewer-{dimension}",
        started=at, finished=at, head=head, stage=stage, dimension=dimension,
    )
    add_gate(
        store, task, f"gate-{lane_id}", lane_id=lane_id, stage=stage,
        dimension=dimension, verdict=verdict, head=head,
    )


def test_pair_runs_are_grouped_by_stage_and_head(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = ready_order(store, worker)
    _pair_lane(store, task, "oq", "quality", "2026-01-01T00:01:00Z", head=OLD)
    _pair_lane(store, task, "ol", "logic", "2026-01-01T00:02:00Z", head=OLD)
    _pair_lane(store, task, "nq", "quality", "2026-01-01T00:03:00Z")
    _pair_lane(store, task, "nl", "logic", "2026-01-01T00:04:00Z", verdict="rejected")
    _pair_lane(store, task, "nl2", "logic", "2026-01-01T00:05:00Z")
    runs = review_runs(store, task)
    assert [run["stage"] for run in runs] == ["review-1", "review-1", "review-1"]
    assert [run["lanes"] for run in runs] == [
        "quality=approved logic=approved",
        "quality=approved logic=rejected",
        "logic=approved",
    ]
    assert [run["verdict"] for run in runs] == ["approved", "rejected", "approved"]
    assert [run["head"] for run in runs] == [OLD, HEAD, HEAD]
    assert [run["round"] for run in runs] == [None, None, None]
    assert [run["defects"] for run in runs] == [0, 1, 0]
    assert runs[1]["at"] == "2026-01-01T00:04:00Z"
    store.close()


def test_pair_lanes_of_one_second_keep_their_start_order(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = ready_order(store, worker)
    for lane_id, dimension in (("q1", "quality"), ("l1", "logic"), ("q2", "quality"), ("l2", "logic")):
        _pair_lane(store, task, lane_id, dimension, "2026-01-01T00:00:00Z")
    assert [run["lanes"] for run in review_runs(store, task)] == [
        "quality=approved logic=approved",
        "quality=approved logic=approved",
    ]
    store.close()


def test_lane_without_a_decided_gate_is_incomplete(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = ready_order(store, worker)
    for index in range(3):
        lane_id = f"lane-{index}"
        add_lane(
            store, task, lane_id, role="pr-reviewer-quality",
            started=f"2026-01-01T00:0{index}:00Z", finished=f"2026-01-01T00:0{index}:00Z",
            stage="review-1", dimension="quality",
        )
        if index == 1:
            add_gate(store, task, "unavailable", lane_id=lane_id, stage="review-1", dimension="quality", verdict="unavailable")
        if index == 2:
            add_gate(store, task, "g1", lane_id=lane_id, stage="review-1", dimension="quality", verdict="approved")
            add_gate(store, task, "g2", lane_id=lane_id, stage="review-1", dimension="quality", verdict="approved")
    runs = review_runs(store, task)
    assert len(runs) == 3
    assert all(run["verdict"] == "incomplete" and run["defects"] == 0 for run in runs)
    store.close()


def test_rejection_with_an_unavailable_sibling_is_kept(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = ready_order(store, worker)
    _pair_lane(store, task, "q", "quality", "2026-01-01T00:01:00Z", verdict="unavailable")
    _pair_lane(store, task, "l", "logic", "2026-01-01T00:02:00Z", verdict="rejected")
    run = review_runs(store, task)[0]
    assert run["lanes"] == "quality=incomplete logic=rejected"
    assert run["verdict"] == "rejected" and run["defects"] == 1
    store.close()


def test_legacy_stage_names_are_canonical(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = ready_order(store, worker)
    _pair_lane(store, task, "q", "quality", "2026-01-01T00:00:00Z", stage="grok-pr")
    assert review_runs(store, task)[0]["stage"] == "review-1"
    store.close()


def test_external_second_review_run(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = a38_order(
        store,
        worker,
        second_review={
            "head": HEAD,
            "reviews": [
                {"reviewer": "first-reviewer", "activity_id": "act-1", "blocks_merge": False},
                {"reviewer": "second-reviewer", "activity_id": "act-2", "blocks_merge": True, "at": "2026-01-01T01:00:00Z"},
            ],
        },
    )
    add_activity(store, task, "act-2", typ="comment.post", body=review_body("conformity-b", defects=2, blocks_merge=True))
    runs = review_runs(store, task)
    assert [run["stage"] for run in runs] == ["review-2", "review-2"]
    assert [run["provider"] for run in runs] == ["external", "external"]
    assert [run["model"] for run in runs] == [None, None]
    assert [run["head"] for run in runs] == [HEAD, HEAD]
    assert [run["lanes"] for run in runs] == [
        "reviewer=first-reviewer",
        "reviewer=second-reviewer",
    ]
    assert runs[0]["defects"] is None and runs[0]["verdict"] == "approved"
    assert runs[1]["defects"] == 2 and runs[1]["verdict"] == "rejected"
    store.close()


def test_review_comments_are_classified(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = ready_order(store, worker)
    add_activity(store, task, "c1-first", typ="comment.post", body=review_body("conformity-a", passes=2))
    add_activity(store, task, "c2-second", typ="comment.post", body=review_body("conformity-b", blocks_merge=False))
    add_gate(store, task, "g-1", lane_id="lane", stage="review-1", dimension="logic", verdict="rejected")
    add_activity(store, task, "c3-gate", typ="review.post", body=gate_body(), event="COMMENT", task_id=task["id"], gate_id="g-1")
    add_activity(store, task, "blocker", typ="comment.post", body="Blocked: x")
    add_activity(store, task, "approve", typ="review.post", body="approve", event="APPROVE")
    add_activity(store, task, "bad", typ="comment.post", body="A38-REVIEW:v1")
    add_activity(store, task, "wrong-number", typ="comment.post", body=review_body("conformity-a"), number=43)
    add_activity(store, task, "pending", typ="comment.post", body=review_body("conformity-a"), status="pending")
    add_activity(store, task, "wrong-task", typ="review.post", body=gate_body(), event="COMMENT", task_id="other", gate_id="g-1")
    comments = review_comments(store, task)
    assert [item["kind"] for item in comments] == ["review-1", "review-2", "gate-findings"]
    assert comments[0] | {"url": None} == {
        "kind": "review-1",
        "url": None,
        "head": HEAD,
        "passes": 2,
        "defects": 0,
        "blocks_merge": None,
        "visible": "EN:\nFirst review with model-x",
    }
    assert comments[0]["url"]
    assert comments[1]["blocks_merge"] is False
    assert comments[2]["head"] == HEAD
    assert comments[2]["passes"] is None
    assert "logic defect" in comments[2]["visible"]
    assert all(set(item) == {"kind", "url", "head", "passes", "defects", "blocks_merge", "visible"} for item in comments)
    no_number = seed_order_task(store, worker, str(uuid.uuid4()), phase="queued")
    assert review_comments(store, no_number) == []
    store.close()


def test_rendered_second_review_comment_is_recognized(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = ready_order(store, worker)
    add_activity(store, task, "second", typ="comment.post", body=second_review_body())
    item = review_comments(store, task)[0]
    assert item["kind"] == "review-2"
    assert item["defects"] == 0 and item["blocks_merge"] is False
    assert item["head"] == HEAD
    assert item["visible"].startswith("EN:\nSecond review with Reviewer Model 2")
    assert "<details>" not in item["visible"]
    store.close()


@pytest.mark.no_pg
@pytest.mark.parametrize(
    ("checkpoint", "expected"),
    (
        ({"head_sha": HEAD, "evidence": {"ci_green": True, "ci_head": HEAD, "ci_observed_at": "now"}}, {"state": "green", "head": HEAD, "observed_at": "now"}),
        ({"head_sha": HEAD, "evidence": {"ci_green": False, "ci_head": HEAD, "ci_observed_at": "old"}}, {"state": "failed", "head": HEAD, "observed_at": None}),
        ({"head_sha": HEAD, "evidence": {"ci_green": True, "ci_head": OLD}}, {"state": "unknown", "head": OLD, "observed_at": None}),
        ({"phase": "ci"}, {"state": "pending", "head": None, "observed_at": None}),
        ({}, {"state": "unknown", "head": None, "observed_at": None}),
        ({"evidence": {"ci_green": True, "ci_head": HEAD}}, {"state": "unknown", "head": HEAD, "observed_at": None}),
    ),
)
def test_ci_view(checkpoint, expected) -> None:
    assert ci_view({"payload": {"coordinator": checkpoint}}) == expected


def test_report_fields(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = ready_order(
        store,
        worker,
        round_limit={"max_rounds": 10, "origin": "default", "model": "grok-model", "limit": 10},
        change_stats={
            "head": HEAD, "base": BASE, "total": 2, "added": 3, "deleted": 1,
            "files": [
                {"path": "src/a.py", "added": 3, "deleted": 1},
                {"path": "img.png", "added": None, "deleted": None},
            ],
            "omitted": 0,
        },
    )
    task["current_round"] = 2
    task["change_summary_en"] = "English summary."
    task["change_summary_de"] = "Deutsche Zusammenfassung."
    save_task(store, task)
    registry_row(store, task, "duplicates_checked", skip={"by": "human-owner", "reason": "none exists", "at": "2026-01-01T00:00:00Z"})
    registry_row(store, task, "review_2", override={"by": "human-owner", "reason": "one review is permitted", "at": "2026-01-02T00:00:00Z", "intent_id": "i-1"})
    store.write(
        "checklist_item",
        "insert",
        f"{task['id']}-ordinary",
        {
            "id": f"{task['id']}-ordinary",
            "task_id": task["id"],
            "key": "ordinary",
            "status": "ja",
        },
    )
    report = build_order_report(store, store.row("task", task["id"]))
    operation = next(op for op in order_ops.OPERATIONS if op.name == "report")
    assert set(report) == set(operation.fields)
    assert report["rounds"] == {"used": 2, "limit": 10, "max_rounds": 10, "origin": "default", "model": "grok-model"}
    assert report["waivers"] == [
        {
            "check": "duplicates_checked",
            "kind": "skip",
            "by": "human-owner",
            "reason": "none exists",
            "at": "2026-01-01T00:00:00Z",
        },
        {
            "check": "review_2",
            "kind": "override",
            "by": "human-owner",
            "reason": "one review is permitted",
            "at": "2026-01-02T00:00:00Z",
        },
    ]
    assert all(set(item) == {"check", "kind", "by", "reason", "at"} for item in report["waivers"])
    assert report["files"]["total"] == 2
    assert report["files"]["listed"] == [
        {"path": "src/a.py", "added": 3, "deleted": 1},
        {"path": "img.png", "added": None, "deleted": None},
    ]
    assert report["files"]["error"] is None
    assert report["pr_url"] == "https://github.com/example/project/pull/42"
    assert report["title"] == "Add a widget" and report["base"] == "develop"
    assert report["a38_report"] is None
    store.close()


def test_report_of_a_queued_order(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = seed_order_task(store, worker, str(uuid.uuid4()), phase="queued")
    report = build_order_report(store, task)
    assert report["state"] == "queued" and report["head"] is None
    assert report["pr_url"] is None and report["files"] is None
    assert report["reviews"] == [] and report["comments"] == []
    assert report["waivers"] == [] and report["local_checks"] == []
    assert report["ci"]["state"] == "unknown"
    assert report["rounds"] == {"used": 0, "limit": None, "max_rounds": None, "origin": None, "model": None}
    lines = render_order_report(report)
    assert "files -" in lines and "ci unknown head - observed -" in lines
    assert not any(line.startswith("a38-report") for line in lines)
    store.close()


@pytest.mark.parametrize(
    ("case", "expected"),
    (
        ("plain", None),
        ("missing", "missing"),
        ("old", "missing"),
        ("pass", "pass"),
        ("fail", "fail"),
        ("bad", "unreadable"),
        ("absent", "unreadable"),
        ("waived", "waived"),
        ("old-waiver", "missing"),
    ),
)
def test_a38_report_outcome(tmp_path, monkeypatch, case, expected) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    if case == "plain":
        task = ready_order(store, worker)
    elif case == "missing":
        task = a38_order(store, worker)
    elif case == "waived":
        task = a38_order(
            store,
            worker,
            report_waiver={
                "head": HEAD,
                "base_sha": BASE,
                "policy_revision": BASE,
                "reason": "markdown-only change set",
                "observed_at": "2026-01-01T00:00:00Z",
            },
        )
    elif case == "old-waiver":
        task = a38_order(store, worker, report_waiver={"head": OLD, "reason": "markdown-only change set"})
    else:
        report_head = OLD if case == "old" else HEAD
        task = a38_order(
            store,
            worker,
            report={
                "head": report_head,
                "base_sha": BASE,
                "policy_revision": BASE,
                "activity_id": "rep-1",
                "id": 9,
                "url": "https://github.com/example/project/pull/42#issuecomment-9",
            },
        )
        if case not in ("absent", "old"):
            if case == "pass":
                body = a38_body(("unit", "pass", 0), ("lint", "pass", 0))
            elif case == "fail":
                body = a38_body(("unit", "pass", 0), ("lint", "fail", 1))
            else:
                body = "no report"
            add_activity(store, task, "rep-1", typ="comment.post", body=body, target="pr", exact_body=True)
    value = build_order_report(store, task)["a38_report"]
    if expected is None:
        assert value is None
    else:
        assert value["result"] == expected
        assert set(value) == {"result", "head", "url", "runs", "runs_omitted", "reason"}
        assert value["head"] == HEAD
        if expected == "missing":
            assert value == {
                "result": "missing",
                "head": HEAD,
                "url": None,
                "runs": [],
                "runs_omitted": 0,
                "reason": None,
            }
        if expected == "unreadable":
            assert value == {
                "result": "unreadable",
                "head": HEAD,
                "url": "https://github.com/example/project/pull/42#issuecomment-9",
                "runs": [],
                "runs_omitted": 0,
                "reason": None,
            }
        if case == "pass":
            assert value["runs"] == [
                {"id": "unit", "name": "Job unit", "result": "pass", "exit_code": 0},
                {"id": "lint", "name": "Job lint", "result": "pass", "exit_code": 0},
            ]
            assert value["runs_omitted"] == 0
            assert value["url"] == "https://github.com/example/project/pull/42#issuecomment-9"
            assert value["reason"] is None
        if case == "fail":
            assert value["runs"][-1] == {
                "id": "lint",
                "name": "Job lint",
                "result": "fail",
                "exit_code": 1,
            }
        if case == "waived":
            assert value == {
                "result": "waived",
                "head": HEAD,
                "url": None,
                "runs": [],
                "runs_omitted": 0,
                "reason": "markdown-only change set",
            }
    store.close()


def test_a38_runs_are_bounded(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = a38_order(
        store,
        worker,
        report={"head": HEAD, "activity_id": "rep-1", "url": "https://x/report"},
    )
    values = [(f"job-{index}", "pass", 0) for index in range(35)]
    add_activity(store, task, "rep-1", typ="comment.post", body=a38_body(*values))
    value = build_order_report(store, task)["a38_report"]
    assert len(value["runs"]) == REPORT_A38_RUNS
    assert value["runs_omitted"] == 5 and value["result"] == "pass"
    store.write("activity", "update", "rep-1", {
        **{key: val for key, val in store.row("activity", "rep-1").items() if not key.startswith("_")},
        "payload": {"repo": "example/project", "number": 42, "body": a38_body(*(values[:-1] + [("job-34", "fail", 1)]))},
    })
    assert build_order_report(store, task)["a38_report"]["result"] == "fail"
    store.close()


def test_local_checks_take_the_newest_row_per_name(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = ready_order(store, worker)
    for row_id, name, result in (
        ("check-1", "coordinator-check", "fail"),
        ("check-2", "coordinator-check", "pass"),
        ("check-3", "a38", "pass"),
    ):
        store.write(
            "local_check",
            "insert",
            row_id,
            {
                "id": row_id,
                "task_id": task["id"],
                "name": name,
                "command": "check",
                "result": result,
                "output": "",
                "ran_at": "2026-01-01T00:00:00Z",
                "head_sha": HEAD,
            },
        )
    checks = build_order_report(store, task)["local_checks"]
    assert checks == [
        {
            "name": "a38",
            "result": "pass",
            "head": HEAD,
            "ran_at": "2026-01-01T00:00:00Z",
        },
        {
            "name": "coordinator-check",
            "result": "pass",
            "head": HEAD,
            "ran_at": "2026-01-01T00:00:00Z",
        },
    ]
    store.close()


def test_report_bounds(tmp_path, monkeypatch) -> None:
    store, worker, _fake = env(tmp_path, monkeypatch)
    task = ready_order(store, worker)
    for number in range(1, 46):
        at = f"2026-01-01T00:{number:02d}:00Z"
        add_round(store, task, number, verdict="approved", finished=at)
        add_lane(store, task, f"inner-{number}", role="reviewer", round=number, started=at, finished=at)
    for number in range(15):
        add_activity(
            store,
            task,
            f"comment-{number:02d}",
            typ="comment.post",
            body=review_body("conformity-a", visible=f"comment {number}"),
        )
    report = build_order_report(store, task)
    assert len(report["reviews"]) == REPORT_REVIEW_RUNS
    assert report["reviews_omitted"] == 5 and report["reviews"][0]["round"] == 6
    assert len(report["comments"]) == REPORT_COMMENTS and report["comments_omitted"] == 3
    assert report["comments"][0]["visible"] == "comment 3"
    store.close()


def sample_report():
    return {
        "order_id": "order-1",
        "title": "Add a widget",
        "state": "ready",
        "phase": "await_merge",
        "repo": "example/project",
        "base": "develop",
        "pr_url": "https://github.com/example/project/pull/42",
        "head": HEAD,
        "summary_en": "Done.",
        "summary_de": "Erledigt.",
        "rounds": {"used": 2, "limit": 10, "max_rounds": 10, "origin": "default", "model": "grok-model"},
        "waivers": [
            {"check": "one", "kind": "skip", "by": "owner", "reason": "reason one", "at": "t1"},
            {"check": "two", "kind": "override", "by": "owner", "reason": "reason two", "at": "t2"},
        ],
        "files": {
            "head": HEAD, "base": BASE, "total": 2, "added": 3, "deleted": 1,
            "listed": [
                {"path": "src/a.py", "added": 3, "deleted": 1},
                {"path": "img.png", "added": None, "deleted": None},
            ],
            "omitted": 0, "error": None,
        },
        "reviews": [
            {"stage": "inner", "round": 1, "head": HEAD, "provider": "grok", "model": "m1", "lanes": "inner=approved", "verdict": "approved", "defects": 0, "at": "t1"},
            {"stage": "review-1", "round": None, "head": HEAD, "provider": "grok", "model": "m2", "lanes": "quality=approved logic=approved", "verdict": "approved", "defects": 0, "at": "t2"},
        ],
        "reviews_omitted": 0,
        "comments": [
            {"kind": "review-1", "url": "u1", "head": HEAD, "passes": 1, "defects": 0, "blocks_merge": None, "visible": "EN:\nFirst\nReady"},
            {"kind": "review-2", "url": "u2", "head": HEAD, "passes": 1, "defects": 0, "blocks_merge": False, "visible": "EN:\nSecond"},
        ],
        "comments_omitted": 0,
        "a38_report": {
            "result": "pass", "head": HEAD, "url": "report-url",
            "runs": [
                {"id": "unit", "name": "Unit", "result": "pass", "exit_code": 0},
                {"id": "lint", "name": "Lint", "result": "pass", "exit_code": 0},
            ],
            "runs_omitted": 0, "reason": None,
        },
        "local_checks": [
            {"name": "a38", "result": "pass", "head": HEAD, "ran_at": "t1"},
            {"name": "coordinator-check", "result": "pass", "head": HEAD, "ran_at": "t2"},
        ],
        "ci": {"state": "green", "head": HEAD, "observed_at": "2026-01-01T00:00:00Z"},
    }


@pytest.mark.no_pg
def test_render_order_and_format() -> None:
    report = sample_report()
    lines = render_order_report(report)
    assert [line.split()[0] for line in lines if not line.startswith("| ")] == [
        "report", "title", "state", "repo", "summary-en", "summary-de", "rounds",
        "waiver", "waiver", "ci", "a38-report", "local-check", "local-check", "files",
        "review", "review", "comment", "comment", "a38-run", "a38-run", "file", "file",
    ]
    review_lines = [line for line in lines if line.startswith("review ")]
    assert " t2 " in review_lines[0]
    comment_lines = [line for line in lines if line.startswith("comment ")]
    assert comment_lines[0].startswith("comment review-2")
    first_comment = lines.index(comment_lines[1])
    assert lines[first_comment + 1:first_comment + 4] == [
        "| EN:",
        "| First",
        "| Ready",
    ]
    assert "rounds 2 of 10 origin default" in lines
    assert "files 2 added 3 deleted 1 on aaaaaaa" in lines
    assert "file +3 -1 src/a.py" in lines and "file +- -- img.png" in lines
    assert "ci green head aaaaaaa observed 2026-01-01T00:00:00Z" in lines
    report["files"]["omitted"] = 4
    report["reviews_omitted"] = 5
    report["comments_omitted"] = 3
    lines = render_order_report(report)
    assert lines.index("files-omitted 4") == lines.index("files 2 added 3 deleted 1 on aaaaaaa") + 1
    assert lines.index("reviews-omitted 5") == lines.index("files-omitted 4") + 1
    assert lines.index("comments-omitted 3") == lines.index("reviews-omitted 5") + 1
    assert lines.index("comments-omitted 3") < next(
        i for i, line in enumerate(lines) if line.startswith("review ")
    )
    assert lines.index("comments-omitted 3") < next(
        i for i, line in enumerate(lines) if line.startswith("comment ")
    )


@pytest.mark.no_pg
def test_render_special_lines() -> None:
    report = sample_report()
    report.update(title=None, files=None, a38_report=None)
    report["comments"][0]["blocks_merge"] = False
    lines = render_order_report(report)
    assert "title -" in lines and "files -" in lines
    assert not any(line.startswith("a38-report") for line in lines)
    assert any("blocks-merge false" in line for line in lines)
    report["files"] = {"error": "fatal: bad revision", "listed": [], "omitted": 0}
    report["a38_report"] = {"result": "waived", "head": HEAD, "url": None, "runs": [], "runs_omitted": 0, "reason": "markdown-only change set"}
    lines = render_order_report(report)
    assert "files error fatal: bad revision" in lines
    assert "a38-report waived head aaaaaaa markdown-only change set" in lines
    report["a38_report"] = {"result": "missing", "head": None, "url": None, "runs": [], "runs_omitted": 0, "reason": None}
    assert "a38-report missing head - -" in render_order_report(report)


@pytest.mark.no_pg
def test_render_is_bounded() -> None:
    report = sample_report()
    report["reviews"] = [dict(report["reviews"][0], at=f"t{index:02d}") for index in range(40)]
    report["comments"] = [dict(report["comments"][0], visible=("x" * 59 + "\n") * 10) for _ in range(12)]
    report["a38_report"]["runs"] = [
        {"id": f"run-{index}", "name": "name", "result": "pass", "exit_code": 0}
        for index in range(30)
    ]
    report["files"]["listed"] = [
        {"path": f"src/{index}.py", "added": 1, "deleted": 0} for index in range(50)
    ]
    lines = render_order_report(report)
    assert len("\n".join(lines)) <= REPORT_TEXT_BOUND
    assert lines[-1] == "report truncated"
    assert all(any(line.startswith(prefix) for line in lines) for prefix in ("ci ", "a38-report ", "local-check ", "files "))
    assert "t39" in next(line for line in lines if line.startswith("review "))
    assert render_order_report(sample_report())[-1] != "report truncated"


@pytest.mark.no_pg
def test_render_bounds_many_local_checks() -> None:
    report = sample_report()
    report["local_checks"] = [
        {
            "name": f"check-{index:03d}-" + "c" * 60,
            "result": "pass",
            "head": "a" * 40,
            "ran_at": "2026-11-14T00:00:00Z",
        }
        for index in range(200)
    ]
    lines = render_order_report(report)
    assert len("\n".join(lines)) <= REPORT_TEXT_BOUND
    assert lines[-1] == "report truncated"
    assert any(line.startswith("ci ") for line in lines)


def test_change_stats_are_recorded_for_an_order(tmp_path, monkeypatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    fake.numstat = "3\t1\tsrc/a.py\n-\t-\timg.png\nbroken line\n"
    task = ready_order(store, worker, worktree=str(tmp_path / "tree"), base_sha=fake.base, head_sha=fake.head)
    path = write_review_diff(store, worker, task, fake, head=fake.head)
    assert path.exists()
    assert coord(task)["change_stats"] == {
        "head": fake.head,
        "base": fake.base,
        "total": 2,
        "added": 3,
        "deleted": 1,
        "files": [
            {"path": "src/a.py", "added": 3, "deleted": 1},
            {"path": "img.png", "added": None, "deleted": None},
        ],
        "omitted": 0,
    }
    assert "change_stats" not in coord(store.row("task", task["id"]))
    assert fake.git_commands[-1][-5:] == [
        "diff",
        "--numstat",
        "--no-ext-diff",
        "--no-textconv",
        f"{fake.base}...{fake.head}",
    ]
    store.close()


def test_change_stats_list_is_bounded(tmp_path, monkeypatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    fake.numstat = "".join(f"{index}\t1\tsrc/{index}.py\n" for index in range(60))
    task = ready_order(store, worker, worktree=str(tmp_path / "tree"), base_sha=fake.base, head_sha=fake.head)
    write_review_diff(store, worker, task, fake, head=fake.head)
    stats = coord(task)["change_stats"]
    assert stats["total"] == 60 and len(stats["files"]) == 50 and stats["omitted"] == 10
    assert stats["added"] == sum(range(60))
    store.close()


def test_change_stats_follow_the_evidence_base(tmp_path, monkeypatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    task = ready_order(
        store,
        replace(worker, a38=True),
        worktree=str(tmp_path / "tree"),
        base_sha=fake.base,
        head_sha=fake.head,
        a38={"enabled": True, "base_sha": OLD},
    )
    write_review_diff(store, replace(worker, a38=True), task, fake, head=fake.head)
    assert fake.git_commands[-1][-1] == f"{OLD}...{fake.head}"
    assert coord(task)["change_stats"]["base"] == OLD
    store.close()


def test_change_stats_failure_is_recorded(tmp_path, monkeypatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    fake.numstat_rc = 128
    task = ready_order(store, worker, worktree=str(tmp_path / "tree"), base_sha=fake.base, head_sha=fake.head)
    path = write_review_diff(store, worker, task, fake, head=fake.head)
    assert coord(task)["change_stats"] == {"head": fake.head, "base": fake.base, "error": "fatal: bad revision"}
    assert path.exists()
    store.close()


def test_issue_task_gets_no_change_stats(tmp_path, monkeypatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    task = issue_like(store, worker)
    write_review_diff(store, worker, task, fake, head=fake.head)
    assert "change_stats" not in coord(task)
    assert not any("--numstat" in command for command in fake.git_commands)
    store.close()


def test_report_reads_the_store_only(tmp_path, monkeypatch) -> None:
    store, worker, fake = env(tmp_path, monkeypatch)
    task = ready_order(store, worker)
    _pair_lane(store, task, "quality", "quality", "2026-01-01T00:00:00Z")
    add_activity(store, task, "first", typ="comment.post", body=review_body("conformity-a"))
    registry_row(store, task, "duplicates_checked", skip={"by": "owner", "reason": "none", "at": "now"})
    before = {table: store.rows(table) for table in ("task", "activity", "agent", "checklist_item")}
    build_order_report(store, task)
    assert {table: store.rows(table) for table in before} == before
    assert fake.git_commands == []
    store.close()
