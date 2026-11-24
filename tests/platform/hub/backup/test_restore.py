"""Refusals of `agent-hub restore` (DR-01): only an empty migrated database, only intact files of this hub, only
verified logs, events and audit chains, only projections that equal their sources; nothing is committed on a refusal.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from agent_hub.backup.export import line
from agent_hub.backup.restore import SESSION_STATEMENTS, RestoreFailed, _last, owner_string, restore, verify_platform
from agent_hub.config import load_config
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.db.migrate import OWNER_VARIABLE, WRITER_VARIABLE
from agent_hub.log.entries import entry_hash
from tests.platform.hub.backup.support import Estate, Json, find_row, lines, resign, table_rows
from tests.platform.hub.conftest import HubDatabase, hub_environ

Change = Callable[[list[Json]], None]


def run(target: HubDatabase, files: list[bytes], *, role: str | None = None, **environ: str) -> str:
    """Restore and return `restored <n>` or `failed <step>` (as the CLI prints it)."""
    config = load_config(hub_environ(target, **environ))
    login = role or target.owner_role
    with target.connect(login, autocommit=True) as connection:
        for statement in SESSION_STATEMENTS:
            connection.execute(statement)
        try:
            return f"restored {restore(connection, login, files, config)}"
        except RestoreFailed as failure:
            return f"failed {failure.step}"


def empty(target: HubDatabase) -> bool:
    return all(table_rows(target, name) == [] for name in ("principal", "event", "audit", "org"))


@pytest.fixture
def exported(estate: Estate) -> list[bytes]:
    return [estate.export(), estate.export(estate.other_org)]


def test_a_single_org_restores_alone(estate: Estate, target: HubDatabase) -> None:
    assert run(target, [estate.export(estate.other_org)]).startswith("restored ")


def test_restore_into_a_database_that_is_not_empty_is_refused(exported: list[bytes], target: HubDatabase) -> None:
    assert run(target, exported).startswith("restored")
    assert run(target, exported) == "failed not_empty"


def test_restore_into_an_unmigrated_or_differently_migrated_database_is_refused(
    exported: list[bytes], target: HubDatabase
) -> None:
    with target.admin() as admin:
        admin.execute("DELETE FROM hub.schema_version WHERE version = 6")
    assert run(target, exported) == "failed schema"
    assert empty(target)


def test_the_restore_runs_as_the_owner_only(exported: list[bytes], target: HubDatabase) -> None:
    assert run(target, exported, role=target.writer_role) == "failed owner"


def test_a_concurrent_lock_makes_the_restore_wait_and_give_up(exported: list[bytes], target: HubDatabase) -> None:
    config = load_config(hub_environ(target))
    with target.connect(target.writer_role) as holder:
        holder.execute("LOCK TABLE hub.task IN ROW EXCLUSIVE MODE")
        with target.connect(target.owner_role, autocommit=True) as connection:
            for statement in SESSION_STATEMENTS:
                connection.execute(statement)
            with pytest.raises(RestoreFailed, match="lock"):
                restore(connection, target.owner_role, exported, config, lock_timeout_ms=200)


@pytest.mark.parametrize(
    ("damage", "step"),
    [
        (lambda data: data[:-1], "file:end"),
        (lambda data: data.split(b"\n", 1)[0] + b"\n", "file:end"),
        (lambda data: b"[]\n" + data.split(b"\n", 1)[1], "file:line"),
        (lambda data: b'{"a":1,"a":2}\n' + data.split(b"\n", 1)[1], "file:line"),
        (lambda data: b'{"a": 1}\n' + data.split(b"\n", 1)[1], "file:line"),
        (lambda data: b"\xff\n" + data.split(b"\n", 1)[1], "file:line"),
        (lambda data: b'{"a":NaN}\n' + data.split(b"\n", 1)[1], "file:line"),
        (lambda data: data.replace(b'"state":"working"', b'"state":"idle"', 1), "file:digest"),
        (lambda data: data.rsplit(b"\n", 2)[0] + b'\n{"trailer":"x"}\n', "file:trailer"),
    ],
)
def test_a_damaged_file_is_refused(
    estate: Estate, target: HubDatabase, damage: Callable[[bytes], bytes], step: str
) -> None:
    assert run(target, [damage(estate.export())]) == f"failed {step}"
    assert empty(target)


def _first(table: str, predicate: Callable[[Json], bool] | None = None) -> Callable[[list[Json]], Json]:
    return lambda body: find_row(body, table, predicate)


def _set(table: str, column: str, value: object, predicate: Callable[[Json], bool] | None = None) -> Change:
    def change(body: list[Json]) -> None:
        find_row(body, table, predicate)[column] = value

    return change


def _header(name: str, value: object) -> Change:
    def change(body: list[Json]) -> None:
        body[0][name] = value

    return change


def _drop(table: str, predicate: Callable[[Json], bool] | None = None) -> Change:
    def change(body: list[Json]) -> None:
        for index, item in enumerate(body[1:], start=1):
            if item["table"] == table and (predicate is None or predicate(item["row"])):
                del body[index]
                return

    return change


def _swap_lines(first: str, second: str) -> Change:
    def change(body: list[Json]) -> None:
        a = next(index for index, item in enumerate(body) if index and item["table"] == first)
        b = next(index for index, item in enumerate(body) if index and item["table"] == second)
        body[a], body[b] = body[b], body[a]

    return change


def _replace(index: int, value: Json) -> Change:
    def change(body: list[Json]) -> None:
        body[index] = value

    return change


def _event_text(mutate: Callable[[Json], None]) -> Change:
    def change(body: list[Json]) -> None:
        row = find_row(body, "event", lambda row: row["kind"] == "prompt")
        header = json.loads(row["header_text"])
        mutate(header)
        row["header_text"] = json.dumps(header, separators=(",", ":"))

    return change


def _entry_body(table: str, mutate: Callable[[Json], None], last: bool = False) -> Change:
    def change(body: list[Json]) -> None:
        rows = [item["row"] for item in body[1:] if item["table"] == table]
        row = rows[-1] if last else rows[0]
        entry = json.loads(row["entry_text"])
        mutate(entry)
        row["entry_text"] = json.dumps(entry, separators=(",", ":"))

    return change


def _both(*changes: Change) -> Change:
    def change(body: list[Json]) -> None:
        for item in changes:
            item(body)

    return change


def _platform_root_resigned(body: list[Json]) -> None:
    """The first platform entry with another time and its own hash: the chain to the next entry breaks."""
    row = find_row(body, "platform_entry")
    entry = json.loads(row["entry_text"])
    entry["body"]["ts"] = "2026-11-21T12:00:01.000Z"
    row["entry_text"] = json.dumps(entry, separators=(",", ":"))
    row["entry_hash"] = entry_hash(entry)


def _org_head(body: list[Json]) -> None:
    row = find_row(body, "org")
    row["log_head_hash"] = "x"
    body[0]["log_head"] = {"seq": row["log_head_seq"], "hash": "x"}


def _extra_scope_head(body: list[Json]) -> None:
    index = max(i for i, item in enumerate(body) if i and item["table"] == "scope_head")
    body.insert(
        index + 1,
        {"table": "scope_head", "row": {"scope_id": "ffffffff-a8c7-7645-8beb-c2984e623d82", "scope": "wg", "seq": 3}},
    )


def _foreign_log(body: list[Json]) -> None:
    index = max(i for i, item in enumerate(body) if i and item["table"] == "membership_entry")
    row = dict(body[index]["row"], org_id="ffffffff-a8c7-7645-8beb-c2984e623d82")
    body.insert(index + 1, {"table": "membership_entry", "row": row})


def _not_prompt(row: Json) -> bool:
    return bool(row["kind"] != "prompt")


SIGNED_DAMAGE: list[tuple[str, Change, str]] = [
    ("header members", _header("extra", 1), "file:header"),
    ("format", _header("format", "agent-hub-export-v2"), "file:header"),
    ("org id type", _header("org_id", 1), "file:header"),
    ("schema type", _header("schema", "x"), "file:header"),
    ("schema items", _header("schema", [1]), "file:header"),
    ("log head", _header("log_head", None), "file:header"),
    ("log head members", _header("log_head", {"seq": 1}), "file:header"),
    ("log head seq", _header("log_head", {"seq": True, "hash": "h"}), "file:header"),
    ("platform head", _header("platform_head", 1), "file:header"),
    ("audit head", _header("audit_head", {"seq": 1, "hash": 2}), "file:header"),
    ("row members", _replace(1, {"table": "principal"}), "file:row"),
    ("unknown table", _replace(1, {"table": "ceremony", "row": {}}), "file:row"),
    ("row columns", _set("principal", "extra", 1), "file:row"),
    ("row type", _replace(1, {"table": "principal", "row": []}), "file:row"),
    ("table order", _swap_lines("principal", "device"), "file:order"),
    ("value form", _set("principal", "created_at", "yesterday"), "file:value"),
    ("org missing", _drop("org"), "file:org"),
    ("other org", _header("org_id", "01a20491-a8c7-7645-8beb-c2984e623d82"), "file:org"),
    ("log head differs", _header("log_head", {"seq": 0, "hash": "x"}), "file:heads"),
    ("platform head differs", _header("platform_head", None), "file:heads"),
    ("audit head differs", _header("audit_head", None), "file:heads"),
    ("platform entry hash", _set("platform_entry", "entry_hash", "x"), "verify:platform_log"),
    ("platform entry seq", _set("platform_entry", "seq", 99), "file:heads"),
    ("platform chain", _platform_root_resigned, "verify:platform_log"),
    ("org head", _org_head, "verify:org_head"),
    ("scope head without events", _extra_scope_head, "verify:scope_head"),
    ("log of an org not in the file", _foreign_log, "verify:membership_log"),
    (
        "platform signature",
        _entry_body("platform_entry", lambda entry: entry["body"].update(ts="2026-11-21T12:00:01.000Z")),
        "verify:platform_log",
    ),
    ("platform entry form", _entry_body("platform_entry", lambda entry: entry.update(body=1)), "verify:platform_log"),
    ("membership entry hash", _set("membership_entry", "entry_hash", "x"), "verify:membership_log"),
    (
        "membership entry form",
        _entry_body("membership_entry", lambda entry: entry.update(body=[])),
        "verify:membership_log",
    ),
    (
        "membership signature",
        _entry_body("membership_entry", lambda entry: entry["body"].update(ts="2026-11-21T12:00:01.000Z")),
        "verify:membership_log",
    ),
    ("founder missing", _drop("user_identity", lambda row: True), "verify:founder"),
    (
        "log without org",
        _set("membership_entry", "org_id", "01a20491-a8c7-7645-8beb-c2984e623d82"),
        "verify:membership_log",
    ),
    (
        "event header not canonical",
        lambda body: find_row(body, "event").update(header_text=" " + find_row(body, "event")["header_text"]),
        "verify:event_header",
    ),
    ("event header not json", _set("event", "header_text", "{"), "verify:event"),
    ("event ct", _set("event", "ct", b64u_encode(b"x" * 40), lambda row: row["kind"] == "prompt"), "verify:event"),
    (
        "event author",
        _event_text(lambda header: header.update(author="01a20491-a8c7-7645-8beb-c2984e623d82")),
        "verify:event",
    ),
    (
        "event column",
        _set("event", "kind", "settings.workgroup", lambda row: row["kind"] == "prompt"),
        "verify:event_columns",
    ),
    ("event size", _set("event", "size", 1, lambda row: row["kind"] == "prompt"), "verify:event_columns"),
    (
        "event author kind",
        _set("event", "author_kind", "device", lambda row: row["kind"] == "prompt"),
        "verify:event_columns",
    ),
    (
        "event ts",
        _set("event", "ts", "2026-11-21T12:00:00.123001Z", lambda row: row["kind"] == "prompt"),
        "verify:event_columns",
    ),
    ("event gap", _drop("event", _not_prompt), "verify:scope_seq"),
    ("scope head", _set("scope_head", "seq", 999), "verify:scope_head"),
    ("scope head missing", _drop("scope_head"), "verify:scope_head"),
    ("audit row", _set("audit", "result", "denied"), "verify:audit_chain"),
    ("checkpoint signature", _set("audit_checkpoint", "signature", b64u_encode(b"\x01" * 64)), "verify:checkpoint"),
    ("checkpoint key", _set("audit_checkpoint", "hub_key_id", "other-key"), "verify:checkpoint"),
    ("checkpoint hash", _set("audit_checkpoint", "hash", "0" * 64), "verify:checkpoint"),
    ("projection", _set("org_member", "role", "member", lambda row: row["role"] == "admin"), "projections"),
    ("insert", _set("wg", "is_default", None), "insert"),
]


@pytest.mark.parametrize(
    ("change", "step"), [item[1:] for item in SIGNED_DAMAGE], ids=[item[0] for item in SIGNED_DAMAGE]
)
def test_a_changed_line_is_refused_even_when_signed(
    estate: Estate, target: HubDatabase, change: Change, step: str
) -> None:
    """What only a holder of the hub key could produce: the semantic checks refuse it before anything is written."""
    assert run(target, [resign(estate.export(), estate.hub, change)]) == f"failed {step}"
    assert empty(target)


def test_a_file_of_another_hub_key_is_refused(estate: Estate, target: HubDatabase) -> None:
    data = estate.export()
    assert run(target, [data], AGENT_HUB_SIGNING_KEY="5b" * 32) == "failed signature"
    assert run(target, [data], AGENT_HUB_HUB_KEY_ID="hub-other") == "failed signature"
    items = lines(data)
    items[-1]["signature"] = "AA"
    assert run(target, [b"".join(line(item) for item in items)]) == "failed signature"


def test_an_org_twice_is_refused(estate: Estate, target: HubDatabase) -> None:
    data = estate.export()
    assert run(target, [data, data]) == "failed file:duplicate_org"


def test_two_exports_disagreeing_on_an_append_only_row_are_refused(
    estate: Estate, exported: list[bytes], target: HubDatabase
) -> None:
    changed = resign(exported[1], estate.hub, _set("audit", "target_id", "changed", lambda row: row["seq"] == 1))
    assert run(target, [exported[0], changed]) == "failed conflict"


def test_two_exports_of_one_moment_disagreeing_on_a_row_are_refused(
    estate: Estate, exported: list[bytes], target: HubDatabase
) -> None:
    changed = resign(exported[1], estate.hub, _set("platform_role", "revoked_seq", 9))
    assert run(target, [exported[0], changed]) == "failed conflict"


def test_the_newest_export_wins_for_a_changed_row(estate: Estate, target: HubDatabase) -> None:
    first = estate.export()
    estate.hub.clock.advance(60)
    with estate.hub.pool.connection() as connection:
        connection.execute("UPDATE hub.metric_rollup_cursor SET received_through = '2026-11-22T00:00:00Z'")
    other = estate.export(estate.other_org)
    assert run(target, [other, first]).startswith("restored")
    with target.admin() as admin:
        cursor = admin.execute("SELECT received_through = '2026-11-22T00:00:00Z' FROM hub.metric_rollup_cursor")
        assert cursor.fetchall() == [(True,)]


def test_the_owner_connection_string_belongs_to_the_same_database(target: HubDatabase) -> None:
    good = {OWNER_VARIABLE: target.owner_url, WRITER_VARIABLE: target.writer_url}
    assert owner_string(good).role == target.owner_role
    for broken in (
        {WRITER_VARIABLE: target.writer_url},
        {OWNER_VARIABLE: target.writer_url, WRITER_VARIABLE: target.writer_url},
        {
            OWNER_VARIABLE: target.url(target.owner_role, target.owner_password, database="other"),
            WRITER_VARIABLE: target.writer_url,
        },
    ):
        with pytest.raises(RestoreFailed, match="config"):
            owner_string(broken)


def test_a_hub_without_a_platform_log_has_no_platform_head() -> None:
    assert _last([], "seq", "entry_hash") is None
    assert verify_platform([], b"\x00" * 32) is None
