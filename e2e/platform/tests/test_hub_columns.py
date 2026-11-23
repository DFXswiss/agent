"""checks/hub_columns.py: the map of every hub column onto 04-crypto.md section 7, the value query and the report."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from checks import hub_columns
from checks.hub_columns import CATEGORIES, MAP, Column, checked, parse, report, values_sql
from support import CORE

MIGRATIONS = CORE / "src" / "agent_hub" / "db" / "migrations"
_TABLE_RE = re.compile(r"CREATE TABLE hub\.(\w+)\s*\((.*?)\n\);", re.DOTALL)
_COLUMN_RE = re.compile(r"^\s{2}\"?(\w+)\"?\s+\w", re.MULTILINE)
_ADD_RE = re.compile(r"ALTER TABLE hub\.(\w+)\s+ADD COLUMN (?:IF NOT EXISTS )?(\w+)", re.IGNORECASE)
_DROP_RE = re.compile(r"ALTER TABLE hub\.(\w+)\s+DROP COLUMN (?:IF EXISTS )?(\w+)", re.IGNORECASE)
_RENAME_RE = re.compile(r"ALTER TABLE hub\.(\w+)\s+RENAME COLUMN (\w+) TO (\w+)", re.IGNORECASE)
_NOT_COLUMNS = {"PRIMARY", "UNIQUE", "CHECK", "FOREIGN", "CONSTRAINT", "EXCLUDE"}


def migration_columns(directory: Path = MIGRATIONS) -> set[tuple[str, str]]:
    """The columns the hub migrations create (CREATE TABLE bodies and ADD COLUMN)."""
    found: set[tuple[str, str]] = set()
    for path in sorted(directory.glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        for table, body in _TABLE_RE.findall(text):
            found |= {(table, name) for name in _COLUMN_RE.findall(body) if name.upper() not in _NOT_COLUMNS}
        found |= set(_ADD_RE.findall(text))
        found -= set(_DROP_RE.findall(text))
        for table, old, new in _RENAME_RE.findall(text):
            found = (found - {(table, old)}) | {(table, new)}
    return found


def test_the_migration_parser_follows_added_dropped_and_renamed_columns(tmp_path: Path) -> None:
    (tmp_path / "0001_a.sql").write_text("CREATE TABLE hub.t (\n  a uuid NOT NULL,\n  b text,\n  PRIMARY KEY (a)\n);\n")
    (tmp_path / "0002_b.sql").write_text(
        "ALTER TABLE hub.t ADD COLUMN c int;\nALTER TABLE hub.t DROP COLUMN b;\n"
        "ALTER TABLE hub.t RENAME COLUMN c TO d;\n"
    )
    assert migration_columns(tmp_path) == {("t", "a"), ("t", "d")}


def test_the_map_covers_exactly_the_columns_of_the_migrations() -> None:
    # The live comparison runs in E2E-09 against the migrated database; this keeps the map current at the gate.
    assert set(MAP) == migration_columns()


def test_every_row_of_section_7_and_only_those_are_categories() -> None:
    used = {entry.category for entry in MAP.values()}
    assert used == set(CATEGORIES)
    assert MAP[("event", "ct")] == Column("event", "ct", "ciphertext", "bytes")
    assert MAP[("device", "capacity")].category == "metrics"
    assert [key for key, entry in MAP.items() if entry.category == "internal"] == [
        ("schema_version", name) for name in ("version", "name", "checksum", "applied_at")
    ]


def test_parse_refuses_bad_and_duplicate_entries() -> None:
    assert parse("t: ids | a:uuid b:code@logs\n  c:json\n") == {
        ("t", "a"): Column("t", "a", "ids", "uuid"),
        ("t", "b"): Column("t", "b", "logs", "code"),
        ("t", "c"): Column("t", "c", "ids", "json"),
    }
    for bad in ("t: nope | a:uuid", "t: ids | a:blob", "t: ids | a:uuid@nope"):
        with pytest.raises(ValueError, match="bad map entry"):
            parse(bad)
    with pytest.raises(ValueError, match="mapped twice"):
        parse("t: ids | a:uuid a:code")


def test_values_sql_checks_only_text_like_columns() -> None:
    entries = [
        Column("audit", "detail", "audit", "json"),
        Column("audit", "seq", "audit", "number"),
        Column("passkey", "transports", "auth", "codes"),
        Column("browser_session", "statement", "auth", "lines"),
        Column('we"ird', "kind", "events", "code"),
    ]
    sql = values_sql(entries)
    assert sql is not None
    assert sql.count("UNION ALL") == 3
    assert "jsonb_path_query(\"detail\"::jsonb, 'strict $.**')" in sql
    assert ".keyvalue().key" in sql  # object keys are checked as well
    assert "sample" not in sql  # no value leaves the database
    assert 'unnest("transports")' in sql
    assert "regexp_split_to_table(\"statement\", E'\\n')" in sql
    assert 'hub."we""ird"' in sql
    assert '"seq"' not in sql
    assert values_sql([Column("audit", "seq", "audit", "number")]) is None


def test_checked_skips_unknown_columns_and_wrong_types() -> None:
    columns = [
        {"table": "audit", "column": "detail", "type": "jsonb"},
        {"table": "audit", "column": "action", "type": "bytea"},
        {"table": "audit", "column": "invented", "type": "text"},
    ]
    assert checked(columns) == [MAP[("audit", "detail")]]


def test_report_lists_every_column_and_every_deviation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_columns, "MAP", parse("audit: audit | seq:number action:code detail:json gone:uuid"))
    columns = [
        {"table": "audit", "column": "seq", "type": "bigint"},
        {"table": "audit", "column": "action", "type": "bytea"},
        {"table": "audit", "column": "detail", "type": "jsonb"},
        {"table": "audit", "column": "extra", "type": "text"},
    ]
    bad = [
        {"table": "audit", "column": "detail", "bad": 2},
        {"table": "audit", "column": "action", "bad": 0},
    ]
    out = report(columns, bad)
    assert out["problems"] == [
        "audit.action: type bytea does not hold code",
        "audit.detail: 2 value(s) not made of tokens",
        "audit.extra: column not in the map of 04-crypto.md section 7",
        "audit.gone: mapped but not in the schema",
    ]
    rows = {row["column"]: row for row in out["columns"]}
    assert rows["seq"] == {
        "table": "audit",
        "column": "seq",
        "type": "bigint",
        "category": "audit",
        "section7": CATEGORIES["audit"],
        "content": "number",
        "values_checked": False,
    }
    assert rows["detail"]["bad"] == 2
    assert "bad" not in rows["action"]
    assert rows["extra"] == {"table": "audit", "column": "extra", "type": "text", "category": None}
    assert out["categories"] == {"audit": CATEGORIES["audit"]}
