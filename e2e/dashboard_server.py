"""Real ``agent dashboard`` on a seeded test home for the Playwright screenshots.

Playwright's ``webServer`` starts this script (see ``playwright.config.ts``). It
starts a throwaway PostgreSQL cluster the way ``tests/conftest.py`` does, seeds
a fresh ``AGENT_HOME`` with the placeholder fixtures of ``tests/`` (no personal
data, no secrets, no network), freezes the server clock at ``NOW`` and then
serves ``cmd_dashboard`` until Playwright stops it with SIGTERM.

``--data filled`` seeds every dashboard section; ``--data empty`` seeds only the
device identity, so every page shows its empty state.

This is not a test module: pytest collects only ``tests/`` (``testpaths``).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import pkgutil
import shutil
import signal
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import agent_cli  # noqa: E402
from agent_cli import main as main_mod  # noqa: E402
from agent_cli import store as store_mod  # noqa: E402
from agent_cli.pg import create_database, ensure_cluster, stop_cluster  # noqa: E402

# The instant every page is rendered at. e2e/dashboard.spec.ts sets the browser
# clock to the same instant, and test_dashboard_html.OVERVIEW_NOW uses it too.
NOW = datetime(2026, 11, 17, 10, 0, tzinfo=timezone.utc)

DEVICE_ID = "5d2c8f1e-7a43-4b9e-9c1d-2f6a8e0b3c47"
OTHER_DEVICE_ID = "a9e4b7c2-1f38-4d6a-8b05-6c3e9d2f7a18"
LOGIN = "human-owner"


class Clock:
    """The server's frozen clock; seeding moves it to give rows distinct ages."""

    at = NOW

    @classmethod
    def utcnow(cls) -> str:
        return cls.at.strftime("%Y-%m-%dT%H:%M:%SZ")


class FrozenDatetime(datetime):
    """``datetime`` of ``agent_cli.main`` whose ``now`` is the frozen clock."""

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return Clock.at.astimezone(tz) if tz is not None else Clock.at.replace(tzinfo=None)


def freeze_clock() -> None:
    """Replace every module's ``utcnow`` (the store's timestamp source) and the
    usage overview's ``datetime.now`` of ``cmd_dashboard`` with the frozen clock,
    as the tests do with ``monkeypatch.setattr(..., "utcnow", ...)``."""
    real = store_mod.utcnow
    for info in pkgutil.walk_packages(agent_cli.__path__, "agent_cli."):
        importlib.import_module(info.name)
    for module in list(sys.modules.values()):
        if getattr(module, "utcnow", None) is real:
            module.utcnow = Clock.utcnow
    main_mod.datetime = FrozenDatetime


def ago(**delta: float) -> str:
    return (NOW - timedelta(**delta)).strftime("%Y-%m-%dT%H:%M:%SZ")


def at(**delta: float) -> None:
    Clock.at = NOW - timedelta(**delta)


def fixed_id(name: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"agent-dashboard-e2e:{name}"))


def write_identity(home: Path, *, paired: bool) -> None:
    identity = {"device_id": DEVICE_ID}
    if paired:
        identity["github_login"] = LOGIN
    (home / "device.json").write_text(json.dumps(identity), encoding="utf-8")


def seed_usage(store, home: Path) -> None:
    from driver_fixtures import WINDOWS_DRIVER_ID, driver_document, windows_driver, write_drivers
    from test_dashboard_html import _spend
    from test_usage import PERIOD_DRIVER, _key_body, _period_documents, _usage
    from test_usage_windows import mapped, usage_body
    from agent_cli.usage import period_snapshot

    document = driver_document()
    document["drivers"][WINDOWS_DRIVER_ID] = windows_driver()
    write_drivers(home, document)
    (home / "ai-subscriptions.json").write_text(
        json.dumps(
            {
                "subscriptions": [
                    {"provider": WINDOWS_DRIVER_ID, "account": "seat-1@example.com", "label": "Team seat 1", "status": "active"},
                    {"provider": WINDOWS_DRIVER_ID, "account": "seat-2@example.com", "label": "Team seat 2", "status": "active"},
                    {"provider": WINDOWS_DRIVER_ID, "account": "seat-3@example.com", "status": "paused", "note": "renews in December"},
                    {"provider": "driver-b", "account": "seat-1@example.com", "label": "Review seat"},
                ]
            }
        ),
        encoding="utf-8",
    )
    five_hour = {"utilization": 41.0, "resets_at": "2026-11-17T13:12:00+00:00"}
    period = period_snapshot(
        PERIOD_DRIVER, _usage(PERIOD_DRIVER), _period_documents(used=37.0), ago(seconds=30), "period@example.com"
    )
    period["period_end"] = "2026-11-20T14:00:00+00:00"
    snapshots = [
        mapped("seat-1@example.com", "max_20x", usage_body(five_hour=five_hour), ago(minutes=4)),
        mapped("seat-3@example.com", "max_5x", usage_body(weekly=82.0), ago(hours=2)),
        period,
        _spend(_key_body(usage=6.4), ago(minutes=12)),
    ]
    for index, snapshot in enumerate(snapshots):
        activity_id = fixed_id(f"usage-{index}")
        store.write(
            "activity",
            "insert",
            activity_id,
            {
                "id": activity_id,
                "session_id": "worker-session",
                "type": "usage.snapshot",
                "payload": snapshot,
                "execution_status": "done",
            },
        )


def seed_sessions(store) -> str:
    from test_coordinator_support import make_session

    at(days=2)
    make_session(store, "worker-session", ["spine", "review-loop", "pr-review"])
    make_session(store, "review-session", ["pr-review"])
    at(minutes=3)
    interactive = fixed_id("session-interactive")
    store.write(
        "session",
        "insert",
        interactive,
        {
            "id": interactive,
            "kind": "interactive",
            "started_at": ago(hours=5),
            "last_seen_at": ago(minutes=3),
            "status": "active",
            "skills": ["spine"],
            "runtime": {"provider": "driver-a", "tmux_target": "agent:1"},
        },
    )
    foreign = fixed_id("session-foreign")
    store.apply_remote(
        {
            "origin_device_id": OTHER_DEVICE_ID,
            "origin_seq": 1,
            "table": "session",
            "op": "insert",
            "row_id": foreign,
            "payload": {
                "id": foreign,
                "kind": "interactive",
                "started_at": ago(hours=9),
                "last_seen_at": ago(hours=1),
                "status": "closed",
                "skills": ["spine"],
            },
            "occurred_at": ago(hours=1),
        },
        wake=False,
    )
    return interactive


def seed_spine(store, session_id: str) -> None:
    head = "3f9a1c7e5b2d8046a1e9c3b7d5f2a8e4c6b0d913"
    tasks = [
        ("task-widget", "Add a settings widget", "implement", "pr-review", 2, {"hours": 6}),
        ("task-docs", "Document the export command", "implement", "done", 1, {"days": 1}),
        ("task-flaky", "Fix the flaky import test", "review", "failed", 1, {"hours": 20}),
    ]
    for name, title, workflow, state, rounds, age in tasks:
        at(**age)
        tid = fixed_id(name)
        store.write(
            "task",
            "insert",
            tid,
            {
                "id": tid,
                "session_id": session_id,
                "workflow": workflow,
                "title": title,
                "repo": "example/project",
                "ref": None,
                "state": state,
                "current_round": rounds,
                "models": {"first": "driver-a", "second": "driver-b"},
                "created_at": ago(**age),
                "updated_at": ago(**age),
                "change_summary_en": None,
                "change_summary_de": None,
            },
        )
        for number in range(1, rounds + 1):
            last = number == rounds
            rid = fixed_id(f"{name}-round-{number}")
            store.write(
                "task_round",
                "insert",
                rid,
                {
                    "id": rid,
                    "task_id": tid,
                    "round": number,
                    "implementer_verdict": "done",
                    "reviewer_verdict": ("rejected" if state == "failed" else "approved") if last else "rejected",
                    "started_at": ago(hours=8 - number),
                    "finished_at": ago(hours=7 - number),
                },
            )
        for role, vendor, status in (
            ("implementer", "driver-a", "done"),
            ("reviewer", "driver-b", "failed" if state == "failed" else "done"),
        ):
            aid = fixed_id(f"{name}-{role}")
            store.write(
                "agent",
                "insert",
                aid,
                {"id": aid, "task_id": tid, "role": role, "vendor": vendor, "status": status, "round": rounds},
            )
        cid = fixed_id(f"{name}-check")
        store.write(
            "local_check",
            "insert",
            cid,
            {
                "id": cid,
                "task_id": tid,
                "name": "pytest",
                "command": "pytest -q",
                "result": "fail" if state == "failed" else "pass",
                "output": None,
                "ran_at": ago(**age),
            },
        )
    widget = fixed_id("task-widget")
    for stage, dimension, vendor, verdict in (
        ("first", "quality", "driver-a", "approved"),
        ("first", "logic", "driver-a", "approved"),
        ("second", "quality", "driver-b", "approved"),
        ("second", "logic", "driver-b", "rejected"),
    ):
        aid = fixed_id(f"gate-agent-{stage}-{dimension}")
        store.write(
            "agent",
            "insert",
            aid,
            {
                "id": aid,
                "task_id": widget,
                "role": f"pr-reviewer-{dimension}",
                "vendor": vendor,
                "stage": stage,
                "status": "done",
                "round": 2,
            },
        )
        gid = fixed_id(f"gate-{stage}-{dimension}")
        store.write(
            "review_gate",
            "insert",
            gid,
            {
                "id": gid,
                "task_id": widget,
                "stage": stage,
                "dimension": dimension,
                "vendor": vendor,
                "verdict": verdict,
                "evidence": None,
                "head_sha": head,
                "agent_id": aid,
                "recorded_at": ago(hours=1),
            },
        )
    at(minutes=40)
    for key, status, closable_by, note in (
        ("ci-green", "open", "agent", "Waiting for the test workflow"),
        ("human-merge", "open", "human", "Merge after the second review"),
        ("docs-updated", "closed", "agent", None),
    ):
        wid = fixed_id(f"work-{key}")
        store.write(
            "open_work",
            "insert",
            wid,
            {
                "id": wid,
                "session_id": session_id,
                "key": key,
                "status": status,
                "closable_by": closable_by,
                "note": note,
                "updated_at": ago(minutes=40),
            },
        )
    for name, to, kind, acked in (
        ("ping-review", "review-bot", "review-request", None),
        ("ping-question", "worker-bot", "question", ago(minutes=25)),
    ):
        pid = fixed_id(name)
        store.write(
            "ping",
            "insert",
            pid,
            {
                "id": pid,
                "from_login": LOGIN,
                "to_login": to,
                "kind": kind,
                "task_id": widget,
                "body": "",
                "created_at": ago(minutes=40),
                "acked_at": acked,
            },
        )


def seed_orders(store, home: Path) -> None:
    from types import SimpleNamespace

    from agent_cli.coordinator_common import save_task
    from agent_cli.coordinator_config import load_coordinator_config
    from agent_cli.coordinator_orders import post_order_event
    from test_coordinator_support import OPEN_ORDER_SEEDS, seed_order_task, write_accounts, write_check_lane
    from test_orders_cli import blocked_order, duplicate_order, order_config

    write_accounts(home)
    write_check_lane(home)
    (home / "work").mkdir()
    (home / "coordinator.json").write_text(json.dumps(order_config(home)), encoding="utf-8")
    worker = load_coordinator_config(home)["worker-session"]
    device = SimpleNamespace(home=home, store=store, worker=worker)

    titles = {"queued": "Add a CSV export", "running": "Add a settings widget", "ready": "Rename the config flag"}
    ages = {"queued": {"minutes": 5}, "running": {"hours": 2}, "ready": {"hours": 26}}
    for name, seed in OPEN_ORDER_SEEDS.items():
        at(**ages[name])
        task = seed_order_task(store, worker, fixed_id(f"order-{name}"), title=titles[name], **seed)
        save_task(store, task)
        post_order_event(store, worker, task, kind="accepted", occurrence="submit", digest="Order accepted")
    at(minutes=50)
    blocked_order(device, fixed_id("order-blocked"))
    at(minutes=15)
    duplicate_order(device, fixed_id("order-duplicate"))
    at(days=3)
    ended = seed_order_task(store, worker, fixed_id("order-done"), title="Remove the legacy importer", phase="done")
    ended["state"] = "done"
    save_task(store, ended)


def seed(home: Path, data: str) -> None:
    from agent_cli.store import Store

    write_identity(home, paired=data == "filled")
    store = Store(home)
    try:
        if data == "filled":
            interactive = seed_sessions(store)
            seed_spine(store, interactive)
            seed_orders(store, home)
            # After seed_orders: its write_accounts writes the default drivers.json.
            seed_usage(store, home)
    finally:
        store.close()
    Clock.at = NOW


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data", choices=("filled", "empty"), required=True)
    args = parser.parse_args()

    def stop(signum: int, frame: object) -> None:
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)

    freeze_clock()
    root = Path(tempfile.mkdtemp(prefix="agent-dashboard-e2e-"))
    cluster = root / "pg-cluster"
    try:
        admin_dsn = ensure_cluster(cluster)
        home = root / "home"
        home.mkdir()
        os.environ["AGENT_HOME"] = str(home)
        os.environ["AGENT_PG_DSN"] = create_database(admin_dsn, "dashboard")
        seed(home, args.data)
        main_mod.cmd_dashboard(["--port", str(args.port)])
    finally:
        stop_cluster(cluster)
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()
