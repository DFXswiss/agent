from __future__ import annotations

import json
from pathlib import Path

from agent_cli.work_signal import is_recent_work, last_work_at, parse_ts


def test_parse_ts() -> None:
    assert parse_ts("2026-08-26T22:52:45.918504Z") == parse_ts("2026-08-26T22:52:45Z")
    assert parse_ts("nope") is None


def test_activity_log_grok_chat_counts_as_work(tmp_path: Path) -> None:
    logdir = tmp_path / "activity-log"
    logdir.mkdir()
    path = logdir / "activity-record-2026-08-27.jsonl"
    rows = [
        {"type": "filesystem", "ts": "2026-08-27T10:00:00Z"},
        {"type": "process_snapshot", "ts": "2026-08-27T10:05:00Z", "interesting": ["grok"]},
        {"type": "grok_chat", "ts": "2026-08-27T10:01:00Z", "role": "assistant"},
    ]
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    env = {"AGENT_ACTIVITY_LOG": str(logdir)}
    stamp = last_work_at(env)
    assert stamp == parse_ts("2026-08-27T10:01:00Z")
    now = parse_ts("2026-08-27T10:05:00Z")
    assert now is not None
    assert is_recent_work(env, now=now, window=600) is True
    assert is_recent_work(env, now=now + 601, window=600) is False


def test_process_snapshot_alone_is_not_work(tmp_path: Path) -> None:
    logdir = tmp_path / "activity-log"
    logdir.mkdir()
    path = logdir / "activity-record-2026-08-27.jsonl"
    path.write_text(
        json.dumps({"type": "process_snapshot", "ts": "2026-08-27T10:05:00Z"}) + "\n",
        encoding="utf-8",
    )
    assert last_work_at({"AGENT_ACTIVITY_LOG": str(logdir)}) is None


def test_grok_home_mtime_fallback(tmp_path: Path) -> None:
    grok = tmp_path / "grok-home"
    logs = grok / "logs"
    logs.mkdir(parents=True)
    unified = logs / "unified.jsonl"
    unified.write_text("{}\n", encoding="utf-8")
    env = {"GROK_HOME": str(grok)}
    stamp = last_work_at(env)
    assert stamp is not None
    assert is_recent_work(env, now=stamp + 10, window=600) is True
    assert is_recent_work(env, now=stamp + 700, window=600) is False


def test_missing_sources_are_not_work(tmp_path: Path) -> None:
    assert last_work_at({}) is None
    assert is_recent_work({}, now=1_000.0, window=600) is False
