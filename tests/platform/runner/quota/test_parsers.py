"""U-RUN-quota: the usage parsers copied from `agent_cli.usage` (05-runner.md sections 1.5 and 10.6, 00-overview.md
section 5 window mapping). Every result is checked against the `quota` outbox contract where it feeds one."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from agent_guest.schema import SchemaSet
from agent_runner.quota.parsers import UsageInvalid, spend_snapshot, windows_snapshot

from .double import KEY_DOCUMENT, USAGE_DOCUMENT

REPO = Path(__file__).resolve().parents[4]
OWNER = "01930003-0000-7000-8000-000000000001"


def quota_errors(windows: list[dict[str, Any]]) -> list[Any]:
    body = {"owner_id": OWNER, "windows": windows, "fetched_at": "2026-11-21T12:00:00.000Z"}
    return list(SchemaSet(REPO / "contracts").errors("outbox/quota.json", body))


def test_windows_of_the_usage_document_are_mapped() -> None:
    windows = windows_snapshot(USAGE_DOCUMENT)
    assert windows == [
        # rounded to the nearest minute (31 s round up), contract time form
        {"window": "five_hour", "used_percent": 12.0, "resets_at": "2026-11-21T17:00:00.000Z", "severity": None},
        {"window": "seven_day", "used_percent": 3.5, "resets_at": "2026-11-26T09:00:00.000Z", "severity": "normal"},
        {"window": "weekly:Opus", "used_percent": 21.0, "resets_at": "2026-11-26T09:00:00.000Z", "severity": "warning"},
    ]
    assert quota_errors(windows) == []


def test_weekly_falls_back_to_the_first_all_models_limit_and_models_are_named() -> None:
    document: dict[str, Any] = {
        "five_hour": {"utilization": None},
        "seven_day": None,
        "limits": [
            {"kind": "weekly_all", "percent": 40, "resets_at": "2026-11-26T09:00:29+01:00", "severity": "x" * 40},
            {"kind": "weekly_all", "percent": 50},
            {"kind": "weekly_scoped", "percent": 7, "scope": {"model": {"display_name": " ", "id": "claude sonnet/5"}}},
            {"kind": "weekly_scoped", "percent": 8, "scope": {"model": {"id": "claude sonnet/5"}}},  # same model
            {"kind": "weekly_scoped", "percent": 9, "scope": {"model": "opus"}},  # no model object: left out
            {"kind": "weekly_scoped", "percent": 9, "scope": None},
            {"kind": "weekly_scoped", "percent": 9, "scope": {"model": {"display_name": "", "id": 5}}},  # no name
            {"kind": "weekly_scoped", "percent": None, "scope": {"model": {"id": "m"}}},  # no value
            {"kind": "weekly_scoped", "percent": 1, "scope": {"model": {"id": "x" * 80}}},
        ],
    }
    windows = windows_snapshot(document)
    assert windows == [
        {"window": "seven_day", "used_percent": 40.0, "resets_at": "2026-11-26T08:00:00.000Z", "severity": None},
        {"window": "weekly:claude_sonnet_5", "used_percent": 7.0, "resets_at": None, "severity": None},
        {"window": "weekly:" + "x" * 64, "used_percent": 1.0, "resets_at": None, "severity": None},
    ]
    assert quota_errors(windows) == []


def test_no_window_and_the_list_limit() -> None:
    assert windows_snapshot({"limits": None}) == []
    assert windows_snapshot({}) == []
    many = {"limits": [{"kind": "weekly_scoped", "percent": n, "scope": {"model": {"id": f"m{n}"}}} for n in range(30)]}
    assert len(windows_snapshot(many)) == 16


def _broken(path: tuple[Any, ...], value: object) -> dict[str, Any]:
    document = copy.deepcopy(USAGE_DOCUMENT)
    target: Any = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    return document


@pytest.mark.parametrize(
    ("document", "field"),
    [
        ([], "document"),
        ({"limits": {}}, "limits"),
        ({"limits": ["x"]}, "limits[0]"),
        ({"limits": [{"kind": "weekly_all", "percent": "5"}]}, "limits[0].percent"),
        ({"limits": [{"kind": "weekly_all", "percent": -1}]}, "limits[0].percent"),
        ({"limits": [{"kind": "weekly_all", "percent": 1001}]}, "limits[0].percent"),
        ({"limits": [{"kind": "weekly_all", "percent": 1, "resets_at": 5}]}, "limits[0].resets_at"),
        (_broken(("five_hour",), "x"), "five_hour"),
        (_broken(("five_hour", "utilization"), True), "five_hour.utilization"),
        (_broken(("five_hour", "utilization"), float("nan")), "five_hour.utilization"),
        (_broken(("five_hour", "utilization"), 10**400), "five_hour.utilization"),
        (_broken(("seven_day", "resets_at"), ""), "seven_day.resets_at"),
        (_broken(("seven_day", "resets_at"), "tomorrow"), "seven_day.resets_at"),
        (_broken(("seven_day", "resets_at"), "2026-11-26T09:00:00"), "seven_day.resets_at"),  # no time zone
        (_broken(("seven_day", "resets_at"), "9999-12-31T23:59:59+00:00"), "seven_day.resets_at"),  # overflow
    ],
)
def test_malformed_usage_documents_name_the_field(document: object, field: str) -> None:
    with pytest.raises(UsageInvalid) as caught:
        windows_snapshot(document)
    assert caught.value.field == field


def test_spend_of_the_key_document() -> None:
    assert spend_snapshot(KEY_DOCUMENT) == {
        "total": 12.5,
        "daily": 0.75,
        "weekly": 3.25,
        "monthly": 9.0,
        "limit": 50.0,
        "limit_remaining": 37.5,
        "limit_reset": "monthly",
    }
    unlimited = copy.deepcopy(KEY_DOCUMENT)
    unlimited["data"]["limit"] = None
    del unlimited["data"]["limit_remaining"]
    del unlimited["data"]["limit_reset"]
    found = spend_snapshot(unlimited)
    assert (found["limit"], found["limit_remaining"], found["limit_reset"]) == (None, None, None)


def _key(**changes: object) -> dict[str, Any]:
    document = copy.deepcopy(KEY_DOCUMENT)
    for name, value in changes.items():
        if value is ...:
            del document["data"][name]
        else:
            document["data"][name] = value
    return document


@pytest.mark.parametrize(
    ("document", "field"),
    [
        (None, "data"),
        ({"data": []}, "data"),
        (_key(usage=...), "usage"),
        (_key(usage_daily="1"), "usage_daily"),
        (_key(usage_weekly=None), "usage_weekly"),
        (_key(limit="x"), "limit"),
        (_key(limit_remaining=float("inf")), "limit_remaining"),
        (_key(limit_reset={}), "limit_reset"),
        (_key(limit_reset=True), "limit_reset"),
    ],
)
def test_malformed_key_documents_name_the_field(document: object, field: str) -> None:
    with pytest.raises(UsageInvalid) as caught:
        spend_snapshot(document)
    assert caught.value.field == field
