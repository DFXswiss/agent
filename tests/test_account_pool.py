"""Choosing a pool account when a coordinator lane starts, and reusing it."""

from __future__ import annotations

import copy
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from agent_cli.account_pool import (
    LANE_ACCOUNT_CHOICE_FIELD,
    LANE_ACCOUNT_FIELD,
    recorded_lane_role,
    select_lane_account,
)
from agent_cli.ai_accounts import (
    POOL_ROLE_NEEDS_COORDINATOR,
    AccountError,
    AIAccounts,
    load_ai_accounts,
)
from agent_cli.lane import launch
from agent_cli.store import Store
from test_cli import run
from test_run import _agents, _bootstrap_implement
from driver_fixtures import (
    WINDOWS_ACCOUNT_FILE,
    accounts_document,
    driver_document,
    windows_usage,
    write_accounts,
    write_drivers,
)

POOL_DRIVER = "driver-p"
POOL_ROLE = "pooled"
SEATS = ("seat-1", "seat-2", "seat-3")
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
LATER = "2026-11-20T00:00:00+00:00"
EARLIER = "2026-11-17T11:00:00+00:00"
# Snapshots are stored only on change, so an old fetch time is still current.
OLD_FETCH = "2026-01-01T00:00:00+00:00"


def login_of(seat: str) -> str:
    return f"{seat}@example.com"


def pool_driver_document() -> dict[str, Any]:
    """The placeholder drivers plus ``driver-p``: a lane driver with a windows adapter."""
    document = driver_document()
    driver = copy.deepcopy(document["drivers"]["driver-c"])
    usage_section = windows_usage()
    usage_section["account_file"].pop("default_name")
    driver.pop("home_default_dir")
    driver.update(
        display_name="Vendor P",
        home_env="TOOL_P_CONFIG",
        credential_env=["TOOL_P_API_KEY"],
        usage=usage_section,
    )
    document["drivers"][POOL_DRIVER] = driver
    return document


def configure(home: Path, logins: Path, seats: tuple[str, ...] = SEATS) -> AIAccounts:
    """Write a configuration whose ``pooled`` role pools ``seats``."""
    write_drivers(home, pool_driver_document())
    data = accounts_document()
    for seat in seats:
        data["accounts"][seat] = {
            "provider": POOL_DRIVER,
            "config_dir": str(logins / seat),
        }
    data["usage_accounts"] = list(seats)
    data["roles"][POOL_ROLE] = {
        "accounts": list(seats),
        "model": "driver-p-model",
        "access": "workspace-write",
    }
    data["lanes"][f"{POOL_DRIVER}:implementer"] = POOL_ROLE
    write_accounts(home, data)
    return load_ai_accounts(home)


def write_login(config_dir: Path, login: str) -> None:
    """The login identity file the windows adapter names."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / WINDOWS_ACCOUNT_FILE).write_text(
        json.dumps({"loginAccount": {"emailAddress": login}}), encoding="utf-8"
    )


def window(used: float, resets_at: str | None = LATER) -> dict[str, Any]:
    return {"used_percent": float(used), "resets_at": resets_at}


def record_snapshot(
    store: Store,
    login: str,
    *,
    five_hour: dict[str, Any] | None,
    weekly: dict[str, Any] | None,
) -> None:
    """Store a window ``usage.snapshot`` as usage polling stores it."""
    record_windows(
        store,
        login,
        {
            "five_hour": five_hour,
            "weekly": weekly,
            "weekly_models": [
                # Not part of the load: a display name, not a role model.
                {"model": "model-x", "used_percent": 99.0, "resets_at": LATER}
            ],
        },
    )


def record_windows(store: Store, login: str, windows: Any) -> None:
    """Store a ``usage.snapshot`` whose ``windows`` value is given as is."""
    activity_id = str(uuid.uuid4())
    store.write(
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": "usage-session",
            "type": "usage.snapshot",
            "payload": {
                "vendor": POOL_DRIVER,
                "provider": POOL_DRIVER,
                "account_email": login,
                "tier": None,
                "windows": windows,
                "fetched_at": OLD_FETCH,
            },
            "execution_status": "done",
        },
    )


def running_lane(store: Store, account: str, *, status: str = "working") -> None:
    agent_id = str(uuid.uuid4())
    store.write(
        "agent",
        "insert",
        agent_id,
        {
            "id": agent_id,
            "task_id": "other-task",
            "role": "implementer",
            "vendor": POOL_DRIVER,
            "status": status,
            LANE_ACCOUNT_FIELD: account,
        },
    )


def pool_setup(tmp_path: Path, loads: dict[str, float]) -> tuple[Store, AIAccounts]:
    """Every seat logged in; each seat in ``loads`` has a snapshot with that weekly load."""
    store = Store(tmp_path)
    logins = tmp_path / "logins"
    ai = configure(store.home, logins)
    for seat in SEATS:
        write_login(logins / seat, login_of(seat))
    for seat, load in loads.items():
        record_snapshot(store, login_of(seat), five_hour=None, weekly=window(load))
    return store, ai


def choose(store: Store, ai: AIAccounts):
    return select_lane_account(store, ai, ai.roles[POOL_ROLE], now=NOW)


def test_lowest_load_wins(tmp_path: Path) -> None:
    store, ai = pool_setup(tmp_path, {"seat-1": 40, "seat-2": 10, "seat-3": 25})
    choice = choose(store, ai)
    assert choice.account == ai.accounts["seat-2"]
    assert choice.role.name == POOL_ROLE
    assert choice.role.model == "driver-p-model"
    assert not choice.role.is_pool
    assert choice.load == 10
    assert choice.running_lanes == 0
    fields = choice.record_fields()
    assert fields[LANE_ACCOUNT_FIELD] == "seat-2"
    assert fields[LANE_ACCOUNT_CHOICE_FIELD] == (
        "account seat-2 chosen from the pool of role pooled at load 10 % "
        "with 0 running lanes"
    )
    # The record carries the configured account name, never the login identity.
    assert "@" not in json.dumps(fields)
    # The chosen account isolates the launched process.
    prefix = choice.role.env_prefix(ai.drivers)
    assert f"TOOL_P_CONFIG={tmp_path / 'logins' / 'seat-2'}" in prefix


def test_equal_load_keeps_configuration_order(tmp_path: Path) -> None:
    store, ai = pool_setup(tmp_path, {"seat-1": 20, "seat-2": 20, "seat-3": 20})
    assert choose(store, ai).account.name == "seat-1"


def test_running_lanes_take_precedence_over_load(tmp_path: Path) -> None:
    store, ai = pool_setup(tmp_path, {"seat-1": 40, "seat-2": 10, "seat-3": 25})
    running_lane(store, "seat-2")
    # A finished lane is not running.
    running_lane(store, "seat-3", status="done")
    choice = choose(store, ai)
    assert choice.account.name == "seat-3"
    assert choice.running_lanes == 0
    assert choice.load == 25

    running_lane(store, "seat-3")
    running_lane(store, "seat-1")
    # Every member runs one lane: the least-loaded one wins again.
    choice = choose(store, ai)
    assert choice.account.name == "seat-2"
    assert choice.running_lanes == 1


def test_reset_window_counts_zero(tmp_path: Path) -> None:
    store, ai = pool_setup(tmp_path, {"seat-2": 10, "seat-3": 25})
    record_snapshot(
        store,
        login_of("seat-1"),
        five_hour=window(90, resets_at=EARLIER),
        weekly=window(5),
    )
    choice = choose(store, ai)
    assert choice.account.name == "seat-1"
    assert choice.load == 5

    # A reset exactly at the start time has happened too.
    record_snapshot(
        store,
        login_of("seat-1"),
        five_hour=window(90, resets_at=NOW.isoformat()),
        weekly=window(95, resets_at=EARLIER),
    )
    choice = choose(store, ai)
    assert choice.account.name == "seat-1"
    assert choice.load == 0


def test_null_window_counts_zero(tmp_path: Path) -> None:
    store, ai = pool_setup(tmp_path, {"seat-2": 10, "seat-3": 25})
    record_snapshot(store, login_of("seat-1"), five_hour=None, weekly=None)
    choice = choose(store, ai)
    assert choice.account.name == "seat-1"
    assert choice.load == 0


def test_newest_snapshot_counts_and_login_matches_without_case(tmp_path: Path) -> None:
    store, ai = pool_setup(tmp_path, {"seat-1": 5, "seat-2": 10, "seat-3": 25})
    record_snapshot(store, login_of("seat-1").upper(), five_hour=window(60), weekly=None)
    choice = choose(store, ai)
    assert choice.account.name == "seat-2"


def test_exhausted_member_is_skipped(tmp_path: Path) -> None:
    store, ai = pool_setup(tmp_path, {"seat-2": 30, "seat-3": 50})
    record_snapshot(
        store, login_of("seat-1"), five_hour=window(100), weekly=window(3)
    )
    assert choose(store, ai).account.name == "seat-2"


def test_missing_login_and_missing_snapshot_are_skipped(tmp_path: Path) -> None:
    store = Store(tmp_path)
    logins = tmp_path / "logins"
    ai = configure(store.home, logins)
    # seat-1 has a snapshot but no login on this device.
    record_snapshot(store, login_of("seat-1"), five_hour=None, weekly=window(1))
    # seat-2 is logged in but was never polled.
    write_login(logins / "seat-2", login_of("seat-2"))
    write_login(logins / "seat-3", login_of("seat-3"))
    record_snapshot(store, login_of("seat-3"), five_hour=None, weekly=window(50))
    choice = choose(store, ai)
    assert choice.account.name == "seat-3"
    assert choice.load == 50


def test_no_eligible_member_raises_with_every_reason(tmp_path: Path) -> None:
    store = Store(tmp_path)
    logins = tmp_path / "logins"
    ai = configure(store.home, logins)
    write_login(logins / "seat-2", login_of("seat-2"))
    write_login(logins / "seat-3", login_of("seat-3"))
    record_snapshot(
        store, login_of("seat-3"), five_hour=window(40), weekly=window(100)
    )
    with pytest.raises(AccountError) as excinfo:
        choose(store, ai)
    assert str(excinfo.value) == (
        "Role pooled has no usable pool account (seat-1: no login; "
        "seat-2: no usage snapshot; seat-3: exhausted at 100 %)"
    )
    assert "@" not in str(excinfo.value)


def test_unreadable_login_file_is_skipped_with_its_own_reason(tmp_path: Path) -> None:
    store = Store(tmp_path)
    logins = tmp_path / "logins"
    ai = configure(store.home, logins, seats=("seat-1", "seat-2"))
    # seat-1 has an account file that is not valid JSON, and a snapshot.
    (logins / "seat-1").mkdir(parents=True)
    (logins / "seat-1" / WINDOWS_ACCOUNT_FILE).write_text("{not json", encoding="utf-8")
    record_snapshot(store, login_of("seat-1"), five_hour=None, weekly=window(1))
    # seat-2 has no login on this device.
    with pytest.raises(AccountError) as excinfo:
        choose(store, ai)
    assert str(excinfo.value) == (
        "Role pooled has no usable pool account (seat-1: unreadable login file; "
        "seat-2: no login)"
    )

    write_login(logins / "seat-2", login_of("seat-2"))
    record_snapshot(store, login_of("seat-2"), five_hour=None, weekly=window(50))
    choice = choose(store, ai)
    assert choice.account.name == "seat-2"
    assert choice.load == 50


@pytest.mark.parametrize(
    "windows",
    [
        pytest.param([], id="windows-list"),
        pytest.param({"five_hour": "x", "weekly": None}, id="window-text"),
        pytest.param(
            {"five_hour": {"used_percent": True, "resets_at": LATER}, "weekly": None},
            id="used-percent-bool",
        ),
        pytest.param(
            {
                "five_hour": {"used_percent": float("inf"), "resets_at": LATER},
                "weekly": None,
            },
            id="used-percent-infinite",
        ),
        pytest.param(
            {"five_hour": {"used_percent": 10.0, "resets_at": 7}, "weekly": None},
            id="resets-at-number",
        ),
        pytest.param(
            {"five_hour": {"used_percent": 10.0, "resets_at": "not-a-date"}, "weekly": None},
            id="resets-at-not-a-date",
        ),
        pytest.param(
            {
                "five_hour": {"used_percent": 10.0, "resets_at": "2026-11-20T00:00:00"},
                "weekly": None,
            },
            id="resets-at-without-zone",
        ),
        pytest.param({}, id="windows-empty"),
        pytest.param({"five_hour": {"used_percent": 10.0}, "weekly": None}, id="resets-at-missing"),
        pytest.param({"weekly": None}, id="five-hour-missing"),
        pytest.param(
            {"five_hour": {"used_percent": 10.0, "resets_at": LATER}},
            id="weekly-missing",
        ),
        pytest.param(
            {"five_hour": {"used_percent": 10**400, "resets_at": LATER}, "weekly": None},
            id="used-percent-too-large-for-a-float",
        ),
    ],
)
def test_unreadable_usage_snapshot_is_skipped_with_its_own_reason(
    tmp_path: Path, windows: Any
) -> None:
    store = Store(tmp_path)
    logins = tmp_path / "logins"
    ai = configure(store.home, logins, seats=("seat-1", "seat-2"))
    # seat-1 is logged in, and its newest snapshot has a broken windows shape.
    write_login(logins / "seat-1", login_of("seat-1"))
    record_windows(store, login_of("seat-1"), windows)
    # seat-2 has no login on this device.
    with pytest.raises(AccountError) as excinfo:
        choose(store, ai)
    assert str(excinfo.value) == (
        "Role pooled has no usable pool account (seat-1: unreadable usage snapshot; "
        "seat-2: no login)"
    )

    write_login(logins / "seat-2", login_of("seat-2"))
    record_snapshot(store, login_of("seat-2"), five_hour=None, weekly=window(50))
    choice = choose(store, ai)
    assert choice.account.name == "seat-2"
    assert choice.load == 50


def test_single_account_role_is_returned_without_choosing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ai = configure(store.home, tmp_path / "logins")
    role = ai.roles["driver-c-writer"]
    # No login and no usage snapshot exist: a single-account role needs neither.
    choice = select_lane_account(store, ai, role, now=NOW)
    assert choice.role is role
    assert choice.load is None
    assert choice.running_lanes is None
    assert choice.summary() is None
    assert choice.record_fields() == {LANE_ACCOUNT_FIELD: "driver-c-account"}


@pytest.mark.no_pg
def test_recorded_account_resolves_a_pool_role_without_choosing(tmp_path: Path) -> None:
    ai = configure(tmp_path, tmp_path / "logins")
    pooled = ai.roles[POOL_ROLE]
    resolved = recorded_lane_role(ai, pooled, "seat-3", lane="agent-1")
    assert resolved.account == ai.accounts["seat-3"]
    assert resolved.model == pooled.model
    assert resolved.access == pooled.access


@pytest.mark.no_pg
def test_recorded_account_outside_the_pool_raises(tmp_path: Path) -> None:
    ai = configure(tmp_path, tmp_path / "logins", seats=("seat-1", "seat-2"))
    with pytest.raises(AccountError) as excinfo:
        recorded_lane_role(ai, ai.roles[POOL_ROLE], "seat-3", lane="agent-1")
    assert str(excinfo.value) == (
        "Lane agent-1 ran on account seat-3, which is no longer in the pool of "
        "role pooled"
    )


@pytest.mark.no_pg
def test_record_without_account_raises_for_a_pool_role_only(tmp_path: Path) -> None:
    ai = configure(tmp_path, tmp_path / "logins")
    with pytest.raises(AccountError) as excinfo:
        recorded_lane_role(ai, ai.roles[POOL_ROLE], None, lane="agent-1")
    assert str(excinfo.value) == (
        "Lane agent-1 of pool role pooled has no recorded account"
    )
    single = ai.roles["driver-c-writer"]
    assert recorded_lane_role(ai, single, None, lane="agent-1") is single


@pytest.mark.no_pg
def test_recorded_account_of_a_single_account_role(tmp_path: Path) -> None:
    ai = configure(tmp_path, tmp_path / "logins")
    single = ai.roles["driver-c-writer"]
    assert recorded_lane_role(ai, single, "driver-c-account", lane="agent-1") is single
    with pytest.raises(AccountError, match="no longer the account of role driver-c-writer"):
        recorded_lane_role(ai, single, "seat-1", lane="agent-1")
    with pytest.raises(AccountError, match="invalid recorded account"):
        recorded_lane_role(ai, single, 7, lane="agent-1")


POOL_REFUSAL = POOL_ROLE_NEEDS_COORDINATOR.format(role=POOL_ROLE)


def lane_spec(home: Path) -> Path:
    spec = home / "spec.md"
    spec.write_text("implement this\n", encoding="utf-8")
    return spec


@pytest.mark.no_pg
def test_pool_role_refusal_is_one_message(tmp_path: Path) -> None:
    ai = configure(tmp_path, tmp_path / "logins")
    pooled = ai.roles[POOL_ROLE]
    with pytest.raises(AccountError) as refused:
        pooled.require_single_account()
    assert str(refused.value) == (
        "Role pooled names an account pool; a pool account is chosen only when "
        "a coordinator lane starts"
    )
    # The process isolation prefix refuses a pool role with the same text.
    with pytest.raises(AccountError) as prefix:
        pooled.env_prefix(ai.drivers)
    assert str(prefix.value) == POOL_REFUSAL
    ai.roles["driver-c-writer"].require_single_account()


@pytest.mark.no_pg
def test_lane_launch_without_binding_refuses_a_pool_role_before_the_overlap_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path, tmp_path / "logins")
    work = tmp_path / "work"
    work.mkdir()

    def overlap_not_reached(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the overlap check ran for a pool role")

    monkeypatch.setattr("agent_cli.lane.credential_dir_overlap_error", overlap_not_reached)
    with pytest.raises(SystemExit) as refused:
        launch(
            role="implementer",
            vendor=POOL_DRIVER,
            spec_file=str(lane_spec(tmp_path)),
            cwd=str(work),
            dry_run=True,
            tmux=False,
            config_home=tmp_path,
            session_id="session-a",
        )
    assert str(refused.value) == POOL_REFUSAL


@pytest.mark.no_pg
def test_lane_launch_with_a_selected_binding_runs_on_that_member(
    tmp_path: Path,
) -> None:
    logins = tmp_path / "logins"
    ai = configure(tmp_path, logins)
    work = tmp_path / "work"
    work.mkdir()
    # The binding a coordinator lane start or a recorded lane passes in.
    binding = recorded_lane_role(ai, ai.roles[POOL_ROLE], "seat-2", lane="agent-1")
    result = launch(
        role="implementer",
        vendor=POOL_DRIVER,
        spec_file=str(lane_spec(tmp_path)),
        cwd=str(work),
        dry_run=True,
        tmux=False,
        config_home=tmp_path,
        session_id="session-a",
        binding=binding,
    )
    assert f"TOOL_P_CONFIG={logins / 'seat-2'}" in result.argv
    assert "driver-p-model" in result.argv


def test_agent_run_refuses_a_pool_role_without_an_agent_row(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(tmp_path, tmp_path / "logins")
    tid = _bootstrap_implement(tmp_path, capsys)
    run(
        tmp_path,
        [
            "task", "models", "--id", tid, "--slot", "implementer",
            "--vendor", POOL_DRIVER, "--reason", "pooled implementer",
        ],
    )
    capsys.readouterr()
    agents_before = _agents(tmp_path, tid)

    def no_launch(**kwargs: Any) -> None:
        raise AssertionError(f"a pool role launched outside a coordinator: {kwargs}")

    monkeypatch.setattr("agent_cli.main.launch", no_launch)
    with pytest.raises(SystemExit) as refused:
        run(
            tmp_path,
            [
                "run", "--task", tid, "--spec-file", str(lane_spec(tmp_path)),
                "--no-tmux", "--cwd", str(tmp_path),
            ],
        )
    assert str(refused.value) == f"agent: {POOL_REFUSAL}"
    assert _agents(tmp_path, tid) == agents_before
    assert not any(agent.get("role") == "implementer" for agent in _agents(tmp_path, tid))


def test_pool_selection_refuses_a_time_without_a_zone(tmp_path: Path) -> None:
    store = Store(tmp_path)
    ai = configure(store.home, tmp_path / "logins")
    naive = datetime(2026, 11, 17, 12, 0)
    with pytest.raises(AccountError) as excinfo:
        # No member has a login: the time is refused before members are read.
        select_lane_account(store, ai, ai.roles[POOL_ROLE], now=naive)
    assert str(excinfo.value) == "pool selection time must carry a time zone"


def test_working_rows_without_a_recorded_account_are_not_counted(tmp_path: Path) -> None:
    store, ai = pool_setup(tmp_path, {"seat-1": 40, "seat-2": 10, "seat-3": 25})
    # A working row of this device that names no account (written before pools
    # existed) counts for no member.
    agent_id = str(uuid.uuid4())
    store.write(
        "agent",
        "insert",
        agent_id,
        {
            "id": agent_id,
            "task_id": "other-task",
            "role": "implementer",
            "vendor": POOL_DRIVER,
            "status": "working",
        },
    )
    choice = choose(store, ai)
    assert choice.account.name == "seat-2"
    assert choice.running_lanes == 0
    assert choice.load == 10
