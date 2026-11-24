"""U-RUN-install-state: the public `state.json` (0644) and the root-only `maintenance.json` (0600) of an installed
runner package (ADR-074 sections 3 and 6): atomic writes with exact modes, tolerant reads, defaults for anything
malformed."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import pytest

from agent_runner.install import state
from agent_runner.install.layout import Layout


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_a_json_file_is_written_atomically_with_its_mode(tmp_path: Path) -> None:
    path = tmp_path / "new/dir/state.json"
    old = os.umask(0o077)
    try:
        state.write_json(path, {"b": 1, "a": [1]}, 0o644)
    finally:
        os.umask(old)
    assert mode(path) == 0o644
    assert path.read_text(encoding="utf-8") == '{\n "a": [\n  1\n ],\n "b": 1\n}\n'
    state.write_json(path, {"c": True}, 0o600)
    assert mode(path) == 0o600
    assert json.loads(path.read_text(encoding="utf-8")) == {"c": True}
    # no temporary file is left behind
    assert sorted(item.name for item in path.parent.iterdir()) == ["state.json"]


def test_a_link_at_the_target_is_replaced_not_followed(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    victim.write_text("keep", encoding="utf-8")
    path = tmp_path / "state.json"
    path.symlink_to(victim)
    state.write_json(path, {"a": 1}, 0o644)
    assert not path.is_symlink()
    assert victim.read_text(encoding="utf-8") == "keep"


def test_reading_json(tmp_path: Path) -> None:
    path = tmp_path / "x.json"
    assert state.read_json(path) is None
    path.write_text("{broken", encoding="utf-8")
    assert state.read_json(path) is None
    path.write_bytes(b"\xff\xfe")
    assert state.read_json(path) is None
    path.write_text("[1, 2]", encoding="utf-8")
    assert state.read_json(path) is None
    path.write_text('{"a": 1}', encoding="utf-8")
    assert state.read_json(path) == {"a": 1}
    assert state.read_json(tmp_path) is None


def test_the_public_state_carries_its_version(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state.write_state(path, {"enrolled": True, "version": "2026.11.24.0800"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"v": 1, "enrolled": True, "version": "2026.11.24.0800"}
    assert mode(path) == 0o644


def test_maintenance_state_round_trips_root_only(tmp_path: Path) -> None:
    path = tmp_path / "maintenance.json"
    memory = state.Maintenance(
        previous="2026.11.23.0800",
        owner="alice",
        created=[".local", ".local/share"],
        pending={"version": "2026.11.25.0800"},
        attempts={"2026.11.25.0800": 2},
        bad=["2026.11.20.0800"],
        first_seen={"2026.11.25.0800": "2026-11-25T08:00:00Z"},
        last_check="2026-11-25T09:00:00Z",
        feed="ok",
    )
    memory.save(path)
    assert mode(path) == 0o600
    assert state.Maintenance.load(path) == memory


def test_missing_or_unknown_maintenance_state_is_the_default(tmp_path: Path) -> None:
    path = tmp_path / "maintenance.json"
    assert state.Maintenance.load(path) == state.Maintenance()
    assert state.Maintenance() == state.Maintenance(None, None, [], None, {}, [], {}, None, "unchecked")
    path.write_text('{"previous": "a", "future_field": 1}', encoding="utf-8")
    assert state.Maintenance.load(path) == state.Maintenance(previous="a")


@pytest.mark.parametrize(
    "field",
    [
        {"previous": 1},
        {"owner": 501},
        {"created": ".local"},
        {"created": [".local", 1]},
        {"pending": "x"},
        {"attempts": []},
        {"bad": {}},
        {"first_seen": "x"},
        {"last_check": 5},
        {"feed": None},
    ],
)
def test_malformed_maintenance_state_is_the_default(tmp_path: Path, field: dict[str, Any]) -> None:
    path = tmp_path / "maintenance.json"
    path.write_text(json.dumps({"previous": "a", "feed": "ok", **field}), encoding="utf-8")
    assert state.Maintenance.load(path) == state.Maintenance()


def test_the_files_of_the_payload_live_below_the_layout_root(tmp_path: Path) -> None:
    layout = Layout(tmp_path)
    payload = tmp_path / "Library/Application Support/DFXAgentRunner"
    assert layout.state_file == payload / "state.json"
    assert layout.maintenance_file == payload / "maintenance.json"
    assert layout.spool == payload / "spool"
    assert layout.request == payload / "spool/enroll.request"
    assert layout.downloads == payload / "downloads"
    assert layout.log_dir == payload / "log"
    assert Layout.runner_home(tmp_path / "Users/alice") == tmp_path / "Users/alice/.local/share/dfx-agent-runner"
    assert Layout().state_file == Path("/Library/Application Support/DFXAgentRunner/state.json")
