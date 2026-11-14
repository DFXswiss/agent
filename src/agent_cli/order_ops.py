"""The order operations: one table for every front end.

The command line and the local dashboard call :func:`run`; no front end holds
a rule about orders.
"""

from __future__ import annotations

import copy
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .coordinator_checks import (
    TaskFacts,
    action_checks,
    current_phase,
    read_fact,
    registry_row_id,
    second_review_waiver_refusal,
)
from .coordinator_common import as_int, coord, redact, target_repo
from .coordinator_config import load_coordinator_config, validate_base
from .coordinator_github import reply_checkpoint_eligible
from .coordinator_orders import (
    ORDER_EVENT,
    ORDER_REPLY,
    acknowledge_event,
    create_order_task,
    find_order,
    latest_order_intent,
    list_orders,
    order_state,
    own_order_events,
    serves_repo,
)
from .preconditions import ACTIONS, CHECKS, ORDER, OVERRIDABLE, PHASE_ACTION
from .store import Store, StoreError, utcnow


class OrderError(StoreError):
    """A refused order operation. Every front end shows the text unchanged."""


@dataclass(frozen=True)
class Param:
    name: str
    flag: str
    meta: str = ""
    kind: str = "line"
    required: bool = False


@dataclass(frozen=True)
class Operation:
    name: str
    mode: str
    params: tuple[Param, ...]
    fields: tuple[str, ...]
    nested: tuple[tuple[str, tuple[str, ...]], ...] = ()
    func: Callable[..., dict[str, Any]] | None = None
    identity: bool = False
    json_flag: bool = False
    json_root: str = ""
    cli_only: str = ""


REASON_BOUND = 500
OPEN_STATES = ("queued", "running", "waiting", "ready")
ENDED_STATES = ("merged", "failed", "cancelled")
ROUND_LIMIT_KEYS = ("max_rounds", "origin", "model", "limit")
SKIPPED_CHECK_KEYS = ("key", "reason", "by", "at")
OPINION_KEYS = ("result", "reasoning", "approach")
ASSESSMENT_KEYS = ("result", "reasoning", "approach", "limit", "rounds", "at")


def int_param(value: object, flag: str, *, minimum: int) -> int | None:
    if value is None:
        return None
    parsed: int | None = None
    if isinstance(value, int) and not isinstance(value, bool):
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]{1,18}", value):
        parsed = int(value)
    if parsed is not None and parsed >= minimum:
        return parsed
    description = "non-negative" if minimum == 0 else "positive"
    raise OrderError(f"{flag} requires a {description} integer")


def wait_interval(home: Path) -> int:
    seconds = [
        worker.poll_seconds
        for worker in load_coordinator_config(home).values()
        if worker.orders
    ]
    if not seconds:
        raise OrderError("orders are not enabled on this device")
    return min(seconds)


def need_order(store: Store, order_id: str) -> dict[str, Any]:
    if store.row("task", order_id) is None:
        raise OrderError(f"unknown task: {order_id}")
    task = find_order(store, order_id)
    if task is None:
        raise OrderError(f"task {order_id} is not an order")
    return task


def require_reason(reason: object) -> str:
    if (
        not isinstance(reason, str)
        or not 1 <= len(reason) <= REASON_BOUND
        or any(character in reason for character in "\r\n\0")
    ):
        raise OrderError("reason must be one line of 1..500 characters")
    return reason


def skip_checks_param(value: object) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, dict)
        or set(item) != {"key", "reason"}
        or not isinstance(item["key"], str)
        or not isinstance(item["reason"], str)
        for item in value
    ):
        raise OrderError("skip_checks must be a list of objects with key and reason")
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for item in value:
        key = item["key"]
        reason = item["reason"]
        if key not in CHECKS:
            raise OrderError(f"unknown check {key}")
        if not CHECKS[key].skippable:
            raise OrderError(f"check {key} cannot be skipped")
        if key in seen:
            raise OrderError(f"check {key} is skipped twice")
        require_reason(reason)
        if not reason.strip():
            raise OrderError("reason must be one line of 1..500 characters")
        seen.add(key)
        result.append({"key": key, "reason": reason})
    return tuple(result)


def round_limit_view(c: dict[str, Any]) -> dict[str, Any] | None:
    pin = c.get("round_limit")
    if (
        not isinstance(pin, dict)
        or not isinstance(pin.get("limit"), int)
        or not isinstance(pin.get("max_rounds"), int)
    ):
        return None
    return {key: pin.get(key) for key in ROUND_LIMIT_KEYS}


def skipped_checks_view(source: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {key: item.get(key) for key in SKIPPED_CHECK_KEYS}
        for item in source.get("skipped_checks") or []
        if isinstance(item, dict)
    ]


def assessment_view(store: Store, order_id: str) -> dict[str, Any] | None:
    row = store.row(
        "checklist_item", registry_row_id(order_id, "round_budget")
    )
    if row is None or not isinstance(row.get("opinion"), dict):
        return None
    return {key: row["opinion"].get(key) for key in ASSESSMENT_KEYS}


def pr_url(task: dict[str, Any]) -> str | None:
    number = as_int(coord(task).get("pr_number"))
    if number is None:
        return None
    return f"https://github.com/{target_repo(task)}/pull/{number}"


def checkpoint_event_id(task: dict[str, Any]) -> str | None:
    order = coord(task).get("order")
    checkpoint = order.get("checkpoint") if isinstance(order, dict) else None
    event_id = checkpoint.get("event_id") if isinstance(checkpoint, dict) else None
    return event_id if isinstance(event_id, str) and event_id else None


def event_view(row: dict[str, Any]) -> dict[str, Any]:
    payload = row["payload"]
    return {
        "id": row["id"],
        "seq": payload.get("seq"),
        "order_id": payload.get("order_id"),
        "kind": payload.get("kind"),
        "wake": payload.get("wake") is True,
        "digest": payload.get("digest") or "",
        "pr_url": payload.get("pr_url"),
        "phase": payload.get("phase"),
        "round": payload.get("round"),
        "check": payload.get("check"),
        "failed_kind": payload.get("failed_kind"),
        "notify_session": payload.get("notify_session"),
        "at": payload.get("at"),
        "acked": payload.get("ack") is not None,
        "answers": list(payload.get("answers") or []),
        "opinion": (
            {key: payload["opinion"].get(key) for key in OPINION_KEYS}
            if isinstance(payload.get("opinion"), dict)
            else None
        ),
    }


def order_summary(task: dict[str, Any]) -> dict[str, Any]:
    checkpoint = coord(task)
    source = (
        checkpoint.get("source")
        if isinstance(checkpoint.get("source"), dict)
        else {}
    )
    return {
        "order_id": str(task["id"]),
        "state": order_state(task),
        "phase": current_phase(task),
        "round": int(task.get("current_round") or 0),
        "repo": target_repo(task),
        "title": str(source.get("title") or ""),
        "pr_url": pr_url(task),
        "head": checkpoint.get("head_sha") or None,
        "updated_at": task.get("updated_at"),
    }


def insert_intent(
    store: Store,
    task: dict[str, Any],
    kind: str,
    *,
    by: str,
    answers: str | None,
    **fields: Any,
) -> str:
    intent_id = str(uuid.uuid4())
    store.write(
        "activity",
        "insert",
        intent_id,
        {
            "id": intent_id,
            "session_id": task["session_id"],
            "type": ORDER_REPLY,
            "execution_status": "pending",
            "payload": {
                "order_id": str(task["id"]),
                "kind": kind,
                **fields,
                "by": by,
                "answers": answers,
                "created_at": utcnow(),
            },
        },
    )
    return intent_id


def op_submit(
    store: Store,
    home: Path,
    *,
    by: str,
    repo: str,
    title: str,
    body: str,
    base: str | None = None,
    worker: str | None = None,
    notify_session: str | None = None,
    skip_checks: object = None,
    max_rounds: object = None,
) -> dict[str, Any]:
    from . import main as cli

    text = cli._read_request_text(repo, title, body, None)
    if "\r" in title or "\n" in title:
        raise OrderError("title must be one line")
    if base is not None:
        validate_base(base)
    skips = skip_checks_param(skip_checks)
    limit = int_param(max_rounds, "--max-rounds", minimum=1)
    config = load_coordinator_config(home)
    if worker is not None:
        if worker not in config:
            raise OrderError("coordinator session is not configured")
        selected = config[worker]
    else:
        candidates = [
            candidate
            for candidate in config.values()
            if candidate.orders and serves_repo(candidate, repo)
        ]
        if not candidates:
            raise OrderError(f"no order-enabled worker serves {repo}")
        from .coordinator_runtime import runnable_count

        selected = min(
            candidates,
            key=lambda candidate: (
                runnable_count(store, candidate),
                candidate.session_id,
            ),
        )
    task = create_order_task(
        store,
        selected,
        repo=repo,
        title=title,
        text=text,
        submitted_by=by,
        requested_base=base,
        notify_session=notify_session,
        max_rounds=limit,
        skipped_checks=skips,
    )
    c = coord(task)
    source = c.get("source") if isinstance(c.get("source"), dict) else {}
    return {
        "order_id": str(task["id"]),
        "repo": target_repo(task),
        "state": order_state(task),
        "worker": selected.session_id,
        "round_limit": round_limit_view(c),
        "skipped_checks": skipped_checks_view(source),
    }


def op_list(
    store: Store, home: Path, *, open_only: bool = False
) -> dict[str, Any]:
    orders = [order_summary(task) for task in list_orders(store)]
    if open_only:
        orders = [item for item in orders if item["state"] in OPEN_STATES]
    return {"orders": orders}


def op_show(store: Store, home: Path, *, order_id: str) -> dict[str, Any]:
    task = need_order(store, order_id)
    checkpoint_data = coord(task)
    source = (
        checkpoint_data.get("source")
        if isinstance(checkpoint_data.get("source"), dict)
        else {}
    )
    events = [
        row
        for row in own_order_events(store)
        if row["payload"].get("order_id") == order_id
    ]
    if events:
        last_view = event_view(events[-1])
        last_event = {
            key: last_view[key] for key in ("id", "seq", "kind", "at", "acked")
        }
    else:
        last_event = None

    pending_event_id = checkpoint_event_id(task)
    pending_row = next(
        (row for row in events if row.get("id") == pending_event_id), None
    )
    if pending_row is None:
        pending_checkpoint = None
    else:
        pending_view = event_view(pending_row)
        pending_checkpoint = {
            "event_id": pending_view["id"],
            "seq": pending_view["seq"],
            "kind": pending_view["kind"],
            "check": pending_view["check"],
            "acked": pending_view["acked"],
            "answers": pending_view["answers"],
        }

    intent = latest_order_intent(store, order_id)
    last_intent = (
        None
        if intent is None
        else {
            "id": intent["id"],
            "kind": intent["payload"].get("kind"),
            "status": intent.get("execution_status"),
            "error": intent.get("execution_error") or None,
            "at": intent["payload"].get("created_at"),
        }
    )

    phase = current_phase(task)
    anchor = (
        str(checkpoint_data.get("resume_phase") or "")
        if phase in ("ask", "blocked")
        else phase
    )
    action = PHASE_ACTION.get(anchor)
    view = copy.deepcopy(task)
    if action is not None:
        coord(view)["phase"] = anchor
    facts = TaskFacts(store, view)
    checks = (
        []
        if action is None
        else [
            {
                "key": declared.key,
                "status": value.status,
                "source": value.source,
                "evidence": value.evidence,
            }
            for declared, value in action_checks(facts, view, action)
        ]
    )
    used_overrides = {
        requirement.check
        for declared_action in ACTIONS.values()
        for requirement in declared_action.requires
        if ORDER in requirement.kinds
    }
    overridable_checks = [
        {
            "key": key,
            "status": read_fact(facts, view, CHECKS[key]).status,
        }
        for key in sorted(OVERRIDABLE)
        if key in used_overrides
    ]

    summary = order_summary(task)
    return {
        **{key: summary[key] for key in (
            "order_id",
            "state",
            "phase",
            "round",
            "repo",
        )},
        "base": source.get("base") or None,
        **{key: summary[key] for key in ("title", "pr_url", "head")},
        "last_event": last_event,
        "checkpoint": pending_checkpoint,
        "last_intent": last_intent,
        "action": action,
        "checks": checks,
        "overridable_checks": overridable_checks,
        "round_limit": round_limit_view(checkpoint_data),
        "skipped_checks": skipped_checks_view(source),
        "assessment": assessment_view(store, order_id),
    }


def op_events(
    store: Store,
    home: Path,
    *,
    after: int | str | None = None,
    notify_session: str | None = None,
    wake_only: bool = False,
    unacked: bool = False,
) -> dict[str, Any]:
    cursor = int_param(after, "--after", minimum=0) or 0
    rows = []
    for row in own_order_events(store):
        payload = row["payload"]
        if int(payload.get("seq") or 0) <= cursor:
            continue
        if (
            notify_session is not None
            and payload.get("notify_session") != notify_session
        ):
            continue
        if wake_only and payload.get("wake") is not True:
            continue
        if unacked and payload.get("ack") is not None:
            continue
        rows.append(row)
    events = [event_view(row) for row in rows]
    return {
        "cursor": max((int(item["seq"] or 0) for item in events), default=cursor),
        "events": events,
    }


def op_ack(
    store: Store, home: Path, *, by: str, event_id: str
) -> dict[str, Any]:
    row = store.row("activity", event_id)
    if (
        row is None
        or row.get("_origin_device_id") != store.device_id()
        or row.get("type") != ORDER_EVENT
    ):
        raise OrderError(f"order event {event_id} not found")
    acknowledge_event(store, event_id, by=by, via="ack")
    return {"event": event_id, "acked": True}


def op_reply(
    store: Store, home: Path, *, by: str, order_id: str, body: str
) -> dict[str, Any]:
    task = need_order(store, order_id)
    checkpoint_data = coord(task)
    event_id = checkpoint_event_id(task)
    if (
        current_phase(task) not in ("ask", "blocked")
        or not reply_checkpoint_eligible(task)
        or event_id is None
    ):
        raise OrderError(
            f"order {order_id} has no pending question or resumable blocker"
        )
    failed = CHECKS.get(str(checkpoint_data.get("failed_check") or ""))
    if failed is not None and failed.on_no == "question:override":
        row = store.row("checklist_item", registry_row_id(order_id, failed.key))
        if row is not None and row.get("status") == "nein":
            raise OrderError(
                f"check {failed.key} blocks order {order_id}; "
                "use agent order override or agent order cancel"
            )
    if not isinstance(body, str) or not 1 <= len(body) <= 60000:
        raise OrderError("body must be 1..60000 characters")
    intent_id = insert_intent(
        store,
        task,
        "reply",
        by=by,
        answers=event_id,
        body=redact(body, limit=2000),
    )
    acknowledge_event(store, event_id, by=by, via="reply")
    return {"intent": intent_id, "order_id": order_id}


def op_override(
    store: Store,
    home: Path,
    *,
    by: str,
    order_id: str,
    check: str,
    reason: str,
) -> dict[str, Any]:
    declared_check = CHECKS.get(check)
    if declared_check is None:
        raise OrderError(f"unknown check {check}")
    if not declared_check.overridable:
        raise OrderError(f"check {check} cannot be overridden")
    task = need_order(store, order_id)
    state = order_state(task)
    if state in ENDED_STATES:
        raise OrderError(f"order {order_id} is already {state}")
    row = store.row("checklist_item", registry_row_id(order_id, check))
    status = row.get("status") if row is not None else None
    if status not in ("nein", "unavailable") and not (
        declared_check.override_pending and status in (None, "pending")
    ):
        raise OrderError(f"check {check} is not failed on order {order_id}")
    if check == "review_2":
        refusal = second_review_waiver_refusal(store, task)
        if refusal is not None:
            raise OrderError(f"check review_2 cannot be waived: {refusal}")
    require_reason(reason)
    event_id = checkpoint_event_id(task)
    intent_id = insert_intent(
        store,
        task,
        "override",
        by=by,
        answers=event_id,
        check=check,
        reason=reason,
    )
    if coord(task).get("failed_check") == check and event_id is not None:
        acknowledge_event(store, event_id, by=by, via="override")
    return {"intent": intent_id, "order_id": order_id, "check": check}


def op_cancel(
    store: Store,
    home: Path,
    *,
    by: str,
    order_id: str,
    reason: str,
) -> dict[str, Any]:
    task = need_order(store, order_id)
    state = order_state(task)
    if state in ENDED_STATES:
        raise OrderError(f"order {order_id} is already {state}")
    require_reason(reason)
    event_id = checkpoint_event_id(task)
    intent_id = insert_intent(
        store,
        task,
        "cancel",
        by=by,
        answers=event_id,
        reason=reason,
    )
    if event_id is not None:
        acknowledge_event(store, event_id, by=by, via="cancel")
    return {"intent": intent_id, "order_id": order_id}


def op_report(store, home, *, order_id) -> dict:
    from .order_report import build_order_report

    return build_order_report(store, need_order(store, order_id))


_ID = Param("order_id", "--id", "UUID", required=True)
_AFTER = Param("after", "--after", "CURSOR", kind="int")
_NOTIFY = Param("notify_session", "--notify-session", "ID")
_EVENT_KEYS = (
    "id",
    "seq",
    "order_id",
    "kind",
    "wake",
    "digest",
    "pr_url",
    "phase",
    "round",
    "check",
    "failed_kind",
    "notify_session",
    "at",
    "acked",
    "answers",
    "opinion",
)

OPERATIONS: tuple[Operation, ...] = (
    Operation(
        "submit",
        "write",
        (
            Param("repo", "--repo", "OWNER/NAME", required=True),
            Param("title", "--title", "TEXT", required=True),
            Param("body", "--body-file", "PATH", kind="body", required=True),
            Param("base", "--base", "BRANCH"),
            Param("worker", "--worker", "SESSION"),
            _NOTIFY,
            Param(
                "skip_checks",
                "--skip-check",
                "KEY --reason TEXT",
                kind="skips",
            ),
            Param("max_rounds", "--max-rounds", "N", kind="int"),
        ),
        fields=(
            "order_id",
            "repo",
            "state",
            "worker",
            "round_limit",
            "skipped_checks",
        ),
        nested=(
            ("round_limit", ROUND_LIMIT_KEYS),
            ("skipped_checks", SKIPPED_CHECK_KEYS),
        ),
        func=op_submit,
        identity=True,
        json_flag=True,
    ),
    Operation(
        "list",
        "read",
        (Param("open_only", "--open", kind="bool"),),
        fields=("orders",),
        nested=((
            "orders",
            (
                "order_id",
                "state",
                "phase",
                "round",
                "repo",
                "title",
                "pr_url",
                "head",
                "updated_at",
            ),
        ),),
        func=op_list,
        json_flag=True,
        json_root="orders",
    ),
    Operation(
        "show",
        "read",
        (_ID,),
        fields=(
            "order_id",
            "state",
            "phase",
            "round",
            "repo",
            "base",
            "title",
            "pr_url",
            "head",
            "last_event",
            "checkpoint",
            "last_intent",
            "action",
            "checks",
            "overridable_checks",
            "round_limit",
            "skipped_checks",
            "assessment",
        ),
        nested=(
            ("last_event", ("id", "seq", "kind", "at", "acked")),
            (
                "checkpoint",
                ("event_id", "seq", "kind", "check", "acked", "answers"),
            ),
            ("last_intent", ("id", "kind", "status", "error", "at")),
            ("checks", ("key", "status", "source", "evidence")),
            ("overridable_checks", ("key", "status")),
            ("round_limit", ROUND_LIMIT_KEYS),
            ("skipped_checks", SKIPPED_CHECK_KEYS),
            ("assessment", ASSESSMENT_KEYS),
        ),
        func=op_show,
        json_flag=True,
    ),
    Operation(
        "events",
        "read",
        (
            _AFTER,
            _NOTIFY,
            Param("wake_only", "--wake-only", kind="bool"),
            Param("unacked", "--unacked", kind="bool"),
        ),
        fields=("cursor", "events"),
        nested=(("events", _EVENT_KEYS),),
        func=op_events,
        json_flag=True,
    ),
    Operation(
        "wait",
        "read",
        (_AFTER, _NOTIFY, Param("timeout", "--timeout", "SECONDS", kind="int")),
        fields=("cursor", "events"),
        nested=(("events", _EVENT_KEYS),),
        cli_only="blocking poll of the events operation; a dashboard polls events",
    ),
    Operation(
        "ack",
        "write",
        (Param("event_id", "--event", "ID", required=True),),
        fields=("event", "acked"),
        func=op_ack,
        identity=True,
    ),
    Operation(
        "reply",
        "write",
        (_ID, Param("body", "--body-file", "PATH", kind="body", required=True)),
        fields=("intent", "order_id"),
        func=op_reply,
        identity=True,
    ),
    Operation(
        "override",
        "write",
        (
            _ID,
            Param("check", "--check", "KEY", required=True),
            Param("reason", "--reason", "TEXT", required=True),
        ),
        fields=("intent", "order_id", "check"),
        func=op_override,
        identity=True,
    ),
    Operation(
        "cancel",
        "write",
        (_ID, Param("reason", "--reason", "TEXT", required=True)),
        fields=("intent", "order_id"),
        func=op_cancel,
        identity=True,
    ),
    Operation(
        "report",
        "read",
        (_ID,),
        fields=(
            "order_id",
            "title",
            "state",
            "phase",
            "repo",
            "base",
            "pr_url",
            "head",
            "summary_en",
            "summary_de",
            "rounds",
            "waivers",
            "files",
            "reviews",
            "reviews_omitted",
            "comments",
            "comments_omitted",
            "a38_report",
            "local_checks",
            "ci",
        ),
        nested=(
            ("rounds", ("used", "limit", "max_rounds", "origin", "model")),
            ("waivers", ("check", "kind", "by", "reason", "at")),
            (
                "files",
                ("head", "base", "total", "added", "deleted", "listed", "omitted", "error"),
            ),
            ("files.listed", ("path", "added", "deleted")),
            (
                "reviews",
                ("stage", "round", "head", "provider", "model", "lanes", "verdict", "defects", "at"),
            ),
            (
                "comments",
                ("kind", "url", "head", "passes", "defects", "blocks_merge", "visible"),
            ),
            (
                "a38_report",
                ("result", "head", "url", "runs", "runs_omitted", "reason"),
            ),
            ("a38_report.runs", ("id", "name", "result", "exit_code")),
            ("local_checks", ("name", "result", "head", "ran_at")),
            ("ci", ("state", "head", "observed_at")),
        ),
        func=op_report,
        json_flag=True,
    ),
)


def run(
    name: str, store: Store, home: Path, params: dict[str, Any]
) -> dict[str, Any]:
    operation = next(
        (candidate for candidate in OPERATIONS if candidate.name == name), None
    )
    if operation is None or operation.func is None:
        raise OrderError("unknown order operation")
    declared = {param.name for param in operation.params}
    for key in params:
        if key not in declared:
            raise OrderError(f"unknown parameter {key}")
    for param in operation.params:
        value = params.get(param.name)
        if param.required and (
            param.name not in params
            or value is None
            or (value == "" and param.kind != "body")
        ):
            raise OrderError(f"{param.flag} is required")
    kwargs = dict(params)
    if operation.identity:
        login = store.meta("github_login")
        if not login:
            raise OrderError("device is not paired")
        kwargs["by"] = login.casefold()
    return operation.func(store, home, **kwargs)


def view_operations() -> dict[str, Any]:
    return {
        operation.name: {
            "mode": operation.mode,
            "method": "GET" if operation.mode == "read" else "POST",
            "path": f"/api/orders/{operation.name}",
            "params": [
                {
                    "name": param.name,
                    "flag": param.flag,
                    "kind": param.kind,
                    "required": param.required,
                }
                for param in operation.params
            ],
            "fields": list(operation.fields),
            "nested": {
                path: list(keys) for path, keys in operation.nested
            },
        }
        for operation in OPERATIONS
        if not operation.cli_only
    }
