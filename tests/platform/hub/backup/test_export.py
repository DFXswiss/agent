"""The export file `agent-hub-export-v1` (02-data-model.md section 3, 04-crypto.md section 7): form, selection per org,
signature, no plaintext."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from datetime import UTC, date, datetime
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agent_core.signing import b64url_decode
from agent_hub.backup import tables
from agent_hub.backup.export import HEADER_ORDER, OrgUnknown, line, signed_text, write_export
from agent_hub.backup.tables import (
    EXCLUDED,
    RULES,
    Column,
    Table,
    ValueInvalid,
    ValueUnwritable,
    decode,
    encode,
    export_tables,
    schema_tables,
)
from tests.platform.hub.backup.support import CANARIES, Estate, lines, rows_of
from tests.platform.hub.conftest import HubHarness


def test_every_table_of_the_schema_has_a_rule_or_a_reason(owner: psycopg.Connection[Any]) -> None:
    found = schema_tables(owner)
    assert set(found) == {rule.table for rule in RULES} | set(EXCLUDED)
    assert len(RULES) == len({rule.table for rule in RULES})
    assert [table.name for table in export_tables(owner)] == [rule.table for rule in RULES]
    assert all(table.key for table in found.values()), "every table has a primary key (merge by key)"


def test_every_column_type_of_the_schema_has_a_form(owner: psycopg.Connection[Any]) -> None:
    kinds = {column.data_type for table in schema_tables(owner).values() for column in table.columns}
    assert kinds <= set(tables._ENCODERS) == set(tables._DECODERS)


def test_a_table_unknown_to_the_rules_stops_the_export(owner: psycopg.Connection[Any]) -> None:
    owner.execute("CREATE TABLE hub.extra (id integer PRIMARY KEY)")
    with pytest.raises(ValueUnwritable):
        export_tables(owner)


VALUES = [
    ("uuid", uuid.UUID("01a20491-a8c7-7645-8beb-c2984e623d82"), "01a20491-a8c7-7645-8beb-c2984e623d82"),
    ("text", "plain", "plain"),
    ("integer", 7, 7),
    ("smallint", 1, 1),
    ("bigint", 2**62, 2**62),
    ("boolean", True, True),
    ("timestamp with time zone", datetime(2026, 11, 21, 12, 0, 0, 123456, tzinfo=UTC), "2026-11-21T12:00:00.123456Z"),
    ("date", date(2026, 11, 21), "2026-11-21"),
    ("bytea", b"\x00\xff", "AP8"),
    ("jsonb", {"a": [1, "b"]}, {"a": [1, "b"]}),
    ("real", 0.25, 0.25),
    ("ARRAY", ["usb", "nfc"], ["usb", "nfc"]),
]


@pytest.mark.parametrize(("kind", "stored", "written"), VALUES)
def test_values_are_written_by_column_type_and_read_back(kind: str, stored: object, written: object) -> None:
    column = Column("c", kind)
    assert encode(column, stored) == written
    assert encode(column, None) is None
    assert decode(column, None) is None
    back = decode(column, written)
    assert getattr(back, "obj", back) == stored


@pytest.mark.parametrize(
    ("kind", "stored"),
    [
        ("uuid", "not-a-uuid-object"),
        ("text", 1),
        ("integer", True),
        ("boolean", 1),
        ("timestamp with time zone", "2026"),
        ("date", datetime(2026, 1, 1, tzinfo=UTC)),
        ("bytea", "text"),
        ("real", float("nan")),
        ("ARRAY", [1]),
        ("money", 1),
    ],
)
def test_a_value_outside_its_form_is_unwritable(kind: str, stored: object) -> None:
    with pytest.raises(ValueUnwritable, match="c has no form"):
        encode(Column("c", kind), stored)


@pytest.mark.parametrize(
    ("kind", "written"),
    [
        ("uuid", "01A20491-A8C7-7645-8BEB-C2984E623D82"),
        ("uuid", 1),
        ("text", 1),
        ("integer", True),
        ("integer", "1"),
        ("bigint", 2**63),
        ("boolean", 0),
        ("timestamp with time zone", "2026-11-21T12:00:00.123Z"),
        ("timestamp with time zone", "2026-13-21T12:00:00.123456Z"),
        ("date", "2026-02-30"),
        ("date", "21.11.2026"),
        ("bytea", "a+b/"),
        ("real", True),
        ("real", "0.5"),
        ("ARRAY", "usb"),
        ("ARRAY", [1]),
    ],
)
def test_a_written_value_outside_its_form_is_invalid(kind: str, written: object) -> None:
    with pytest.raises(ValueInvalid, match="c breaks its form"):
        decode(Column("c", kind), written)


def test_real_accepts_integers() -> None:
    assert decode(Column("c", "real"), 2) == 2.0


def test_table_names_and_keys() -> None:
    table = Table("t", (Column("a", "text"), Column("b", "integer")), ("b",))
    assert table.names == ("a", "b")
    assert table.key_of({"a": "x", "b": 3}) == (3,)


def test_the_file_is_in_the_writers_form_with_header_rows_and_a_signed_trailer(estate: Estate) -> None:
    data = estate.export()
    raw = data.split(b"\n")
    assert raw[-1] == b""
    items = lines(data)
    for text, item in zip(raw[:-1], items, strict=True):
        assert line(item) == text + b"\n"
    header, trailer = items[0], items[-1]
    assert tuple(header) == HEADER_ORDER
    assert header["format"] == "agent-hub-export-v1"
    assert header["org_id"] == estate.org
    assert header["exported_at"] == "2026-11-21T12:00:00.123000Z"
    assert header["hub_key_id"] == "hub-test"
    schema = estate.rows("SELECT checksum FROM hub.schema_version ORDER BY version")
    assert header["schema"] == [row[0] for row in schema]
    org = estate.rows("SELECT log_head_seq, log_head_hash FROM hub.org WHERE id = %s", estate.org)[0]
    assert header["log_head"] == {"seq": org[0], "hash": org[1]}
    platform = estate.rows("SELECT seq, entry_hash FROM hub.platform_entry ORDER BY seq DESC LIMIT 1")[0]
    assert header["platform_head"] == {"seq": platform[0], "hash": platform[1]}
    audit = rows_of(data, "audit")[-1]
    assert header["audit_head"] == {"seq": audit["seq"], "hash": audit["hash"]}
    body = data[: len(data) - len(raw[-2]) - 1]
    assert trailer["rows"] == len(items) - 2
    assert trailer["sha256"] == hashlib.sha256(body).hexdigest()
    signer = estate.hub.config.signer()
    signature = b64url_decode(trailer["signature"])
    assert signature is not None
    text = signed_text("hub-test", estate.org, trailer["rows"], trailer["sha256"])
    Ed25519PublicKey.from_public_bytes(signer.public_key).verify(signature, text)
    order = [rule.table for rule in RULES]
    positions = [order.index(item["table"]) for item in items[1:-1]]
    assert positions == sorted(positions)


def test_rows_follow_their_primary_key_and_their_columns(estate: Estate) -> None:
    data = estate.export()
    events = rows_of(data, "event")
    keys = [(row["scope_id"], row["scope_seq"]) for row in events]
    assert keys == sorted(keys)
    columns = [
        row[0]
        for row in estate.rows(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = 'hub' AND table_name = 'event'"
            " ORDER BY ordinal_position"
        )
    ]
    assert all(list(row) == columns for row in events)


def test_the_export_holds_one_org_and_the_hub_wide_chains(estate: Estate) -> None:
    data = estate.export()
    other = estate.other_org
    assert {row["org_id"] for row in rows_of(data, "event")} == {estate.org}
    assert [row["id"] for row in rows_of(data, "org")] == [estate.org]
    assert {row["org_id"] for row in rows_of(data, "membership_entry")} == {estate.org}
    assert all(row["org_id"] != other for table in ("task", "session", "wg") for row in rows_of(data, table))
    stranger = estate.scene.stranger.id
    assert stranger not in {row["id"] for row in rows_of(data, "principal")}
    count = estate.rows("SELECT count(*) FROM hub.platform_entry")[0][0]
    assert len(rows_of(data, "platform_entry")) == count
    assert estate.platform_device in {row["id"] for row in rows_of(data, "device")}
    assert {row["org_id"] for row in rows_of(data, "audit")} >= {estate.org, other}
    other_file = estate.export(other)
    principals = {row["id"] for row in rows_of(other_file, "principal")}
    assert estate.scene.reader.id not in principals
    assert estate.scene.writer.id not in principals  # a platform role does not make a user hub-wide
    assert estate.scene.writer.id in {row["user_id"] for row in rows_of(other_file, "platform_role")}
    assert rows_of(other_file, "platform_entry") == rows_of(data, "platform_entry")


def test_the_export_holds_no_plaintext(estate: Estate) -> None:
    data = estate.export()
    assert estate.rows("SELECT count(*) FROM hub.event WHERE kind = 'prompt'")[0][0] == 1
    for canary in CANARIES:
        raw = canary.encode()
        for form in (raw, raw.hex().encode(), base64.b64encode(raw), base64.urlsafe_b64encode(raw).rstrip(b"=")):
            assert form not in data
    assert all(table not in data.decode() for table in ('"table":"ceremony"', '"table":"device_presence"'))


def test_an_unknown_org_is_refused(hub: HubHarness) -> None:
    out = io.BytesIO()
    with hub.pool.connection() as connection:
        connection.autocommit = True
        with pytest.raises(OrgUnknown):
            write_export(connection, uuid.uuid4(), hub.config.signer(), out, hub.clock())


def test_an_unwritable_stored_value_stops_the_export(estate: Estate, owner: psycopg.Connection[Any]) -> None:
    owner.execute("ALTER TABLE hub.task ADD COLUMN extra money")
    owner.execute("UPDATE hub.task SET extra = 1")
    out = io.BytesIO()
    with estate.hub.pool.connection() as connection:
        connection.autocommit = True
        with pytest.raises(ValueUnwritable, match="extra"):
            write_export(connection, uuid.UUID(estate.org), estate.hub.config.signer(), out, estate.hub.clock())


def test_a_line_is_compact_ascii() -> None:
    assert line({"a": "Grüße", "b": [1, None]}) == b'{"a":"Gr\\u00fc\\u00dfe","b":[1,null]}\n'
    assert json.loads(line({"x": 0.1})) == {"x": 0.1}
