"""Local PostgreSQL session store. This device is the write owner of its own rows."""

from __future__ import annotations

import functools
import json
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Callable, Iterator
from typing import Any

import psycopg
from psycopg.rows import dict_row

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

CREATE TABLE IF NOT EXISTS wake_queue (
  activity_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  delivered_at TEXT
);
"""

OWNED_TABLES = frozenset(
    {
        "session",
        "activity",
        "task",
        "task_round",
        "agent",
        "checklist_item",
        "local_check",
        "review_gate",
        "open_work",
        "ping",
        "job",
    }
)

WAKE_ACTIVITY_TYPES = frozenset({"message", "pr.merged", "error.seen"})
DONE_WAKE_ACTIVITY_TYPES = frozenset({"pr.merged"})

EXECUTABLE_ACTIVITY_TYPES = frozenset(
    {
        "issue.write",
        "pr.open",
        "comment.post",
        "mail.ingest",
        "mail.seen",
        "mail.reply",
        "query.request",
        "subscription.set",
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


class StoreConnectionError(StoreError):
    """A lost/reset postgres connection specifically - distinct from a StoreError
    raised for a genuine data problem (a conflicting event, an origin_seq gap, an
    ownership violation). A caller may want to retry the former but must never
    silently retry or swallow the latter."""


def _wrap_pg_errors(fn: Callable[..., Any]) -> Callable[..., Any]:
    """A lost/reset postgres connection raises psycopg.OperationalError or
    psycopg.InterfaceError - plain Exception subclasses, not caught by callers
    (e.g. cmd_sync's reconnect loop) that only handle StoreConnectionError.
    Translate those specifically so those call sites can retry instead of dying.
    Any other psycopg.Error (DataError, IntegrityError, ProgrammingError, ...) is a
    genuine data/query problem, not a connection issue - it becomes a plain
    StoreError so it still fails loud with a clean message instead of an uncaught
    traceback, but is never mistaken for something safe to retry."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return fn(*args, **kwargs)
        except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
            raise StoreConnectionError(f"postgres error: {exc}") from exc
        except psycopg.Error as exc:
            raise StoreError(f"postgres error: {exc}") from exc

    return wrapper


class Store:
    def __init__(self, home: Path, dsn: str | None = None) -> None:
        self.home = Path(home)
        self.home.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
        self.identity_path = self.home / "device.json"
        self.path = self.home
        resolved = dsn if dsn not in (None, "") else os.environ.get("AGENT_PG_DSN")
        if not resolved:
            raise StoreError("postgres DSN is missing")
        from .pg import require_loopback_dsn

        require_loopback_dsn(resolved)
        self.dsn = resolved
        try:
            self.conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True)
            self.conn.execute("SET timezone TO 'UTC'")
        except psycopg.Error as exc:
            raise StoreError(f"cannot connect to postgres: {exc}") from exc
        self._lock = threading.RLock()
        self._init_schema()
        self._load_identity()

    def _init_schema(self) -> None:
        for stmt in SCHEMA.split(";"):
            stmt = stmt.strip()
            if stmt:
                self.conn.execute(stmt)

    def _load_identity(self) -> None:
        if self.identity_path.is_file():
            data = json.loads(self.identity_path.read_text(encoding="utf-8"))
            device_id = data.get("device_id")
            if not isinstance(device_id, str) or device_id == "":
                raise StoreError("device.json is missing device_id")
            existing = self.meta("device_id")
            if existing is not None and existing != device_id:
                raise StoreError("device.json does not match this database")
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
        tmp = self.identity_path.with_name(f"{self.identity_path.name}.tmp.{os.getpid()}")
        replaced = False
        try:
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.chmod(tmp, 0o600)
            tmp.replace(self.identity_path)
            replaced = True
        finally:
            if not replaced and tmp.exists():
                tmp.unlink()

    def close(self) -> None:
        self.conn.close()

    def reconnect(self) -> None:
        """Re-establish the postgres connection after StoreConnectionError. The old
        connection is already dead (that's what raised the error); discard it and
        open a fresh one, same as __init__. Unlike _wrap_pg_errors' narrower
        translation, ANY psycopg.Error here becomes StoreConnectionError - this
        call only ever does a bare connect + SET timezone, nothing that could
        legitimately raise a data/query error, so any failure here genuinely is a
        connection problem. cmd_sync's reconnect loop expects and handles exactly
        this type from this method. Holds self._lock so ThreadingHTTPServer
        handlers cannot race a reconnect against other store calls."""
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass
            try:
                self.conn = psycopg.connect(self.dsn, row_factory=dict_row, autocommit=True)
                self.conn.execute("SET timezone TO 'UTC'")
            except psycopg.Error as exc:
                raise StoreConnectionError(f"cannot reconnect to postgres: {exc}") from exc

    def device_id(self) -> str:
        value = self.meta("device_id")
        if value is None:
            raise StoreError("device_id is missing")
        return value

    @_wrap_pg_errors
    def meta(self, key: str) -> str | None:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key = %s", (key,)).fetchone()
            return None if row is None else row["value"]

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )
        if key in ("device_token", "github_login", "hub_url", "pair_challenge"):
            self.save_identity()

    def sync_get(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM sync_state WHERE key = %s", (key,)).fetchone()
        if row is None:
            return default
        return row["value"]

    def sync_set(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def next_seq(self) -> int:
        origin = self.device_id()
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (origin,))
        row = self.conn.execute(
            "SELECT COALESCE(MAX(origin_seq), 0) AS m FROM ledger_event WHERE origin_device_id = %s",
            (origin,),
        ).fetchone()
        return int(row["m"]) + 1

    @contextmanager
    def exclusive(self, key: str) -> Iterator[None]:
        with self._lock:
            self.conn.execute("SELECT pg_advisory_lock(hashtext(%s))", (key,))
            try:
                yield
            finally:
                self.conn.execute("SELECT pg_advisory_unlock(hashtext(%s))", (key,))

    @_wrap_pg_errors
    def write(self, table: str, op: str, row_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self.conn.transaction():
            return self._write_in_txn(table, op, row_id, payload)

    def write_with_advisory(
        self,
        table: str,
        op: str,
        row_id: str,
        payload: dict[str, Any],
        *,
        lock_key: str,
        skip: Callable[[], bool] | None = None,
    ) -> dict[str, Any] | None:
        with self._lock, self.conn.transaction():
            self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
            if skip is not None and skip():
                return None
            return self._write_in_txn(table, op, row_id, payload)

    def _write_in_txn(self, table: str, op: str, row_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if table not in OWNED_TABLES:
            raise StoreError(f"unknown table: {table}")
        if op not in ("insert", "update", "delete"):
            raise StoreError(f"unknown op: {op}")
        origin = self.device_id()
        existing = self.conn.execute(
            "SELECT origin_device_id FROM row_data WHERE table_name = %s AND row_id = %s",
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
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (origin, seq, table, op, row_id, encoded, occurred),
        )
        if op == "delete":
            deleted = self.conn.execute(
                "DELETE FROM row_data WHERE table_name = %s AND row_id = %s AND origin_device_id = %s "
                "RETURNING row_id",
                (table, row_id, origin),
            ).fetchone()
            if deleted is None:
                raise StoreError("cannot steal a row owned by another device")
        else:
            self._upsert_row(table, row_id, origin, encoded, occurred, require_newer=False)
        event = {
            "origin_device_id": origin,
            "origin_seq": seq,
            "table": table,
            "op": op,
            "row_id": row_id,
            "payload": payload,
            "occurred_at": occurred,
        }
        self._maybe_wake(event)
        self._maybe_work(event)
        return event

    @_wrap_pg_errors
    def apply_remote(self, event: dict[str, Any], *, wake: bool = True) -> None:
        with self._lock, self.conn.transaction():
            inserted = self._insert_event_idempotent(event)
            if inserted:
                self._materialize(event)
            if wake:
                self._maybe_wake(event)
            elif inserted:
                payload = event.get("payload")
                if isinstance(payload, dict) and payload.get("type") in WAKE_ACTIVITY_TYPES:
                    target = self._inbox_target(payload)
                    if target is not None and self._owns_session(target):
                        self.enqueue_wake(event["row_id"], target)
                        self.claim_wake(event["row_id"])

    def _insert_event_idempotent(self, event: dict[str, Any]) -> bool:
        encoded = dumps(event["payload"])
        self.conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (event["origin_device_id"],))
        existing = self.conn.execute(
            "SELECT payload, table_name, op, row_id, occurred_at FROM ledger_event "
            "WHERE origin_device_id = %s AND origin_seq = %s",
            (event["origin_device_id"], event["origin_seq"]),
        ).fetchone()
        if existing is not None:
            if (
                existing["payload"] != encoded
                or existing["table_name"] != event["table"]
                or existing["op"] != event["op"]
                or existing["row_id"] != event["row_id"]
                or existing["occurred_at"] != event["occurred_at"]
            ):
                raise StoreError(
                    f"conflicting event {event['origin_device_id']} seq {event['origin_seq']}"
                )
            return False
        last = self.conn.execute(
            "SELECT COALESCE(MAX(origin_seq), 0) AS m FROM ledger_event WHERE origin_device_id = %s",
            (event["origin_device_id"],),
        ).fetchone()
        last_seq = int(last["m"])
        if event["origin_seq"] != last_seq + 1:
            raise StoreError(
                f"origin_seq gap for {event['origin_device_id']}: have {last_seq}, got {event['origin_seq']}"
            )
        self.conn.execute(
            "INSERT INTO ledger_event (origin_device_id, origin_seq, table_name, op, row_id, payload, occurred_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
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
        return True

    def _upsert_row(
        self,
        table: str,
        row_id: str,
        origin: str,
        encoded: str,
        updated_at: str,
        *,
        require_newer: bool = False,
    ) -> None:
        newer = (
            " AND excluded.updated_at::timestamptz >= row_data.updated_at::timestamptz"
            if require_newer
            else ""
        )
        found = self.conn.execute(
            "INSERT INTO row_data (table_name, row_id, origin_device_id, payload, updated_at) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (table_name, row_id) DO UPDATE SET payload = excluded.payload, "
            "updated_at = excluded.updated_at "
            "WHERE row_data.origin_device_id = excluded.origin_device_id"
            f"{newer} "
            "RETURNING row_id",
            (table, row_id, origin, encoded, updated_at),
        ).fetchone()
        if found is not None:
            return
        current = self.conn.execute(
            "SELECT origin_device_id FROM row_data WHERE table_name = %s AND row_id = %s",
            (table, row_id),
        ).fetchone()
        if current is not None and current["origin_device_id"] != origin:
            raise StoreError("cannot steal a row owned by another device")

    def _materialize(self, event: dict[str, Any]) -> None:
        if event["op"] == "delete":
            deleted = self.conn.execute(
                "DELETE FROM row_data WHERE table_name = %s AND row_id = %s AND origin_device_id = %s "
                "RETURNING row_id",
                (event["table"], event["row_id"], event["origin_device_id"]),
            ).fetchone()
            if deleted is None:
                current = self.conn.execute(
                    "SELECT origin_device_id FROM row_data WHERE table_name = %s AND row_id = %s",
                    (event["table"], event["row_id"]),
                ).fetchone()
                if current is not None and current["origin_device_id"] != event["origin_device_id"]:
                    raise StoreError("cannot steal a row owned by another device")
            return
        self._upsert_row(
            event["table"],
            event["row_id"],
            event["origin_device_id"],
            dumps(event["payload"]),
            event["occurred_at"],
            require_newer=False,
        )

    @_wrap_pg_errors
    def apply_replica_row(self, row: dict[str, Any], *, wake: bool = True) -> None:
        if row.get("table") is None:
            raise StoreError("replica row missing table")
        if row["origin_device_id"] == self.device_id() and row.get("table") != "ping":
            return
        payload = row["payload"]
        if row.get("table") == "ping" and isinstance(payload, dict):
            current = self.row("ping", row["row_id"])
            if isinstance(current, dict) and current.get("acked_at") and not payload.get("acked_at"):
                payload = {**payload, "acked_at": current["acked_at"]}
        with self._lock, self.conn.transaction():
            self._upsert_row(
                row["table"],
                row["row_id"],
                row["origin_device_id"],
                dumps(payload),
                row["updated_at"],
                require_newer=row.get("table") != "ping",
            )
            event = {
                "origin_device_id": row["origin_device_id"],
                "table": row["table"],
                "op": "insert",
                "row_id": row["row_id"],
                "payload": payload,
            }
            if wake:
                self._maybe_wake(event)
            else:
                payload = event.get("payload")
                if isinstance(payload, dict) and payload.get("type") in WAKE_ACTIVITY_TYPES:
                    target = self._inbox_target(payload)
                    if target is not None and self._owns_session(target):
                        self.enqueue_wake(event["row_id"], target)
                        self.claim_wake(event["row_id"])

    @_wrap_pg_errors
    def pending_events(self) -> list[dict[str, Any]]:
        after = int(self.sync_get("pushed_origin_seq", "0") or "0")
        rows = self.conn.execute(
            "SELECT * FROM ledger_event WHERE origin_device_id = %s AND origin_seq > %s ORDER BY origin_seq ASC",
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

    @_wrap_pg_errors
    def mark_pushed(self, seq: int) -> None:
        self.sync_set("pushed_origin_seq", str(seq))

    def origin_cursor(self, origin: str) -> int:
        raw = self.sync_get(f"origin:{origin}", "0")
        return int(raw or "0")

    @_wrap_pg_errors
    def mark_origin(self, origin: str, seq: int) -> None:
        current = self.origin_cursor(origin)
        if seq > current:
            self.sync_set(f"origin:{origin}", str(seq))

    @_wrap_pg_errors
    def all_cursors(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT key, value FROM sync_state WHERE key LIKE 'origin:%'").fetchall()
        return {r["key"][7:]: int(r["value"]) for r in rows}

    @_wrap_pg_errors
    def rows(self, table: str) -> list[dict[str, Any]]:
        with self._lock:
            found = self.conn.execute(
            "SELECT row_id, origin_device_id, payload FROM row_data WHERE table_name = %s ORDER BY updated_at DESC",
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

    @_wrap_pg_errors
    def row(self, table: str, row_id: str) -> dict[str, Any] | None:
        with self._lock:
            found = self.conn.execute(
            "SELECT origin_device_id, payload FROM row_data WHERE table_name = %s AND row_id = %s",
            (table, row_id),
        ).fetchone()
        if found is None:
            return None
        payload = loads(found["payload"])
        if not isinstance(payload, dict):
            raise StoreError(f"{table} {row_id} payload is not an object")
        payload["_origin_device_id"] = found["origin_device_id"]
        return payload

    def last_own_activity_insert(self, activity_type: str) -> dict[str, Any] | None:
        """Newest own ledger insert of this activity type (origin_seq DESC). Full activity row dict or None."""
        with self._lock:
            found = self.conn.execute(
                "SELECT payload FROM ledger_event WHERE origin_device_id = %s AND table_name = %s AND op = %s "
                "ORDER BY origin_seq DESC",
                (self.device_id(), "activity", "insert"),
            ).fetchall()
        for row in found:
            event = loads(row["payload"])
            if not isinstance(event, dict):
                continue
            if event.get("type") == activity_type:
                return event
        return None

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "generated_at": utcnow(),
                "device_id": self.device_id(),
                "login": self.meta("github_login"),
                "sessions": self.rows("session"),
                "activity": self.rows("activity"),
                "tasks": self.rows("task"),
                "rounds": self.rows("task_round"),
                "agents": self.rows("agent"),
                "checklist": self.rows("checklist_item"),
                "checks": self.rows("local_check"),
                "gates": self.rows("review_gate"),
                "work": self.rows("open_work"),
                "pings": self.rows("ping"),
            }

    def enqueue_wake(self, activity_id: str, session_id: str) -> bool:
        with self._lock:
            found = self.conn.execute(
                "INSERT INTO wake_queue (activity_id, session_id, created_at, delivered_at) "
                "VALUES (%s, %s, %s, NULL) "
                "ON CONFLICT (activity_id) DO NOTHING "
                "RETURNING activity_id",
                (activity_id, session_id, utcnow()),
            ).fetchone()
            return found is not None

    def wake_delivered(self, activity_id: str) -> bool:
        with self._lock:
            found = self.conn.execute(
                "SELECT delivered_at FROM wake_queue WHERE activity_id = %s",
                (activity_id,),
            ).fetchone()
            return found is not None and found["delivered_at"] is not None

    def pending_wakes(self) -> list[dict[str, str]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT activity_id, session_id FROM wake_queue WHERE delivered_at IS NULL ORDER BY created_at ASC"
            ).fetchall()
            return [{"activity_id": r["activity_id"], "session_id": r["session_id"]} for r in rows]

    def pending_work(self) -> list[dict[str, Any]]:
        with self._lock:
            found = self.conn.execute(
                "SELECT row_id, origin_device_id, payload FROM row_data "
                "WHERE table_name = %s AND origin_device_id = %s "
                "ORDER BY updated_at ASC, row_id ASC",
                ("activity", self.device_id()),
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in found:
            payload = loads(row["payload"])
            if not isinstance(payload, dict):
                continue
            if payload.get("execution_status") != "pending":
                continue
            if payload.get("type") not in EXECUTABLE_ACTIVITY_TYPES:
                continue
            payload["_origin_device_id"] = row["origin_device_id"]
            out.append(payload)
        return out

    def claim_wake(self, activity_id: str) -> bool:
        with self._lock:
            found = self.conn.execute(
                "UPDATE wake_queue SET delivered_at = %s "
                "WHERE activity_id = %s AND delivered_at IS NULL "
                "RETURNING activity_id",
                (utcnow(), activity_id),
            ).fetchone()
            if found is not None:
                return True
            existing = self.conn.execute(
                "SELECT delivered_at FROM wake_queue WHERE activity_id = %s",
                (activity_id,),
            ).fetchone()
            if existing is None:
                self.conn.execute(
                    "INSERT INTO wake_queue (activity_id, session_id, created_at, delivered_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (activity_id, "", utcnow(), utcnow()),
                )
                return True
            return False

    def unclaim_wake(self, activity_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE wake_queue SET delivered_at = NULL WHERE activity_id = %s",
                (activity_id,),
            )

    def _inbox_target(self, payload: dict[str, Any]) -> str | None:
        typ = payload.get("type")
        if typ in ("pr.merged", "error.seen"):
            sid = payload.get("session_id")
            return sid if isinstance(sid, str) and sid else None
        if typ == "message":
            inner = payload.get("payload")
            if isinstance(inner, dict):
                to_s = inner.get("to_session")
                if isinstance(to_s, str) and to_s:
                    return to_s
        return None

    def _owns_session(self, session_id: str) -> bool:
        found = self.conn.execute(
            "SELECT origin_device_id FROM row_data WHERE table_name = %s AND row_id = %s",
            ("session", session_id),
        ).fetchone()
        return found is not None and found["origin_device_id"] == self.device_id()

    def _maybe_wake(self, event: dict[str, Any]) -> None:
        if event.get("table") != "activity" or event.get("op") == "delete":
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if payload.get("type") not in WAKE_ACTIVITY_TYPES:
            return
        if event.get("op") == "update" and payload.get("type") == "error.seen":
            return
        if (
            payload.get("type") in DONE_WAKE_ACTIVITY_TYPES
            and payload.get("execution_status") != "done"
        ):
            return
        target = self._inbox_target(payload)
        if target is None:
            return
        if not self._owns_session(target):
            return
        row_id = event["row_id"]
        if self.enqueue_wake(row_id, target):
            self.conn.execute("SELECT pg_notify(%s, %s)", ("agent_inbox", row_id))

    def _maybe_work(self, event: dict[str, Any]) -> None:
        if event.get("table") != "activity" or event.get("op") == "delete":
            return
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        if payload.get("execution_status") != "pending":
            return
        if payload.get("type") not in EXECUTABLE_ACTIVITY_TYPES:
            return
        sid = payload.get("session_id")
        if not isinstance(sid, str) or sid == "":
            return
        if not self._owns_session(sid):
            return
        self.conn.execute("SELECT pg_notify(%s, %s)", ("agent_work", event["row_id"]))
