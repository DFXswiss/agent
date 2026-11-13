"""The acceptance report of an order, compiled from stored facts only.

This module performs no network access, starts no subprocess, and asks no model.
"""

from __future__ import annotations

from typing import Any

from .a38_review import RUNS, ReviewError, looks_like_review, parse_review_block
from .coordinator_checks import one_line
from .coordinator_common import a38_task, as_int, coord, redact, target_repo
from .local_ci import LocalCiError, parse_comment
from .model_slots import canonical_stage
from .order_ops import order_summary, round_limit_view
from .store import Store


REPORT_REVIEW_RUNS = 40
REPORT_COMMENTS = 12
REPORT_VISIBLE_BOUND = 600
REPORT_A38_RUNS = 30
REPORT_TEXT_BOUND = 12_000


def _rows_in_start_order(
    store: Store, table: str, task_id: object
) -> list[dict[str, Any]]:
    sequences = {
        row_id: sequence
        for row_id, (_origin, sequence) in store.row_insert_sequences(table).items()
    }
    rows = [row for row in store.rows(table) if row.get("task_id") == task_id]
    rows.sort(
        key=lambda row: (
            sequences.get(str(row.get("id")), 0),
            str(row.get("started_at") or row.get("ran_at") or ""),
            str(row.get("id") or ""),
        )
    )
    return rows


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def visible_text(body: str) -> str:
    if not isinstance(body, str):
        return ""
    lines: list[str] = []
    for raw_line in body.splitlines():
        if raw_line.strip() == "<details>":
            break
        line = raw_line.strip()
        if line:
            lines.append(line)
    return redact("\n".join(lines), limit=REPORT_VISIBLE_BOUND)


def review_runs(store: Store, task: dict[str, Any]) -> list[dict[str, Any]]:
    task_id = task["id"]
    lanes = [
        row
        for row in _rows_in_start_order(store, "agent", task_id)
        if row.get("status") == "done"
    ]
    rounds = {
        int(row.get("round") or 0): row
        for row in store.rows("task_round")
        if row.get("task_id") == task_id
    }
    gates: dict[str, list[dict[str, Any]]] = {}
    for row in store.rows("review_gate"):
        if row.get("task_id") == task_id:
            gates.setdefault(str(row.get("agent_id") or ""), []).append(row)

    last_reviewers: dict[int, dict[str, Any]] = {}
    for row in lanes:
        if row.get("role") == "reviewer":
            last_reviewers[int(row.get("round") or 0)] = row

    runs: list[dict[str, Any]] = []
    open_pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for lane in lanes:
        role = lane.get("role")
        provider = one_line(lane.get("vendor"), 80) or None
        model = one_line(lane.get("model"), 200) or None
        head = lane.get("head_sha") or None
        at = lane.get("finished_at") or lane.get("started_at")
        if role == "reviewer":
            number = int(lane.get("round") or 0)
            round_row = rounds.get(number)
            verdict = "incomplete"
            lane_finished = lane.get("finished_at")
            round_finished = round_row.get("finished_at") if round_row else None
            recorded = round_row.get("reviewer_verdict") if round_row else None
            if (
                last_reviewers.get(number) is lane
                and recorded in ("approved", "rejected")
                and isinstance(lane_finished, str)
                and bool(lane_finished)
                and isinstance(round_finished, str)
                and bool(round_finished)
                and lane_finished <= round_finished
            ):
                verdict = recorded
            runs.append(
                {
                    "stage": "inner",
                    "round": lane.get("round"),
                    "head": head,
                    "provider": provider,
                    "model": model,
                    "lanes": f"inner={verdict}",
                    "verdict": verdict,
                    "defects": 1 if verdict == "rejected" else 0,
                    "at": at,
                }
            )
            continue
        if role not in ("pr-reviewer-quality", "pr-reviewer-logic"):
            continue
        stage = canonical_stage(str(lane.get("stage") or ""))
        key = (stage, str(lane.get("head_sha") or ""))
        dimension = str(lane.get("dimension") or "")
        run = open_pairs.get(key)
        if run is None or dimension in run["_lanes"]:
            run = {
                "stage": stage,
                "round": None,
                "head": head,
                "provider": provider,
                "model": model,
                "lanes": "",
                "verdict": "incomplete",
                "defects": 0,
                "at": None,
                "_lanes": {},
            }
            runs.append(run)
            open_pairs[key] = run
        lane_gates = gates.get(str(lane.get("id") or ""), [])
        verdict = "incomplete"
        if len(lane_gates) == 1 and lane_gates[0].get("verdict") in (
            "approved",
            "rejected",
        ):
            verdict = lane_gates[0]["verdict"]
        run["_lanes"][dimension] = verdict
        if str(at or "") > str(run["at"] or ""):
            run["at"] = at

    for run in runs:
        pair = run.pop("_lanes", None)
        if pair is None:
            continue
        ordered = [
            (dimension, pair[dimension])
            for dimension in ("quality", "logic")
            if dimension in pair
        ]
        run["lanes"] = " ".join(
            f"{dimension}={verdict}" for dimension, verdict in ordered
        )
        verdicts = [verdict for _dimension, verdict in ordered]
        run["verdict"] = (
            "rejected"
            if "rejected" in verdicts
            else "incomplete"
            if "incomplete" in verdicts
            else "approved"
        )
        run["defects"] = verdicts.count("rejected")

    c = coord(task)
    a38 = c.get("a38") if isinstance(c.get("a38"), dict) else {}
    second = a38.get("second_review")
    if isinstance(second, dict) and isinstance(second.get("reviews"), list):
        for review in second["reviews"]:
            if not isinstance(review, dict):
                continue
            defects = None
            activity_id = review.get("activity_id")
            row = (
                store.row("activity", activity_id)
                if isinstance(activity_id, str) and activity_id
                else None
            )
            body = (
                row.get("payload", {}).get("body")
                if isinstance(row, dict) and isinstance(row.get("payload"), dict)
                else None
            )
            if isinstance(body, str):
                try:
                    defects = _integer(parse_review_block(body).get("defects"))
                except ReviewError:
                    pass
            blocked = review.get("blocks_merge") is True
            runs.append(
                {
                    "stage": "review-2",
                    "round": None,
                    "head": second.get("head") or None,
                    "provider": "external",
                    "model": None,
                    "lanes": f"reviewer={one_line(review.get('reviewer'), 80) or '-'}",
                    "verdict": "rejected" if blocked else "approved",
                    "defects": defects,
                    "at": review.get("at") or None,
                }
            )
    return sorted(runs, key=lambda run: str(run["at"] or ""))


def review_comments(store: Store, task: dict[str, Any]) -> list[dict[str, Any]]:
    number = as_int(coord(task).get("pr_number"))
    if number is None:
        return []
    task_id = str(task["id"])
    repo = target_repo(task).casefold()
    gates = {
        str(row.get("id")): row
        for row in store.rows("review_gate")
        if row.get("task_id") == task["id"]
    }
    comments: list[dict[str, Any]] = []
    for row in store.activity_of_types(("comment.post", "review.post")):
        payload = row.get("payload")
        if (
            row.get("execution_status") != "done"
            or row.get("session_id") != task.get("session_id")
            or not isinstance(payload, dict)
            or str(payload.get("repo") or "").casefold() != repo
            or as_int(payload.get("number")) != number
            or not isinstance(payload.get("body"), str)
        ):
            continue
        body = payload["body"]
        kind = None
        head = passes = defects = blocks_merge = None
        if row.get("type") == "comment.post" and looks_like_review(body):
            try:
                block = parse_review_block(body)
            except ReviewError:
                continue
            block_lanes = block.get("lanes")
            first = block_lanes[0] if isinstance(block_lanes, list) and block_lanes else None
            lane_id = first.get("id") if isinstance(first, dict) else None
            if lane_id == RUNS[0][0]:
                kind = "review-1"
            elif lane_id == RUNS[1][0]:
                kind = "review-2"
            else:
                continue
            head = block.get("head") if isinstance(block.get("head"), str) else None
            passes = _integer(block.get("passes"))
            defects = _integer(block.get("defects"))
            blocks_merge = (
                block.get("blocks_merge")
                if isinstance(block.get("blocks_merge"), bool)
                else None
            )
        elif (
            row.get("type") == "review.post"
            and payload.get("event") == "COMMENT"
            and payload.get("task_id") == task_id
            and isinstance(payload.get("gate_id"), str)
            and payload["gate_id"]
        ):
            kind = "gate-findings"
            gate = gates.get(payload["gate_id"])
            head = (gate.get("head_sha") or None) if gate is not None else None
        else:
            continue
        result = row.get("result")
        raw_url = result.get("url") if isinstance(result, dict) else None
        url = one_line(raw_url, 300) if isinstance(raw_url, str) and raw_url else None
        comments.append(
            {
                "kind": kind,
                "url": url,
                "head": head,
                "passes": passes,
                "defects": defects,
                "blocks_merge": blocks_merge,
                "visible": visible_text(body),
            }
        )
    return comments


def ci_view(task: dict[str, Any]) -> dict[str, Any]:
    c = coord(task)
    evidence = c.get("evidence") if isinstance(c.get("evidence"), dict) else {}
    head = str(c.get("head_sha") or "")
    if head and evidence.get("ci_green") is True and evidence.get("ci_head") == head:
        state = "green"
    elif head and evidence.get("ci_green") is False and evidence.get("ci_head") == head:
        state = "failed"
    elif str(c.get("phase") or "") == "ci":
        state = "pending"
    else:
        state = "unknown"
    return {
        "state": state,
        "head": evidence.get("ci_head") or None,
        "observed_at": (evidence.get("ci_observed_at") or None) if state == "green" else None,
    }


def _a38_report(store: Store, c: dict[str, Any], head: str) -> dict[str, Any] | None:
    if not a38_task(c):
        return None
    a38 = c.get("a38") if isinstance(c.get("a38"), dict) else {}
    report = a38.get("report")
    waiver = a38.get("report_waiver")
    if head and isinstance(report, dict) and report.get("head") == head:
        activity_id = report.get("activity_id")
        row = (
            store.row("activity", activity_id)
            if isinstance(activity_id, str) and activity_id
            else None
        )
        body = (
            row.get("payload", {}).get("body")
            if isinstance(row, dict) and isinstance(row.get("payload"), dict)
            else None
        )
        try:
            parsed = parse_comment(body) if isinstance(body, str) else None
        except LocalCiError:
            parsed = None
        if parsed is None:
            return {
                "result": "unreadable", "head": head, "url": report.get("url") or None,
                "runs": [], "runs_omitted": 0, "reason": None,
            }
        all_runs = list(parsed.runs)
        return {
            "result": "pass" if all(run.result == "pass" and run.exit_code == 0 for run in all_runs) else "fail",
            "head": head,
            "url": report.get("url") or None,
            "runs": [
                {"id": run.id, "name": one_line(run.name, 120), "result": run.result, "exit_code": run.exit_code}
                for run in all_runs[:REPORT_A38_RUNS]
            ],
            "runs_omitted": max(0, len(all_runs) - REPORT_A38_RUNS),
            "reason": None,
        }
    if head and isinstance(waiver, dict) and waiver.get("head") == head:
        return {
            "result": "waived", "head": head, "url": None, "runs": [],
            "runs_omitted": 0, "reason": one_line(waiver.get("reason"), 200) or None,
        }
    return {
        "result": "missing", "head": head or None, "url": None, "runs": [],
        "runs_omitted": 0, "reason": None,
    }


def build_order_report(store: Store, task: dict[str, Any]) -> dict[str, Any]:
    c = coord(task)
    source = c.get("source") if isinstance(c.get("source"), dict) else {}
    summary = order_summary(task)
    head = str(c.get("head_sha") or "")
    pin = round_limit_view(c) or {}
    runs = review_runs(store, task)
    comments = review_comments(store, task)

    waivers: list[dict[str, Any]] = []
    registry = [
        row for row in store.rows("checklist_item")
        if row.get("task_id") == task["id"] and row.get("registry") is True
    ]
    registry.sort(key=lambda row: str(row.get("key") or ""))
    for row in registry:
        for kind in ("skip", "override"):
            value = row.get(kind)
            if isinstance(value, dict):
                waivers.append(
                    {
                        "check": row.get("key"), "kind": kind,
                        "by": one_line(value.get("by"), 80) or None,
                        "reason": one_line(value.get("reason"), 500) or None,
                        "at": value.get("at") or None,
                    }
                )

    stats = c.get("change_stats")
    files = None
    if isinstance(stats, dict):
        files = {
            "head": stats.get("head"), "base": stats.get("base"),
            "total": stats.get("total"), "added": stats.get("added"),
            "deleted": stats.get("deleted"),
            "listed": [
                {"path": item.get("path"), "added": item.get("added"), "deleted": item.get("deleted")}
                for item in (stats.get("files") or []) if isinstance(item, dict)
            ],
            "omitted": stats.get("omitted"), "error": stats.get("error"),
        }

    sequences = {
        row_id: sequence
        for row_id, (_origin, sequence) in store.row_insert_sequences("local_check").items()
    }
    check_rows = [
        row for row in store.rows("local_check") if row.get("task_id") == task["id"]
    ]
    check_rows.sort(
        key=lambda row: (
            str(row.get("ran_at") or ""),
            sequences.get(str(row.get("id")), 0),
        )
    )
    latest_checks: dict[str, dict[str, Any]] = {}
    for row in check_rows:
        latest_checks[str(row.get("name") or "")] = row
    local_checks = [
        {
            "name": one_line(name, 80), "result": row.get("result"),
            "head": row.get("head_sha") or None, "ran_at": row.get("ran_at") or None,
        }
        for name, row in sorted(latest_checks.items())
    ]

    return {
        "order_id": summary["order_id"], "title": one_line(source.get("title"), 256) or None,
        "state": summary["state"], "phase": summary["phase"], "repo": summary["repo"],
        "base": one_line(source.get("base"), 100) or None, "pr_url": summary["pr_url"],
        "head": head or None,
        "summary_en": one_line(task.get("change_summary_en"), 800) or None,
        "summary_de": one_line(task.get("change_summary_de"), 800) or None,
        "rounds": {
            "used": int(task.get("current_round") or 0), "limit": pin.get("limit"),
            "max_rounds": pin.get("max_rounds"), "origin": pin.get("origin"),
            "model": pin.get("model"),
        },
        "waivers": waivers, "files": files,
        "reviews": runs[-REPORT_REVIEW_RUNS:],
        "reviews_omitted": max(0, len(runs) - REPORT_REVIEW_RUNS),
        "comments": comments[-REPORT_COMMENTS:],
        "comments_omitted": max(0, len(comments) - REPORT_COMMENTS),
        "a38_report": _a38_report(store, c, head),
        "local_checks": local_checks, "ci": ci_view(task),
    }


def render_order_report(report: dict[str, Any]) -> list[str]:
    def v(value: object) -> str:
        if value is None or value == "":
            return "-"
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    def h(value: object) -> str:
        return str(value)[:7] if isinstance(value, str) and value else "-"

    rounds = report["rounds"]
    lines = [
        f"report {report['order_id']}",
        f"title {v(report.get('title'))}",
        f"state {report['state']} phase {report['phase']}",
        f"repo {v(report.get('repo'))} base {v(report.get('base'))} pr {v(report.get('pr_url'))} head {v(report.get('head'))}",
        f"summary-en {v(report.get('summary_en'))}",
        f"summary-de {v(report.get('summary_de'))}",
        f"rounds {rounds['used']} of {v(rounds.get('limit'))} origin {v(rounds.get('origin'))}",
    ]
    for waiver in report.get("waivers") or []:
        reason = one_line(waiver.get("reason"), 160) or "-"
        lines.append(
            f"waiver {waiver.get('check')} {waiver.get('kind')} by {v(waiver.get('by'))}: {reason}"
        )
    ci = report["ci"]
    lines.append(f"ci {ci['state']} head {h(ci.get('head'))} observed {v(ci.get('observed_at'))}")
    a38 = report.get("a38_report")
    if a38 is not None:
        last = a38.get("reason") if a38.get("result") == "waived" else a38.get("url")
        lines.append(f"a38-report {a38.get('result')} head {h(a38.get('head'))} {v(last)}")
    for check in report.get("local_checks") or []:
        lines.append(
            f"local-check {check.get('name')} {v(check.get('result'))} head {h(check.get('head'))} {v(check.get('ran_at'))}"
        )
    files = report.get("files")
    if files is None:
        lines.append("files -")
    elif files.get("error"):
        lines.append(f"files error {files['error']}")
    else:
        lines.append(
            f"files {v(files.get('total'))} added {v(files.get('added'))} deleted {v(files.get('deleted'))} on {h(files.get('head'))}"
        )
    if files is not None and int(files.get("omitted") or 0) > 0:
        lines.append(f"files-omitted {files['omitted']}")
    if int(report.get("reviews_omitted") or 0) > 0:
        lines.append(f"reviews-omitted {report['reviews_omitted']}")
    if int(report.get("comments_omitted") or 0) > 0:
        lines.append(f"comments-omitted {report['comments_omitted']}")
    facts_end = len(lines)

    details: list[str] = []
    for run in reversed(report.get("reviews") or []):
        details.append(
            f"review {run.get('stage')} {v(run.get('at'))} head {h(run.get('head'))} round {v(run.get('round'))} "
            f"{v(run.get('provider'))}/{v(run.get('model'))} {run.get('lanes')} verdict {run.get('verdict')} defects {v(run.get('defects'))}"
        )
    for comment in reversed(report.get("comments") or []):
        details.append(
            f"comment {comment.get('kind')} head {h(comment.get('head'))} passes {v(comment.get('passes'))} "
            f"defects {v(comment.get('defects'))} blocks-merge {v(comment.get('blocks_merge'))} {v(comment.get('url'))}"
        )
        details.extend(f"| {line}" for line in str(comment.get("visible") or "").splitlines())
    if a38 is not None:
        details.extend(
            f"a38-run {run.get('id')} {run.get('result')} exit {run.get('exit_code')} {run.get('name')}"
            for run in a38.get("runs") or []
        )
    if files is not None:
        details.extend(
            f"file +{v(item.get('added'))} -{v(item.get('deleted'))} {item.get('path')}"
            for item in files.get("listed") or []
        )
    lines.extend(details)
    if len("\n".join(lines)) > REPORT_TEXT_BOUND:
        while len(lines) > facts_end and len("\n".join(lines + ["report truncated"])) > REPORT_TEXT_BOUND:
            lines.pop()
        lines.append("report truncated")
    return lines
