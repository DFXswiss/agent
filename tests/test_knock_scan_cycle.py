from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli import main as main_mod
from agent_cli.main import open_store


def _init_paired_store(tmp_path: Path) -> None:
    os.environ["AGENT_HOME"] = str(tmp_path)
    main_mod.main(["init"])
    store = open_store()
    try:
        store.set_meta("hub_url", "https://hub.example")
        store.set_meta("device_token", "fake-token")
    finally:
        store.close()


def _stub_scans(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_mod, "scan_usage", lambda store: None)
    monkeypatch.setattr(main_mod, "scan_merged", lambda store, run_argv: ([], 0))
    monkeypatch.setattr("agent_cli.pending.scan_pending", lambda store, hub: [])
    monkeypatch.setattr("agent_cli.github_act.scan_github", lambda store, run_argv: [])
    monkeypatch.setattr("agent_cli.mail_act.scan_mail", lambda store, run_argv: [])
    monkeypatch.setattr("agent_cli.error_fix_act.scan_error_fix", lambda store, run_argv: [])


def test_knock_scan_cycle_pushes_when_paired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: calls.append(1))
    _stub_scans(monkeypatch)
    _init_paired_store(tmp_path)

    store = open_store()
    try:
        main_mod._knock_scan_cycle(store, lambda _argv: None)
    finally:
        store.close()

    assert calls == [1]


def test_knock_scan_cycle_skips_sync_when_unpaired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: calls.append(1))
    _stub_scans(monkeypatch)

    os.environ["AGENT_HOME"] = str(tmp_path)
    main_mod.main(["init"])

    store = open_store()
    try:
        main_mod._knock_scan_cycle(store, lambda _argv: None)
    finally:
        store.close()

    assert calls == []


def test_knock_scan_cycle_logs_and_continues_on_malformed_pull_response(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: _sync_once() calls die() (bare SystemExit, not a HubError/
    StoreError) when the hub returns a malformed pull payload. A narrow except that
    only caught (HubError, StoreError) would let that SystemExit propagate and kill
    the whole cmd_knock daemon loop instead of logging and moving on like every
    other scan in this function."""

    def _raise(_store: object) -> None:
        raise SystemExit("agent: pull response missing events")

    monkeypatch.setattr(main_mod, "_sync_once", _raise)
    _stub_scans(monkeypatch)
    _init_paired_store(tmp_path)

    store = open_store()
    try:
        main_mod._knock_scan_cycle(store, lambda _argv: None)
    finally:
        store.close()
