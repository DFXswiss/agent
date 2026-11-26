"""Usage, audit, operations and metrics of one workgroup (KOR-53, ADR-095 sections 3 and 4): the arguments of the
session read tools `get_usage`, `list_audit`, `list_workgroup_operations` and `get_metrics` (03-api.md section 8.1)
and the hub's device routes `GET /v1/device/workgroups/{wg}/usage|audit|operations|metrics` (right `device:wg`,
03-api.md section 6.1).

- **Arguments.** The guest checked them against the tool schema, but the VM is not trusted (00-overview.md section 3),
  so they are checked again (`params_invalid`): times of the contract form, `since < until` and a period of at most
  `MAX_DAYS` days (`until` defaults to now), opaque cursors, closed value sets; `get_metrics` at most `MAX_POINTS`
  points (`ceil(period / step)`). A hub refusal `400 request_invalid` (for example a step below the rollup step of an
  old period) is `params_invalid` naming the argument of its first pointer.
- **Answers.** The tool checked that the session may read the workgroup (`common.workgroup`, ADR-089). The hub's
  answer still cannot widen the read (ADR-089 section 2): every row is checked against the tool's output contract and
  a row that does not match is dropped; an audit row must belong to the session's organization and to the read
  workgroup. An audit row's `detail` reaches the model only as flat tokens (ADR-095 section 3): keys `DETAIL_KEY_RE`,
  values integers, booleans, null or strings of `TOKEN_RE`; any other entry is dropped, and a `target_id` outside
  `TARGET_ID_RE` becomes null, so a hub cannot hand free text to the model through the audit. Lists are cut to the
  asked `limit` (then without a cursor: the rest is not reachable through the hub's cursor), groups to `MAX_GROUPS`
  (then `truncated`), series to `MAX_POINTS`. A usage or metrics frame that is not the one asked for (another
  workgroup, grouping, period or entity) makes the read `unavailable`.
- **Refusals.** `403`/`404` on a list is a workgroup without rows (the device's assignment may lag behind the log);
  on metrics it is `not_found` (the entity is not of the workgroup). An unreachable hub and any other refusal are
  `unavailable`. Every refusal names the read workgroup (audited `read_wg_id`).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

from agent_runner.queries.common import PARAMS_INVALID
from agent_runner.queries.device_api import (
    BEFORE_RE,
    HUB_UNAVAILABLE,
    OPERATION_STATES,
    OPERATION_TYPE_RE,
    SESSION_STATES,
    TIME_RE,
)
from agent_runner.queries.rights import ReadRefused
from agent_runner.queries.tools.list_operations import ERROR_CODES
from agent_runner.sessions.records import time_text
from agent_runner.sync.http import HubRefusal, HubTransportError
from agent_runner.sync.runtime import runtime

Json = dict[str, Any]
Check = Callable[[object], bool]
DEFAULT_LIMIT, MAX_LIMIT = 50, 200
MAX_GROUPS = 200
MAX_POINTS = 1000
MAX_DAYS = 92
MIN_STEP, MAX_STEP = 15, 9999999
COUNT_MAX = 9007199254740991
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
"""`common.json#/$defs/uuid` (UUIDv7)."""
ACTION_RE = re.compile(r"(?=.{1,96}$)[a-z_]+(\.[a-z_]+)*")
TARGET_KIND_RE = re.compile(r"[a-z_]{1,32}")
TARGET_ID_RE = re.compile(r"[A-Za-z0-9_.:/@+=-]{1,128}")
DETAIL_KEY_RE = re.compile(r"[a-z_]{1,64}")
TOKEN_RE = re.compile(r"[A-Za-z0-9_.:/@+=-]{0,128}")
MODEL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}")
ENTITY_ID_RES = {
    "session": UUID_RE,
    "vm": re.compile(r"vm-[0-9a-f]{8}"),
    "workvm": re.compile(r"wvm-[0-9a-f]{8}"),
}
BY = frozenset({"task", "session"})
ACTOR_KINDS = frozenset({"user", "device", "session", "hub", "operator", "anonymous"})
AUDIT_RESULTS = frozenset({"ok", "denied", "rejected", "duplicate", "failed"})
METRICS = frozenset(
    {
        "cpu_ratio",
        "mem_used_bytes",
        "mem_limit_bytes",
        "disk_used_bytes",
        "disk_limit_bytes",
        "net_rx_bytes",
        "net_tx_bytes",
        "uptime_seconds",
        "reachable",
        "free_vm_slots",
        "free_mem_bytes",
    }
)
FIGURES = (
    "requests",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_micro_usd",
    "charged_micro_usd",
    "runtime_ms",
    "operations",
    "failed_operations",
)
POINTERS = {
    "/since": "since",
    "/until": "until",
    "/by": "by",
    "/action": "action",
    "/task": "task_id",
    "/session": "session_id",
    "/type": "type",
    "/state": "state",
    "/group_by": "group_by",
    "/entity_kind": "entity_kind",
    "/entity_id": "entity_id",
    "/metric": "metric",
    "/step": "step",
    "/before": "before",
    "/limit": "limit",
}
"""The hub's query parameter pointers and the tool arguments they come from."""
HUB_INVALID = ReadRefused("unavailable", "The platform answered with data of another read.")
NO_ENTITY = ReadRefused("not_found", "There is no such session or VM of this workgroup you can read.")


# -- arguments -------------------------------------------------------------------------------------------------------


def _moment(text: object) -> datetime | None:
    if not isinstance(text, str) or TIME_RE.fullmatch(text) is None:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError:
        return None


def optional(found: Mapping[str, Any], name: str, valid: Check) -> Any:
    """The argument `name` when given and `valid`, None when absent; `params_invalid` otherwise (also for null)."""
    if name not in found:
        return None
    value = found[name]
    if not valid(value):
        raise PARAMS_INVALID
    return value


def pattern(regex: re.Pattern[str]) -> Check:
    return lambda value: isinstance(value, str) and regex.fullmatch(value) is not None


def one_of(values: frozenset[str]) -> Check:
    return lambda value: isinstance(value, str) and value in values


def now() -> datetime:
    return datetime.now(UTC)


def period(found: Mapping[str, Any]) -> tuple[str | None, str | None, datetime]:
    """`(since, until, end)` of the arguments: `end` is `until` or now; `since` before `end` and at most `MAX_DAYS`
    days before it (module docstring)."""
    since = optional(found, "since", lambda value: _moment(value) is not None)
    until = optional(found, "until", lambda value: _moment(value) is not None)
    end = now() if until is None else _moment(until)
    assert end is not None
    start = _moment(since)
    if start is not None and (start >= end or end - start > timedelta(days=MAX_DAYS)):
        raise PARAMS_INVALID
    return since, until, end


def cursor(found: Mapping[str, Any]) -> str | None:
    return optional(found, "before", pattern(BEFORE_RE))  # type: ignore[no-any-return]


def points(since: str, end: datetime, step: int) -> int:
    start = _moment(since)
    assert start is not None
    return math.ceil((end - start).total_seconds() / step)


# -- the hub's answer ------------------------------------------------------------------------------------------------


def _uuid(value: object) -> bool:
    return isinstance(value, str) and UUID_RE.fullmatch(value) is not None


def _time(value: object) -> bool:
    return isinstance(value, str) and TIME_RE.fullmatch(value) is not None


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= COUNT_MAX


def _positive(value: object) -> bool:
    return _count(value) and value != 0


def _or_null(check: Check) -> Check:
    return lambda value: value is None or check(value)


def _token(value: object) -> bool:
    if isinstance(value, str):
        return TOKEN_RE.fullmatch(value) is not None
    return value is None or isinstance(value, (bool, int))


def _number(value: object) -> bool:
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))


def checked(value: object, checks: Mapping[str, Check]) -> Json | None:
    """The fields `checks` of a row that has each of them in the contract's form, else None (other fields dropped)."""
    if not isinstance(value, Mapping) or not all(
        name in value and check(value[name]) for name, check in checks.items()
    ):
        return None
    return {name: value[name] for name in checks}


FIGURE_CHECKS: dict[str, Check] = dict.fromkeys(FIGURES, _count)
TOTAL_CHECKS: dict[str, Check] = {"sessions": _count, "tasks": _count, **FIGURE_CHECKS}
USAGE_CHECKS: dict[str, dict[str, Check]] = {
    "task": {"task_id": _uuid, "created_at": _time, "sessions": _count, **FIGURE_CHECKS},
    "session": {
        "session_id": _uuid,
        "task_id": _uuid,
        "parent_session_id": _or_null(_uuid),
        "depth": _count,
        "state": one_of(SESSION_STATES),
        "model_id": _or_null(pattern(MODEL_ID_RE)),
        "created_at": _time,
        "ended_at": _or_null(_time),
        **FIGURE_CHECKS,
    },
}
AUDIT_CHECKS: dict[str, Check] = {
    "seq": _positive,
    "at": _time,
    "actor_kind": one_of(ACTOR_KINDS),
    "actor_id": _or_null(_uuid),
    "action": pattern(ACTION_RE),
    "result": one_of(AUDIT_RESULTS),
    "task_id": _or_null(_uuid),
    "session_id": _or_null(_uuid),
    "target_kind": _or_null(pattern(TARGET_KIND_RE)),
}
AUDIT_GROUP_CHECKS: dict[str, Check] = {"action": pattern(ACTION_RE), "result": one_of(AUDIT_RESULTS), "count": _count}
OPERATION_CHECKS: dict[str, Check] = {
    "operation_id": _uuid,
    "task_id": _uuid,
    "session_id": _uuid,
    "type": pattern(OPERATION_TYPE_RE),
    "state": one_of(OPERATION_STATES),
    "error_code": _or_null(one_of(ERROR_CODES)),
    "created_at": _time,
    "finished_at": _or_null(_time),
}
OPERATION_GROUP_CHECKS: dict[str, Check] = {
    "type": pattern(OPERATION_TYPE_RE),
    "state": one_of(OPERATION_STATES),
    "error_code": _or_null(one_of(ERROR_CODES)),
    "count": _count,
}


def audit_row(value: object, org_id: str, wg_id: str) -> Json | None:
    """An audit row of the organization and workgroup with its detail reduced to flat tokens, else None."""
    if not isinstance(value, Mapping) or value.get("org_id") != org_id or value.get("wg_id") != wg_id:
        return None
    row, detail, target = checked(value, AUDIT_CHECKS), value.get("detail"), value.get("target_id")
    if row is None or not isinstance(detail, Mapping):
        return None
    row["target_id"] = target if isinstance(target, str) and TARGET_ID_RE.fullmatch(target) else None
    row["detail"] = {key: item for key, item in detail.items() if DETAIL_KEY_RE.fullmatch(key) and _token(item)}
    return row


def _listed(answer: Mapping[str, Any], key: str) -> list[Any]:
    found = answer.get(key)
    return found if isinstance(found, list) else []


def _page(answer: Mapping[str, Any], key: str, row: Callable[[object], Json | None], limit: int) -> Json:
    rows = [found for found in map(row, _listed(answer, key)) if found is not None]
    if len(rows) > limit:
        # a hub answering more rows than asked: the rest is not reachable through its cursor, so none is given
        return {key: rows[:limit], "next_before": None}
    following = answer.get("next_before")
    return {key: rows, "next_before": following if pattern(BEFORE_RE)(following) else None}


def _groups(answer: Mapping[str, Any], checks: Mapping[str, Check]) -> Json:
    groups = [found for found in (checked(item, checks) for item in _listed(answer, "groups")) if found is not None]
    truncated = len(groups) > MAX_GROUPS or answer.get("truncated") is not False
    return {"groups": groups[:MAX_GROUPS], "truncated": truncated}


async def _fetch(context: Any, wg_id: str, resource: str, query: Mapping[str, str | None]) -> Json | None:
    """The hub's answer of `GET /v1/device/workgroups/{wg}/<resource>`; None for `403`/`404` (module docstring)."""
    sync = runtime(context)
    if sync is None:
        raise HUB_UNAVAILABLE.at(wg_id)
    given = {name: value for name, value in query.items() if value is not None}
    target = f"/v1/device/workgroups/{quote(wg_id)}/{resource}?{urlencode(given)}"
    try:
        return await sync.client.request("GET", target)
    except HubRefusal as exc:
        if exc.status in (403, 404):
            return None
        if exc.status == 400 and exc.code == "request_invalid":
            raise refused_arguments(exc.detail).at(wg_id) from None
        raise HUB_UNAVAILABLE.at(wg_id) from None
    except HubTransportError:
        raise HUB_UNAVAILABLE.at(wg_id) from None


def refused_arguments(detail: Mapping[str, object]) -> ReadRefused:
    """`params_invalid` for a hub's `400 request_invalid`, naming the argument of its first known pointer."""
    pointers = detail.get("pointers")
    known = (
        [POINTERS[p] for p in pointers if isinstance(p, str) and p in POINTERS] if isinstance(pointers, list) else []
    )
    if not known:
        return ReadRefused("params_invalid", "The platform refused the arguments of this read.")
    return ReadRefused("params_invalid", f"The platform refused the argument {known[0]} of this read.")


# -- the four reads --------------------------------------------------------------------------------------------------


async def usage(
    context: Any, wg_id: str, *, since: str, until: str | None, by: str, before: str | None, limit: int
) -> Json:
    """The `get_usage` result (module docstring)."""
    query = {"since": since, "until": until, "by": by, "before": before, "limit": str(limit)}
    answer = await _fetch(context, wg_id, "usage", query)
    if answer is None:
        end = until if until is not None else time_text(now().timestamp())
        return {
            "workgroup_id": wg_id,
            "since": since,
            "until": end,
            "by": by,
            "total": None,
            "rows": [],
            "next_before": None,
        }
    frame = (answer.get("workgroup_id"), answer.get("by"), answer.get("since"))
    hub_until = answer.get("until")
    if frame != (wg_id, by, since) or not _time(hub_until) or until not in (None, hub_until):
        raise HUB_INVALID.at(wg_id)
    total = None if before is not None else checked(answer.get("total"), TOTAL_CHECKS)
    page = _page(answer, "rows", lambda value: checked(value, USAGE_CHECKS[by]), limit)
    return {"workgroup_id": wg_id, "since": since, "until": hub_until, "by": by, "total": total, **page}


async def audit(
    context: Any,
    org_id: str,
    wg_id: str,
    *,
    query: Mapping[str, str | None],
    grouped: bool,
    limit: int,
) -> Json:
    """The `list_audit` result; `query` the hub's parameters without `group_by` and `limit` (module docstring)."""
    answer = await _fetch(
        context, wg_id, "audit", {**query, "group_by": "action" if grouped else None, "limit": str(limit)}
    )
    if grouped:
        return _groups(answer or {"truncated": False}, AUDIT_GROUP_CHECKS)
    return _page(answer or {}, "rows", lambda value: audit_row(value, org_id, wg_id), limit)


async def operations(context: Any, wg_id: str, *, query: Mapping[str, str | None], grouped: bool, limit: int) -> Json:
    """The `list_workgroup_operations` result; `query` the hub's parameters without `group_by` and `limit`."""
    answer = await _fetch(
        context, wg_id, "operations", {**query, "group_by": "type" if grouped else None, "limit": str(limit)}
    )
    if grouped:
        return _groups(answer or {"truncated": False}, OPERATION_GROUP_CHECKS)
    return _page(answer or {}, "operations", lambda value: checked(value, OPERATION_CHECKS), limit)


def point(value: object) -> bool:
    """A series point `[bucket start, finite number or null]`."""
    return isinstance(value, list) and len(value) == 2 and _time(value[0]) and _number(value[1])


async def metrics(context: Any, wg_id: str, *, query: Mapping[str, str | None]) -> Json:
    """The `get_metrics` result; `query` the hub's parameters (module docstring)."""
    answer = await _fetch(context, wg_id, "metrics", query)
    if answer is None:
        raise NO_ENTITY.at(wg_id)
    asked = (query["entity_kind"], query["entity_id"], query["metric"])
    if (answer.get("entity_kind"), answer.get("entity_id"), answer.get("metric")) != asked:
        raise HUB_INVALID.at(wg_id)
    found = [list(item) for item in _listed(answer, "points") if point(item)]
    return {"entity_kind": asked[0], "entity_id": asked[1], "metric": asked[2], "points": found[:MAX_POINTS]}
