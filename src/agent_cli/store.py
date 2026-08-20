"""Local SQLite session store. This device is the write owner of its own rows."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ledger_event (
  origin_device_id TEXT NOT NULL,
  origin_seq INTEGER NOT NULL,
  table_name TEXT NOT NULL,
  op TEXT NOT NULL,
  row_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  PRIMARY KEY (origin_device_id, origin_seq)
);

CREATE TABLE IF NOT EXISTS row_data (
  table_name TEXT NOT NULL,
  row_id TEXT NOT NULL,
  origin_device_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (table_name, row_id)
);

CREATE TABLE IF NOT EXISTS sync_state (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

OWNED_TABLES = frozenset(
    {
        "session",
        "task",
        "task_round",
        "agent",
        "checklist_item",
        "local_check",
        "review_gate",
        "open_work",
        "ping",
    }
)


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dumps(payload: Any) -> str:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def loads(raw: str) -> Any:
    return json.loads(raw)


class StoreError(SystemExit):
    pass


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        self.path = path
        self.identity_path = path.parent / "device.json"
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()
        os.chmod(path, 0o600)
        self._load_identity()

    def _load_identity(self) -> None:
        if self.identity_path.is_file():
            data = json.loads(self.identity_path.read_text(encoding="utf-8"))
            device_id = data.get("device_id")
            if not isinstance(device_id, str) or device_id == "":
                raise StoreError("device.json is missing device_id")
            self.set_meta("device_id", device_id)
            for key in ("device_token", "github_login", "hub_url", "pair_challenge"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    self.set_meta(key, value)
            return
        device_id = self.meta("device_id") or str(uuid.uuid4())
        self.set_meta("device_id", device_id)
        self.save_identity()

    def save_identity(self) -> None:
        payload = {
            "device_id": self.device_id(),
            "device_token": self.meta("device_token"),
            "github_login": self.meta("github_login"),
            "hub_url": self.meta("hub_url"),
            "pair_challenge": self.meta("pair_challenge"),
        }
        tmp = self.identity_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.identity_path)

    def close(self) -> None:
        self.conn.close()

    def device_id(self) -> str:
        value = self.meta("device_id")
        if value is None:
            raise StoreError("device_id is missing")
        return value

    def meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()
        if key in ("device_token", "github_login", "hub_url", "pair_challenge"):
            self.save_identity()

    def sync_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return default
        return row["value"]

    def sync_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.conn.commit()

    def next_seq(self) -> int:
        self.conn.execute("BEGIN IMMEDIATE")
        row = self.conn.execute(
            "SELECT COALESCE(MAX(origin_seq), 0) AS m FROM ledger_event WHERE origin_device_id = ?",
            (self.device_id(),),
        ).fetchone()
        return int(row["m"]) + 1

    def write(self, table: str, op: str, row_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if table not in OWNED_TABLES:
            raise StoreError(f"unknown table: {table}")
        if op not in ("insert", "update", "delete"):
            raise StoreError(f"unknown op: {op}")
        origin = self.device_id()
        existing = self.conn.execute(
            "SELECT origin_device_id FROM row_data WHERE table_name = ? AND row_id = ?",
            (table, row_id),
        ).fetchone()
        if existing is not None and existing["origin_device_id"] != origin:
            raise StoreError("cannot mutate a row owned by another device")
        if op == "insert" and existing is not None:
            raise StoreError(f"{table} {row_id} already exists")
        if op in ("update", "delete") and existing is None:
            raise StoreError(f"{table} {row_id} does not exist")
        seq = self.next_seq()
        occurred = utcnow()
        encoded = dumps(payload)
        self.conn.execute(
            "INSERT INTO ledger_event (origin_device_id, origin_seq, table_name, op, row_id, payload, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (origin, seq, table, op, row_id, encoded, occurred),
        )
        if op == "delete":
            self.conn.execute("DELETE FROM row_data WHERE table_name = ? AND row_id = ?", (table, row_id))
        else:
            self.conn.execute(
                "INSERT INTO row_data (table_name, row_id, origin_device_id, payload, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(table_name, row_id) DO UPDATE SET payload = excluded.payload, "
                "origin_device_id = excluded.origin_device_id, updated_at = excluded.updated_at",
                (table, row_id, origin, encoded, occurred),
            )
        self.conn.commit()
        return {
            "origin_device_id": origin,
            "origin_seq": seq,
            "table": table,
            "op": op,
            "row_id": row_id,
            "payload": payload,
            "occurred_at": occurred,
        }

    def apply_remote(self, event: dict[str, Any]) -> None:
        origin = event["origin_device_id"]
        if origin == self.device_id():
            self._insert_event_idempotent(event)
            self._materialize(event)
            return
        self._insert_event_idempotent(event)
        self._materialize(event)

    def _insert_event_idempotent(self, event: dict[str, Any]) -> None:
        encoded = dumps(event["payload"])
        existing = self.conn.execute(
            "SELECT payload, table_name, op, row_id FROM ledger_event WHERE origin_device_id = ? AND origin_seq = ?",
            (event["origin_device_id"], event["origin_seq"]),
        ).fetchone()
        if existing is not None:
            if (
                existing["payload"] != encoded
                or existing["table_name"] != event["table"]
                or existing["op"] != event["op"]
                or existing["row_id"] != event["row_id"]
            ):
                raise StoreError(
                    f"conflicting event {event['origin_device_id']} seq {event['origin_seq']}"
                )
            return
        last = self.conn.execute(
            "SELECT COALESCE(MAX(origin_seq), 0) AS m FROM ledger_event WHERE origin_device_id = ?",
            (event["origin_device_id"],),
        ).fetchone()
        last_seq = int(last["m"])
        if event["origin_seq"] != last_seq + 1:
            raise StoreError(
                f"origin_seq gap for {event['origin_device_id']}: have {last_seq}, got {event['origin_seq']}"
            )
        self.conn.execute(
            "INSERT INTO ledger_event (origin_device_id, origin_seq, table_name, op, row_id, payload, occurred_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event["origin_device_id"],
                event["origin_seq"],
                event["table"],
                event["op"],
                event["row_id"],
                encoded,
                event["occurred_at"],
            ),
        )
        self.conn.commit()

    def _materialize(self, event: dict[str, Any]) -> None:
        if event["op"] == "delete":
            self.conn.execute(
                "DELETE FROM row_data WHERE table_name = ? AND row_id = ?",
                (event["table"], event["row_id"]),
            )
        else:
            self.conn.execute(
                "INSERT INTO row_data (table_name, row_id, origin_device_id, payload, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(table_name, row_id) DO UPDATE SET payload = excluded.payload, "
                "origin_device_id = excluded.origin_device_id, updated_at = excluded.updated_at",
                (
                    event["table"],
                    event["row_id"],
                    event["origin_device_id"],
                    dumps(event["payload"]),
                    event["occurred_at"],
                ),
            )
        self.conn.commit()

    def apply_replica_row(self, row: dict[str, Any]) -> None:
        if row["origin_device_id"] == self.device_id():
            return
        if row.get("table") is None:
            raise StoreError("replica row missing table")
        self.conn.execute(
            "INSERT INTO row_data (table_name, row_id, origin_device_id, payload, updated_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(table_name, row_id) DO UPDATE SET payload = excluded.payload, "
            "origin_device_id = excluded.origin_device_id, updated_at = excluded.updated_at",
            (
                row["table"],
                row["row_id"],
                row["origin_device_id"],
                dumps(row["payload"]),
                row["updated_at"],
            ),
        )
        self.conn.commit()

    def pending_events(self) -> list[dict[str, Any]]:
        after = int(self.sync_get("pushed_origin_seq", "0") or "0")
        rows = self.conn.execute(
            "SELECT * FROM ledger_event WHERE origin_device_id = ? AND origin_seq > ? ORDER BY origin_seq ASC",
            (self.device_id(), after),
        ).fetchall()
        return [
            {
                "origin_device_id": r["origin_device_id"],
                "origin_seq": r["origin_seq"],
                "table": r["table_name"],
                "op": r["op"],
                "row_id": r["row_id"],
                "payload": loads(r["payload"]),
                "occurred_at": r["occurred_at"],
            }
            for r in rows
        ]

    def mark_pushed(self, seq: int) -> None:
        self.sync_set("pushed_origin_seq", str(seq))

    def origin_cursor(self, origin: str) -> int:
        raw = self.sync_get(f"origin:{origin}", "0")
        return int(raw or "0")

    def mark_origin(self, origin: str, seq: int) -> None:
        current = self.origin_cursor(origin)
        if seq > current:
            self.sync_set(f"origin:{origin}", str(seq))

    def all_cursors(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT key, value FROM sync_state WHERE key LIKE 'origin:%'").fetchall()
        return {r["key"][7:]: int(r["value"]) for r in rows}

    def rows(self, table: str) -> list[dict[str, Any]]:
        found = self.conn.execute(
            "SELECT row_id, origin_device_id, payload FROM row_data WHERE table_name = ? ORDER BY updated_at DESC",
            (table,),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for row in found:
            payload = loads(row["payload"])
            if not isinstance(payload, dict):
                raise StoreError(f"{table} {row['row_id']} payload is not an object")
            payload["_origin_device_id"] = row["origin_device_id"]
            out.append(payload)
        return out

    def row(self, table: str, row_id: str) -> dict[str, Any] | None:
        found = self.conn.execute(
            "SELECT origin_device_id, payload FROM row_data WHERE table_name = ? AND row_id = ?",
            (table, row_id),
        ).fetchone()
        if found is None:
            return None
        payload = loads(found["payload"])
        if not isinstance(payload, dict):
            raise StoreError(f"{table} {row_id} payload is not an object")
        payload["_origin_device_id"] = found["origin_device_id"]
        return payload

    def snapshot(self) -> dict[str, Any]:
        return {
            "generated_at": utcnow(),
            "device_id": self.device_id(),
            "login": self.meta("github_login"),
            "sessions": self.rows("session"),
            "tasks": self.rows("task"),
            "rounds": self.rows("task_round"),
            "agents": self.rows("agent"),
            "checklist": self.rows("checklist_item"),
            "checks": self.rows("local_check"),
            "gates": self.rows("review_gate"),
            "work": self.rows("open_work"),
            "pings": self.rows("ping"),
        }
