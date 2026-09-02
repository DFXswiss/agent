from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent_cli import main as main_mod
from agent_cli.hub import HubError
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


def _stub_scans(monkeypatch: pytest.MonkeyPatch, order: list[str] | None = None) -> None:
    log = order if order is not None else []

    def scan_usage(store: object) -> None:
        log.append("scan_usage")

    def scan_merged(store: object, run_argv: object) -> tuple[list[str], int]:
        log.append("scan_merged")
        return ([], 0)

    def scan_pending(store: object, hub: object) -> list[str]:
        log.append("scan_pending")
        return []

    def scan_github(store: object, run_argv: object) -> list[str]:
        log.append("scan_github")
        return []

    def scan_mail(store: object, run_argv: object) -> list[str]:
        log.append("scan_mail")
        return []

    def scan_error_fix(store: object, run_argv: object) -> list[str]:
        log.append("scan_error_fix")
        return []

    monkeypatch.setattr(main_mod, "scan_usage", scan_usage)
    monkeypatch.setattr(main_mod, "scan_merged", scan_merged)
    monkeypatch.setattr("agent_cli.pending.scan_pending", scan_pending)
    monkeypatch.setattr("agent_cli.github_act.scan_github", scan_github)
    monkeypatch.setattr("agent_cli.mail_act.scan_mail", scan_mail)
    monkeypatch.setattr("agent_cli.error_fix_act.scan_error_fix", scan_error_fix)


def test_knock_scan_cycle_syncs_when_paired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync must run once, and after every scan - not just alongside them - since
    it is meant to push whatever those scans just created."""
    order: list[str] = []
    monkeypatch.setattr(main_mod, "_sync_once", lambda store: order.append("sync"))
    _stub_scans(monkeypatch, order)
    _init_paired_store(tmp_path)

    store = open_store()
    try:
        main_mod._knock_scan_cycle(store, lambda _argv: None)
    finally:
        store.close()

    assert order[-1] == "sync"
    assert order.count("sync") == 1
    assert set(order[:-1]) == {
        "scan_usage",
        "scan_merged",
        "scan_pending",
        "scan_github",
        "scan_mail",
        "scan_error_fix",
    }


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
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: _sync_once() raises HubError when the hub returns a
    malformed pull payload. A narrow except that only caught (HubError, StoreError)
    still covers this - HubError is exactly what the malformed-response checks
    raise - so the daemon logs and moves on instead of dying, without needing to
    also catch bare SystemExit (nothing else _sync_once can raise from this call
    site is a plain SystemExit)."""

    def _raise(_store: object) -> None:
        raise HubError("pull response missing events")

    monkeypatch.setattr(main_mod, "_sync_once", _raise)
    _stub_scans(monkeypatch)
    _init_paired_store(tmp_path)
    capsys.readouterr()

    store = open_store()
    try:
        main_mod._knock_scan_cycle(store, lambda _argv: None)
    finally:
        store.close()

    captured = capsys.readouterr()
    assert "sync error: pull response missing events" in captured.err
