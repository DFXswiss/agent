"""The order command line, generated from ``order_ops.OPERATIONS``.

This module holds no rule about orders.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any

from . import order_ops
from .coordinator_checks import one_line
from .order_report import render_order_report


USAGE = "Usage: agent order " + "|".join(
    operation.name for operation in order_ops.OPERATIONS
)


def usage_line(operation: order_ops.Operation) -> str:
    parts = ["Usage:", "agent", "order", operation.name]
    for param in operation.params:
        part = param.flag if param.kind == "bool" else f"{param.flag} {param.meta}"
        if param.kind == "skips":
            parts.append(f"[{part}]...")
        else:
            parts.append(part if param.required else f"[{part}]")
    if operation.json_flag:
        parts.append("[--json]")
    return " ".join(parts)


def parse_order_args(
    operation: order_ops.Operation, rest: list[str], cli: Any
) -> tuple[dict[str, Any], bool]:
    params: dict[str, Any] = {}
    as_json = False
    by_flag = {param.flag: param for param in operation.params}
    skips_param = next(
        (param for param in operation.params if param.kind == "skips"), None
    )
    index = 0
    while index < len(rest):
        argument = rest[index]
        if argument == "--json" and operation.json_flag and not as_json:
            as_json = True
            index += 1
            continue
        param = by_flag.get(argument)
        if param is not None and param.kind == "skips":
            if index + 3 >= len(rest) or rest[index + 2] != "--reason":
                cli.die(f"{param.flag} KEY must be followed by --reason TEXT")
            params.setdefault(param.name, []).append(
                {"key": rest[index + 1], "reason": rest[index + 3]}
            )
            index += 4
            continue
        if (
            argument == "--reason"
            and skips_param is not None
            and "--reason" not in by_flag
        ):
            cli.die(f"--reason must follow {skips_param.flag} KEY")
        if param is None or param.name in params:
            cli.die(usage_line(operation))
        if param.kind == "bool":
            params[param.name] = True
            index += 1
            continue
        if index + 1 >= len(rest):
            cli.die(f"{param.flag} needs a value")
        params[param.name] = rest[index + 1]
        index += 2
    return params, as_json


def _json_line(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _render_submit(result: dict[str, Any]) -> list[str]:
    return [f"order {result['order_id']}"]


def _render_list(result: dict[str, Any]) -> list[str]:
    if not result["orders"]:
        return ["order list none"]
    return [
        "  ".join(
            (
                str(item["order_id"]),
                str(item["state"]),
                str(item["phase"]),
                f"r{item['round']}",
                str(item["repo"]),
                str(item["pr_url"] or "-"),
                one_line(item["title"], 80),
            )
        )
        for item in result["orders"]
    ]


def _render_show(result: dict[str, Any]) -> list[str]:
    lines = [
        f"order {result['order_id']}",
        f"state {result['state']} phase {result['phase']} round {result['round']}",
        f"repo {result['repo']} base {result['base'] or '-'}",
        f"pr {result['pr_url'] or '-'}",
        f"head {result['head'] or '-'}",
    ]
    round_limit = result["round_limit"]
    if round_limit is None:
        lines.append("round-limit -")
    else:
        origin = str(round_limit.get("origin") or "-")
        model = (
            f" {round_limit.get('model')}"
            if origin == "model" and round_limit.get("model")
            else ""
        )
        lines.append(
            f"round-limit {round_limit.get('limit')} "
            f"per-grant {round_limit.get('max_rounds')} origin {origin}{model}"
        )
    lines.extend(
        f"skipped {item.get('key')} by {item.get('by')}: "
        f"{one_line(item.get('reason') or '', 160)}"
        for item in result["skipped_checks"]
    )
    last_event = result["last_event"]
    if last_event is None:
        lines.append("last-event -")
    else:
        acknowledgement = "acked" if last_event["acked"] else "unacked"
        lines.append(
            f"last-event {last_event['seq']} {last_event['kind']} "
            f"{last_event['at']} {acknowledgement}"
        )
    checkpoint = result["checkpoint"]
    if checkpoint is None:
        lines.append("checkpoint -")
    else:
        acknowledgement = "acked" if checkpoint["acked"] else "unacked"
        answers = ",".join(checkpoint["answers"]) or "-"
        lines.append(
            f"checkpoint {checkpoint['seq']} {checkpoint['event_id']} "
            f"{checkpoint['kind']} {acknowledgement} answers {answers}"
        )
    last_intent = result["last_intent"]
    if last_intent is None:
        lines.append("last-answer -")
    else:
        error = one_line(last_intent["error"] or "", 160) or "-"
        lines.append(
            f"last-answer {last_intent['kind']} {last_intent['status']} {error}"
        )
    lines.append(f"action {result['action'] or '-'}")
    lines.extend(
        f"check {item['key']} {item['status']} "
        f"{one_line(item['evidence'] or '', 160) or '-'}"
        for item in result["checks"]
    )
    lines.extend(
        f"overridable {item['key']} {item['status']}"
        for item in result["overridable_checks"]
    )
    assessment = result["assessment"]
    if assessment is not None:
        reasoning = one_line(assessment.get("reasoning") or "", 160) or "-"
        lines.append(
            f"assessment {assessment.get('result') or '-'} "
            f"at-limit {assessment.get('limit') or '-'}: {reasoning}"
        )
        if assessment.get("result") == "change-approach":
            lines.append(
                f"approach {one_line(assessment.get('approach') or '', 300)}"
            )
    return lines


def _render_events(result: dict[str, Any]) -> list[str]:
    if not result["events"]:
        return ["order events none"]
    lines = []
    for event in result["events"]:
        first_line = str(event["digest"] or "").splitlines()
        digest = one_line(first_line[0] if first_line else "", 120) or "-"
        lines.append(
            f"event {event['seq']} {event['id']} {event['kind']} "
            f"order {event['order_id']} "
            f"wake={'true' if event['wake'] else 'false'} "
            f"acked={'true' if event['acked'] else 'false'} {digest}"
        )
    return lines


def _render_ack(result: dict[str, Any]) -> list[str]:
    return [f"acked {result['event']}"]


def _render_reply(result: dict[str, Any]) -> list[str]:
    return [f"reply {result['intent']} order {result['order_id']}"]


def _render_override(result: dict[str, Any]) -> list[str]:
    return [
        f"override {result['intent']} order {result['order_id']} "
        f"check {result['check']}"
    ]


def _render_cancel(result: dict[str, Any]) -> list[str]:
    return [f"cancel {result['intent']} order {result['order_id']}"]


RENDER: dict[str, Callable[[dict[str, Any]], list[str]]] = {
    "submit": _render_submit,
    "list": _render_list,
    "show": _render_show,
    "events": _render_events,
    "ack": _render_ack,
    "reply": _render_reply,
    "override": _render_override,
    "cancel": _render_cancel,
    "report": render_order_report,
}


def run_order_command(args: list[str]) -> None:
    from . import main as cli

    if not args:
        cli.die(USAGE)
    operation = next(
        (candidate for candidate in order_ops.OPERATIONS if candidate.name == args[0]),
        None,
    )
    if operation is None:
        cli.die(USAGE)
    params, as_json = parse_order_args(operation, args[1:], cli)
    for param in operation.params:
        if param.kind == "body" and param.name in params:
            params[param.name] = cli._read_body_file(params[param.name])

    home = cli.home()
    store = cli.open_store()
    try:
        if operation.name == "wait":
            after = order_ops.int_param(
                params.get("after"), "--after", minimum=0
            ) or 0
            timeout = order_ops.int_param(
                params.get("timeout"), "--timeout", minimum=1
            )
            query: dict[str, Any] = {
                "after": after,
                "wake_only": True,
                "unacked": True,
            }
            if params.get("notify_session") is not None:
                query["notify_session"] = params["notify_session"]
            deadline = None if timeout is None else time.monotonic() + timeout
            while True:
                interval = order_ops.wait_interval(home)
                result = order_ops.run("events", store, home, query)
                if result["events"]:
                    print(_json_line(result), flush=True)
                    return
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        print(_json_line(result), flush=True)
                        raise SystemExit(3)
                    interval = min(interval, remaining)
                time.sleep(interval)

        result = order_ops.run(operation.name, store, home, params)
        if as_json:
            value = result[operation.json_root] if operation.json_root else result
            print(_json_line(value))
        else:
            for line in RENDER[operation.name](result):
                print(line)
    finally:
        store.close()
