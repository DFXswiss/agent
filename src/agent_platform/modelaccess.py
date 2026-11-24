"""Model access with fallback and visible quotas (ADR-080 sections 1, 5 and 6; twins of
`web/src/core/credentials/orgQuotas.ts`, `web/src/core/credentials/model.ts`, the settings section "Model access and
quotas", the counter of the situation view and its card `quota_choice`).

- `quotas --org`: `GET /v1/orgs/{org}/quotas` (members of the org): the caller's own subscription (its windows, the
  newest read per window as `quotas` shows them, since when it is not readable, the latest observed limit), the
  operator's subscriptions when the organization may use them (per subscription its label, state, whether its quota is
  readable, the used share of its fullest window, the reset, the latest observed limit and its end), whether DFX access
  over OpenRouter is available, and the summary (`usable`, `exhausted`, `next_release`). "Not readable" stays visible
  with the last observed limit time; nothing is estimated. Rows that do not have the web's form are left out; an
  answer without the summary, the two flags or the own part is `response_invalid`. A hub of an older release has no
  such route and answers `not_found`.
- `task model-consent --task`: `POST /v1/tasks/{task}/model-consent` (writers of the task's workgroup): the task may
  continue on DFX access while its owner's own subscription is used up (the card's "Continue with DFX access"; the
  hub audits it as `task.model_consent` and hands it to the task's sessions).
- `settings set --fallback on|off --fallback-platform on|off` (`fallback_body`): the two settings of the user,
  `fallback.own_to_dfx` (default off: an own subscription that is used up never moves to paid DFX access silently)
  and `fallback.operator_to_platform` (default on: from used-up operator subscriptions to DFX access over
  OpenRouter). A hub whose settings carry no `fallback` is of an older release and gets the body it knows unless an
  option asks for the setting.
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from agent_platform.account import Account, unverified
from agent_platform.billing import number
from agent_platform.commands import Context, Tree
from agent_platform.eventio import uuid_argument
from agent_platform.hub import segment

WINDOW_RE = re.compile(r"five_hour|seven_day|weekly:[A-Za-z0-9._-]{1,64}")
FALLBACK_DEFAULTS = {"own_to_dfx": False, "operator_to_platform": True}
"""The settings of ADR-080 section 1 when the hub has none stored."""
SUMMARY_KEYS = ("usable", "exhausted")
SUBSCRIPTION_TIMES = ("resets_at", "limited_at", "until", "fetched_at")


# --- quota rows (the user's own, `GET /v1/me/quotas`, and the own part of an org's) ---


def parsed_time(value: object) -> datetime | None:
    """An ISO time of an answer, None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def quota_bar(value: dict[str, Any]) -> dict[str, Any] | None:
    """One row as a bar, or None when it is malformed."""
    window, used = value.get("window"), value.get("used_percent")
    if not isinstance(window, str) or not WINDOW_RE.fullmatch(window):
        return None
    if isinstance(used, bool) or not isinstance(used, int | float) or not math.isfinite(used) or used < 0:
        return None
    fetched, resets = parsed_time(value.get("fetched_at")), value.get("resets_at")
    if fetched is None or (resets is not None and parsed_time(resets) is None):
        return None
    severity = value.get("severity")
    return {
        "window": window,
        "model": window.removeprefix("weekly:") if window.startswith("weekly:") else None,
        "used_percent": used,
        "resets_at": resets,
        "severity": severity if isinstance(severity, str) else None,
        "fetched_at": value["fetched_at"],
        "_fetched": fetched,
    }


def newest_bars(rows: list[Any]) -> list[dict[str, Any]]:
    """The newest bar per window of quota rows, ordered five hours, week, then by model; malformed rows left out."""
    newest: dict[str, dict[str, Any]] = {}
    for row in rows:
        bar = quota_bar(row) if isinstance(row, dict) else None
        if bar is None:
            continue
        known = newest.get(bar["window"])
        if known is None or known["_fetched"] < bar["_fetched"]:
            newest[bar["window"]] = bar

    def rank(bar: dict[str, Any]) -> tuple[int, str]:
        order = {"five_hour": 0, "seven_day": 1}
        return order.get(bar["window"], 2), bar["window"]

    return [{k: v for k, v in bar.items() if k != "_fetched"} for bar in sorted(newest.values(), key=rank)]


# --- settings, the org's model access, the task's consent ---


def fallback_body(current: object, own: str | None, platform: str | None) -> dict[str, bool] | None:
    """The `fallback` of a `PUT /v1/me/settings`: the given options over the stored settings over the defaults; None
    for a hub of an older release (no stored `fallback`) when no option asks for it."""
    stored = current.get("fallback") if isinstance(current, dict) else None
    if own is None and platform is None and not isinstance(stored, dict):
        return None
    known: dict[str, Any] = stored if isinstance(stored, dict) else {}

    def pick(name: str, given: str | None) -> bool:
        if given is not None:
            return given == "on"
        value = known.get(name)
        return value if isinstance(value, bool) else FALLBACK_DEFAULTS[name]

    return {"own_to_dfx": pick("own_to_dfx", own), "operator_to_platform": pick("operator_to_platform", platform)}


def _when(value: object) -> tuple[bool, str | None]:
    """`(well_formed, value)` of an optional time: null is well-formed, a malformed time is not."""
    if value is None:
        return True, None
    return (parsed_time(value) is not None, value if isinstance(value, str) else None)


def _record(value: object) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _limited(value: object) -> dict[str, Any] | None:
    """The latest observed limit `{at, until, readable}`, None when there is none or it is malformed."""
    found = _record(value)
    if found is None or not isinstance(found.get("readable"), bool):
        return None
    at, until = _when(found.get("at")), _when(found.get("until"))
    if not at[0] or at[1] is None or not until[0]:
        return None
    return {"at": at[1], "until": until[1], "readable": found["readable"]}


def subscription(value: object) -> dict[str, Any] | None:
    """One operator subscription as the web shows it, None when it is malformed."""
    row = _record(value)
    if row is None:
        return None
    label, state, readable, used = row.get("label"), row.get("state"), row.get("readable"), row.get("used_percent")
    if not isinstance(label, str) or not label or not isinstance(state, str) or not isinstance(readable, bool):
        return None
    if used is not None and (number(used) is None or used < 0):
        return None
    times = {name: _when(row.get(name)) for name in SUBSCRIPTION_TIMES}
    if not all(ok for ok, _ in times.values()):
        return None
    return {
        "label": label,
        "state": state,
        "readable": readable,
        "used_percent": used,
        **{name: found for name, (_ok, found) in times.items()},
    }


def org_quotas(account: Account, org: str) -> dict[str, Any]:
    answer = account.call("GET", f"/v1/orgs/{segment(uuid_argument(org))}/quotas")
    own, operator = _record(answer.get("own")), _record(answer.get("operator"))
    platform, summary = _record(answer.get("platform")), _record(answer.get("summary"))
    if own is None or operator is None or platform is None or summary is None:
        raise unverified("response_invalid")
    granted, available = operator.get("granted"), platform.get("available")
    release = _when(summary.get("next_release"))
    if (
        not isinstance(granted, bool)
        or not isinstance(available, bool)
        or not all(type(summary.get(key)) is int for key in SUMMARY_KEYS)
        or not release[0]
    ):
        raise unverified("response_invalid")
    windows = own.get("windows")
    unreadable = _record(own.get("unreadable"))
    since = None if unreadable is None else unreadable.get("since")
    listed = operator.get("subscriptions")
    made = (subscription(row) for row in (listed if isinstance(listed, list) else []))
    return {
        "org_id": org,
        "own": {
            "quotas": newest_bars(windows if isinstance(windows, list) else []),
            "unreadable_since": since if parsed_time(since) is not None else None,
            "limited": _limited(own.get("limited")),
        },
        "operator": {"granted": granted, "subscriptions": [row for row in made if row is not None]},
        "platform": {"available": available},
        "summary": {"usable": summary["usable"], "exhausted": summary["exhausted"], "next_release": release[1]},
    }


def model_consent(account: Account, task: str) -> dict[str, Any]:
    answer = account.call("POST", f"/v1/tasks/{segment(uuid_argument(task))}/model-consent", {})
    if answer.get("model_consent") is not True:
        raise unverified("response_invalid")
    return {"task_id": task, "model_consent": True}


def _consent(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return model_consent(account, args.task)


def register(tree: Tree) -> None:
    group = tree.group("task", "tasks (sealed and opened on this computer)")
    sub = tree.command(
        group,
        "model-consent",
        _consent,
        "let the task continue on DFX access while its owner's own subscription is used up",
        example="agent-platform task model-consent --task <task_id>",
        words=(
            "abo erschöpft kontingent weiterarbeiten dfx-zugang zustimmen modellzugang ausweichen quota exhausted "
            "continue consent fallback openrouter"
        ),
        then="agent-platform task follow --task <task_id>",
    )
    sub.add_argument("--task", required=True)
