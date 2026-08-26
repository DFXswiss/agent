"""Whether the worker produced Grok work recently.

Prefers AGENT_ACTIVITY_LOG JSONL (`grok_unified` / `grok_chat`), then
GROK_HOME/logs/unified.jsonl mtime. Pane flicker is not work.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

WORK_TYPES = frozenset({"grok_unified", "grok_chat"})
TAIL_BYTES = 2_000_000


def parse_ts(raw: object) -> float | None:
    if not isinstance(raw, str) or raw == "":
        return None
    text = raw[:-1] if raw.endswith("Z") else raw
    if "." in text:
        text = text.split(".", 1)[0]
    try:
        dt = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.timestamp()


def last_work_from_jsonl(path: Path) -> float | None:
    if not path.is_file():
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(size - TAIL_BYTES)
                fh.readline()
            data = fh.read()
    except OSError:
        return None
    best: float | None = None
    for line in data.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        if row.get("type") not in WORK_TYPES:
            continue
        ts = parse_ts(row.get("ts"))
        if ts is not None and (best is None or ts > best):
            best = ts
    return best


def last_work_from_activity_dir(path: Path) -> float | None:
    if not path.is_dir():
        return None
    best: float | None = None
    files = sorted(path.glob("activity-record-*.jsonl"))[-3:]
    for item in files:
        ts = last_work_from_jsonl(item)
        if ts is not None and (best is None or ts > best):
            best = ts
    return best


def last_work_from_grok_home(path: Path) -> float | None:
    unified = path / "logs" / "unified.jsonl"
    if not unified.is_file():
        return None
    try:
        return unified.stat().st_mtime
    except OSError:
        return None


def last_work_at(environ: Mapping[str, str] | None = None) -> float | None:
    env = os.environ if environ is None else environ
    logdir = env.get("AGENT_ACTIVITY_LOG", "")
    if isinstance(logdir, str) and logdir != "":
        found = last_work_from_activity_dir(Path(logdir))
        if found is not None:
            return found
    grok = env.get("GROK_HOME", "")
    if isinstance(grok, str) and grok != "":
        return last_work_from_grok_home(Path(grok))
    return None


def is_recent_work(
    environ: Mapping[str, str] | None = None,
    *,
    now: float,
    window: int,
) -> bool:
    stamp = last_work_at(environ)
    if stamp is None:
        return False
    return now - stamp < window
