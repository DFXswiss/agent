"""Operations of the device double of the web suite (WEB-09).

The runner (RUN-07 registration, RUN-09/RUN-10 executors) is not part of the web Playwright suite. The director plays
the session's device through the hub's device API, signed with the device key, as the runner does it:

- `POST /sessions/<id>/operations {"type", "params", "outcome"?, "dedupe"?: bool, "dedupe_extra"?}`: the
  `operation.params` event is sealed with the workgroup key the browser wrapped for the device (`ref` = the
  operation, header naming workgroup, task and session) and registered with `POST /v1/device/operations`
  (`local_check: "passed"`, with the dedupe tag of 04-crypto.md section 4.10 for types that have dedupe fields unless
  `dedupe` is false; `dedupe_extra` adds the values the runner derives, e.g. `parent_sha`/`tree_sha` of a commit).
  An accepted operation with an `outcome` (`{"state": "succeeded", "result"}` or `{"state": "failed", "error"}`) is
  finished through the outbox (`POST /v1/device/outbox`): `executing`, the encrypted `operation.result` event, the
  final transition - for `task.set_title` also the device-authored `task.meta` with the new title, for an accepted
  `subagent.request` the result names the child session the hub created. Answers the registration (`state`,
  `duplicate_of`, `child_session_id`) with `operation_id`.

Only loopback, only for tests.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
from psycopg_pool import ConnectionPool

from agent_hub.crypto import event as event_format
from agent_hub.crypto.dedupe import DEDUPE_TYPES, dedupe_fields, dedupe_tag
from agent_hub.crypto.encoding import format_time
from agent_hub.crypto.ids import uuid7
from agent_hub.crypto.kdf import dedupe_key
from agent_hub.crypto.keys import Identity
from agent_hub.crypto.wrap import unwrap_key
from agent_hub.wraps.rules import wraps_for

Json = dict[str, Any]
Pool = ConnectionPool[psycopg.Connection[Any]]
Signed = Callable[[str, str, str, Json | None], httpx.Response]


def now() -> datetime:
    return datetime.now(UTC)


class Sealer:
    """Seals events of one session's device with the newest workgroup key it holds."""

    def __init__(self, pool: Pool, identity_of: Callable[[str], Identity], session_id: str) -> None:
        with pool.connection() as connection:
            row = connection.execute(
                "SELECT s.org_id, s.wg_id, s.task_id, s.device_id, o.log_head_seq FROM hub.session s"
                " JOIN hub.org o ON o.id = s.org_id WHERE s.id = %s",
                (session_id,),
            ).fetchone()
            if row is None or row[3] is None:
                raise RuntimeError("the session does not exist or has no device")
            self.org, self.wg, self.task, self.device = (str(value) for value in row[:4])
            self.lh = int(row[4])
            wraps = wraps_for(connection, self.device, scope="wg", scope_id=self.wg)
        if not wraps:
            raise RuntimeError("the device holds no key of the workgroup")
        newest = max(wraps, key=lambda wrap: int(wrap["epoch"]))
        self.identity = identity_of(self.device)
        self.session = session_id
        self.epoch = int(newest["epoch"])
        self.key = unwrap_key(
            self.identity.kem,
            scope="wg",
            scope_id=self.wg,
            epoch=self.epoch,
            recipient=self.device,
            wrap=newest["wrap"],
        )

    def seal(self, kind: str, body: Json, *, ref: str = "", session: str | None = None) -> Json:
        fields = event_format.new_header(
            id=str(uuid7()),
            org=self.org,
            scope="wg",
            scope_id=self.wg,
            epoch=self.epoch,
            author=self.device,
            kind=kind,
            ts=format_time(now()),
            lh=self.lh,
            wg=self.wg,
            task=self.task,
            session=self.session if session is None else session,
            ref=ref,
        )
        plaintext = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return event_format.seal_event(self.key, fields, plaintext, self.identity.sig)


def _push(pool: Pool, signed: Signed, device: str, items: list[tuple[str, Json]]) -> None:
    """One outbox push of `items` (`(item_kind, body)`) after the device's stream position; waits out the pacing."""
    with pool.connection() as connection:
        row = connection.execute(
            "SELECT last_origin_seq FROM hub.device_stream WHERE device_id = %s", (device,)
        ).fetchone()
    last = 0 if row is None else int(row[0])
    body = {
        "items": [
            {"origin_seq": last + index + 1, "item_kind": kind, "body": item}
            for index, (kind, item) in enumerate(items)
        ]
    }
    for _attempt in range(10):
        answer = signed(device, "POST", "/v1/device/outbox", body)
        if answer.status_code == 429:
            time.sleep(1.0)
            continue
        if answer.status_code != 200 or any(item.get("status") == "rejected" for item in answer.json()["results"]):
            raise RuntimeError(f"outbox refused: {answer.text}")
        return
    raise RuntimeError("outbox kept refusing the pace")


def operation(pool: Pool, signed: Signed, identity_of: Callable[[str], Identity], session_id: str, spec: Json) -> Json:
    """Registers (and finishes) one operation of the session (module docstring)."""
    sealer = Sealer(pool, identity_of, session_id)
    op_type = str(spec["type"])
    params: Json = dict(spec.get("params", {}))
    operation_id = str(uuid7())
    params_event = sealer.seal(
        "operation.params", {"operation_id": operation_id, "type": op_type, "params": params}, ref=operation_id
    )
    dedupe = None
    if op_type in DEDUPE_TYPES and spec.get("dedupe", True):
        # values the runner derives (e.g. the parent and tree SHA of a commit) come with the spec
        fields = dedupe_fields(op_type, {**params, **dict(spec.get("dedupe_extra", {})), "session": session_id})
        dedupe = {"epoch": sealer.epoch, "tag": dedupe_tag(dedupe_key(sealer.key), op_type, fields)}
    registration = {
        "operation_id": operation_id,
        "session_id": session_id,
        "task_id": sealer.task,
        "wg_id": sealer.wg,
        "type": op_type,
        "requested_at": format_time(now()),
        "local_check": "passed",
        "dedupe": dedupe,
        "params_event": params_event,
    }
    answer = signed(sealer.device, "POST", "/v1/device/operations", registration)
    if answer.status_code != 200:
        raise RuntimeError(f"registration refused: {answer.text}")
    decided: Json = {"operation_id": operation_id, **answer.json()}
    outcome = spec.get("outcome")
    if decided["state"] != "accepted" or not isinstance(outcome, dict):
        return decided
    items: list[tuple[str, Json]] = [
        ("operation_transition", {"operation_id": operation_id, "state": "executing", "at": format_time(now())})
    ]
    state = str(outcome["state"])
    if state == "succeeded":
        result = dict(outcome.get("result", {}))
        if op_type == "subagent.request":
            result = {"session_id": decided["child_session_id"]}
        if op_type == "task.set_title":
            items.append(("event", {"event": sealer.seal("task.meta", {"title": params["title"]}, session="")}))
        body: Json = {"operation_id": operation_id, "status": "succeeded", "result": result}
    else:
        body = {"operation_id": operation_id, "status": state, "error": dict(outcome["error"])}
    result_event = sealer.seal("operation.result", body, ref=operation_id)
    items.append(("event", {"event": result_event}))
    final: Json = {"operation_id": operation_id, "state": state, "at": format_time(now())}
    final["result_event_id"] = result_event["header"]["id"]
    if state == "failed":
        final.update(error_code=outcome["error"]["code"], retryable=bool(outcome["error"].get("retryable", False)))
    items.append(("operation_transition", final))
    _push(pool, signed, sealer.device, items)
    return {**decided, "state": state}
