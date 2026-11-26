"""U-HUB-analysis (request): the `analysis` of `POST /v1/tasks` checked against the hub's time - scope, period
defaults and bounds, the stored record, the fields a repetition must repeat and the profile snapshot (ADR-095
sections 1 and 2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent_hub.analysis import request
from agent_hub.errors import HubError
from agent_hub.profiles.model import BUILTIN, REACH_ALL, REACH_WORKGROUP

NOW = datetime(2026, 11, 21, 12, 0, 0, 123000, tzinfo=UTC)


def _pointer(value: dict[str, Any]) -> str:
    with pytest.raises(HubError) as refused:
        request.parse(value, NOW)
    assert refused.value.code == "request_invalid"
    detail = refused.value.detail
    assert detail is not None
    [pointer] = detail["pointers"]
    return str(pointer)


def test_defaults_are_the_last_seven_days_up_to_now() -> None:
    found = request.parse({"scope": "wg"}, NOW)
    assert (found.scope, found.since, found.until) == ("wg", NOW - timedelta(days=7), NOW)
    assert found.stored() == {"scope": "wg", "since": "2026-11-14T12:00:00.123Z", "until": "2026-11-21T12:00:00.123Z"}
    assert found.given({"scope": "wg"}) == {"scope": "wg"}


def test_explicit_times_are_kept_and_named_for_a_repetition() -> None:
    value = {"scope": "org", "since": "2026-08-21T12:01:00.123Z", "until": "2026-11-21T12:01:00.123Z"}
    found = request.parse(value, NOW)
    assert found.stored() == value
    assert found.given(value) == value
    only_until = request.parse({"scope": "org", "until": "2026-11-20T00:00:00.000Z"}, NOW)
    assert only_until.since == datetime(2026, 11, 13, tzinfo=UTC)
    assert only_until.given({"scope": "org", "until": "x"}) == {"scope": "org", "until": "2026-11-20T00:00:00.000Z"}


def test_the_profile_reaches_the_organization_or_only_the_workgroup() -> None:
    org = request.parse({"scope": "org"}, NOW).profile()
    wg = request.parse({"scope": "wg"}, NOW).profile()
    assert (org.name, org.operations, org.reads) == ("analysis", BUILTIN["analysis"].operations, REACH_ALL)
    assert (wg.name, wg.operations, wg.reads) == ("analysis", BUILTIN["analysis"].operations, REACH_WORKGROUP)


@pytest.mark.parametrize(
    ("value", "pointer"),
    [
        ({}, "/analysis/scope"),
        ({"scope": "device"}, "/analysis/scope"),
        ({"scope": None}, "/analysis/scope"),
        ({"scope": "wg", "days": 7, "zone": "x"}, "/analysis/days"),
        ({"scope": "wg", "until": "2026-11-21T12:01:00.124Z"}, "/analysis/until"),
        ({"scope": "wg", "until": "2026-11-21"}, "/analysis/until"),
        ({"scope": "wg", "since": 1700000000}, "/analysis/since"),
        ({"scope": "wg", "since": "2026-11-21T12:00:00.123Z"}, "/analysis/since"),
        ({"scope": "wg", "since": "2026-11-21T12:00:00.124Z"}, "/analysis/since"),
        ({"scope": "wg", "since": "2026-08-21T12:00:00.122Z"}, "/analysis/since"),
        ({"scope": "wg", "since": "2026-11-01T00:00:00.000Z", "until": "2026-10-01T00:00:00.000Z"}, "/analysis/since"),
    ],
)
def test_invalid_analyses_name_their_field(value: dict[str, Any], pointer: str) -> None:
    assert _pointer(value) == pointer


def test_the_longest_period_and_the_slack_into_the_future_are_allowed() -> None:
    value = {"scope": "wg", "since": "2026-08-21T12:01:00.123Z", "until": "2026-11-21T12:01:00.123Z"}
    found = request.parse(value, NOW)
    assert found.until - found.since == timedelta(days=request.MAX_DAYS)
