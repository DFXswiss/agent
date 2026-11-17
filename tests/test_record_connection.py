"""The form of a connection string of the record database (RECORD-PROTOCOL.md sections 14 and 15).

Pure parsing: these tests need no database. The passwords are test constants of the form of
section 15 and open nothing.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg import sql

from agent_core.record import connection
from agent_core.record.connection import (
    ConnectionString,
    ConnectionStringError,
    identifier,
    libpq_supported,
    libpq_version,
    migrate_config,
    parse_connection_string,
    read_connection_string,
    reserved_name,
)

PASSWORD = "0123456789abcdef" * 4
OTHER_PASSWORD = "fedcba9876543210" * 4


def url(
    host: str = "db.example.org",
    sslmode: str = "require",
    *,
    role: str = "record_writer",
    password: str = PASSWORD,
    port: str = ":5432",
    database: str = "record",
) -> str:
    return f"postgresql://{role}:{password}@{host}{port}/{database}?sslmode={sslmode}"


def test_a_connection_string_is_parsed() -> None:
    parsed = parse_connection_string(url())
    assert parsed == ConnectionString(
        role="record_writer",
        password=PASSWORD,
        host="db.example.org",
        port=5432,
        database="record",
        sslmode="require",
    )


def test_the_port_defaults_to_5432() -> None:
    assert parse_connection_string(url(port="")).port == 5432
    assert parse_connection_string(url(port=":1")).port == 1
    assert parse_connection_string(url(port=":65535")).port == 65535


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "127.1.2.3", "[::1]"])
def test_disable_is_accepted_for_a_loopback_host(host: str) -> None:
    parsed = parse_connection_string(url(host, "disable"))
    assert parsed.sslmode == "disable"
    assert parsed.host == host.strip("[]")


@pytest.mark.parametrize(
    "host",
    [
        "postgres",
        "db.example.org",
        "localhost2",
        "localhost.example.org",
        "10.0.0.1",
        "128.0.0.1",
        "0.0.0.0",
        "[::2]",
        "[2001:db8::1]",
    ],
)
def test_disable_is_refused_for_any_other_host(host: str) -> None:
    with pytest.raises(ConnectionStringError):
        parse_connection_string(url(host, "disable"))


@pytest.mark.parametrize("sslmode", ["require", "verify-full"])
@pytest.mark.parametrize(
    "host", ["postgres", "db.example.org", "localhost", "127.0.0.1", "10.0.0.1", "[::1]", "[2001:db8::1]"]
)
def test_require_and_verify_full_are_accepted_for_any_host(host: str, sslmode: str) -> None:
    parsed = parse_connection_string(url(host, sslmode))
    assert parsed.sslmode == sslmode
    assert parsed.host == host.strip("[]")


MALFORMED = [
    ("sslmode missing", "postgresql://record_writer:" + PASSWORD + "@db.example.org:5432/record"),
    ("empty query", url().replace("?sslmode=require", "?")),
    ("sslmode prefer", url(sslmode="prefer")),
    ("sslmode verify-ca", url(sslmode="verify-ca")),
    ("sslmode in uppercase", url(sslmode="REQUIRE")),
    ("sslmode twice", url() + "&sslmode=require"),
    ("sslrootcert after sslmode", url() + "&sslrootcert=system"),
    ("sslrootcert before sslmode", url().replace("?sslmode=require", "?sslrootcert=system&sslmode=require")),
    ("another parameter", url() + "&connect_timeout=5"),
    ("a fragment", url() + "#x"),
    ("a trailing line feed", url() + "\n"),
    ("scheme postgres", url().replace("postgresql://", "postgres://")),
    ("no password", "postgresql://record_writer@db.example.org:5432/record?sslmode=require"),
    ("password of 63 characters", url(password=PASSWORD[:63])),
    ("password of 65 characters", url(password=PASSWORD + "a")),
    ("password in uppercase", url(password=PASSWORD.upper())),
    ("password percent-encoded", url(password="%30" + PASSWORD[1:])),
    ("role empty", url(role="")),
    ("role in uppercase", url(role="Record_writer")),
    ("role starting with a digit", url(role="1record")),
    ("role with -", url(role="record-writer")),
    ("role of 64 characters", url(role="r" * 64)),
    ("database empty", url(database="")),
    ("database with -", url(database="record-db")),
    ("database of 64 characters", url(database="d" * 64)),
    ("database with a path", url(database="record/extra")),
    ("host empty", url(host="")),
    ("host in uppercase", url(host="DB.example.org")),
    ("host with _", url(host="db_1.example.org")),
    ("host label starting with -", url(host="-db.example.org")),
    ("host label ending with -", url(host="db-.example.org")),
    ("host with an empty label", url(host="db..example.org")),
    ("host ending with a dot", url(host="db.example.org.")),
    ("host label of 64 characters", url(host="a" * 64 + ".example.org")),
    ("host of 254 characters", url(host=".".join(["a" * 63] * 3 + ["a" * 62]))),
    ("host outside ASCII", url(host="bücher.example.org")),
    ("IPv4 address above 255", url(host="1.2.3.256")),
    ("IPv4 address with three parts", url(host="127.0.1")),
    ("IPv4 address with a leading zero", url(host="127.0.0.01")),
    ("IPv6 address without brackets", url(host="::1")),
    ("IPv6 address that does not parse", url(host="[1:2:3]")),
    ("IPv6 address with a zone", url(host="[fe80::1%eth0]")),
    ("IPv6 address in uppercase", url(host="[2001:DB8::1]")),
    ("port 0", url(port=":0")),
    ("port 65536", url(port=":65536")),
    ("port with a leading zero", url(port=":05432")),
    ("port empty", url(port=":")),
    ("user information twice", url(host="x@db.example.org")),
]


@pytest.mark.parametrize("text", [case[1] for case in MALFORMED], ids=[case[0] for case in MALFORMED])
def test_a_malformed_connection_string_is_refused(text: str) -> None:
    with pytest.raises(ConnectionStringError) as refused:
        parse_connection_string(text)
    assert PASSWORD not in str(refused.value)
    assert "example" not in str(refused.value)


RESERVED = [
    "public",
    "none",
    "user",
    "current_user",
    "current_role",
    "session_user",
    "pg_",
    "pg_monitor",
    "pg_record",
]


@pytest.mark.parametrize("name", RESERVED)
def test_a_reserved_role_name_is_refused(name: str) -> None:
    with pytest.raises(ConnectionStringError) as refused:
        parse_connection_string(url(role=name))
    assert PASSWORD not in str(refused.value)
    assert name not in str(refused.value).split()


@pytest.mark.parametrize("name", RESERVED)
def test_a_reserved_database_name_is_refused(name: str) -> None:
    with pytest.raises(ConnectionStringError):
        parse_connection_string(url(database=name))


@pytest.mark.parametrize("name", ["publicx", "_public", "nones", "users", "pg", "pgx", "record_pg_", "_pg_x"])
def test_a_name_that_only_resembles_a_reserved_one_is_accepted(name: str) -> None:
    parsed = parse_connection_string(url(role=name, database=name))
    assert (parsed.role, parsed.database) == (name, name)


@pytest.mark.parametrize(
    ("name", "reserved"),
    [
        ("public", True),
        ("PUBLIC", True),
        ("Public", True),
        ("NONE", True),
        ("Current_User", True),
        ("CURRENT_ROLE", True),
        ("Session_User", True),
        ("USER", True),
        ("PG_monitor", True),
        ("Pg_x", True),
        ("record_writer", False),
        ("publicx", False),
        ("x_pg_", False),
    ],
)
def test_reserved_name_ignores_case(name: str, reserved: bool) -> None:
    assert reserved_name(name) is reserved


def test_identifier_accepts_a_name_of_section_15() -> None:
    assert identifier("record_writer") == sql.Identifier("record_writer")


@pytest.mark.parametrize(
    "name", [None, 1, "", "Record", "record-writer", "1record", "r" * 64, "public", "PUBLIC", "pg_x", "none"]
)
def test_identifier_refuses_a_name_of_another_form_or_a_reserved_name(name: object) -> None:
    with pytest.raises(ConnectionStringError):
        identifier(name)


def test_the_host_of_254_characters_case_is_really_254_characters() -> None:
    assert len(".".join(["a" * 63] * 3 + ["a" * 62])) == 254


def test_a_host_of_253_characters_is_accepted() -> None:
    host = ".".join(["a" * 63] * 3 + ["a" * 61])
    assert len(host) == 253
    assert parse_connection_string(url(host=host)).host == host


def test_a_value_that_is_not_text_is_refused() -> None:
    with pytest.raises(ConnectionStringError):
        parse_connection_string(None)


def test_the_password_never_appears_in_a_repr() -> None:
    parsed = parse_connection_string(url())
    assert PASSWORD not in repr(parsed)
    assert PASSWORD not in str(parsed)


def test_no_part_of_a_connection_string_appears_in_a_repr() -> None:
    parsed = parse_connection_string(
        url("db.example.org", "verify-full", role="t_role_x", port=":6543", database="t_database_y")
    )
    for shown in (repr(parsed), str(parsed)):
        for part in ("t_role_x", "db.example.org", "6543", "t_database_y", "verify-full", PASSWORD):
            assert part not in shown
    assert repr(parsed) == "ConnectionString()"


def test_connect_kwargs_for_require() -> None:
    assert parse_connection_string(url()).connect_kwargs() == {
        "host": "db.example.org",
        "hostaddr": "",
        "options": "",
        "port": 5432,
        "dbname": "record",
        "user": "record_writer",
        "password": PASSWORD,
        "sslmode": "require",
        "connect_timeout": 10,
    }


def test_connect_kwargs_for_an_ipv6_host() -> None:
    assert parse_connection_string(url("[::1]", "disable")).connect_kwargs()["host"] == "::1"


def test_verify_full_passes_sslrootcert_system_to_the_driver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PGSSLROOTCERT", "/tmp/not-the-system-store.pem")
    seen: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> str:
        seen.append(kwargs)
        return "connection"

    monkeypatch.setattr(psycopg, "connect", capture)
    parsed = parse_connection_string(url(sslmode="verify-full"))
    assert parsed.connect(autocommit=True) == "connection"
    assert seen == [
        {
            "host": "db.example.org",
            "hostaddr": "",
            "options": "",
            "port": 5432,
            "dbname": "record",
            "user": "record_writer",
            "password": PASSWORD,
            "sslmode": "verify-full",
            "connect_timeout": 10,
            "sslrootcert": "system",
            "autocommit": True,
        }
    ]


def test_require_passes_no_sslrootcert(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []
    monkeypatch.setattr(psycopg, "connect", lambda **kwargs: seen.append(kwargs))
    parse_connection_string(url()).connect()
    assert "sslrootcert" not in seen[0]


# --- variables ----------------------------------------------------------------------------------


def test_read_connection_string() -> None:
    env = {"AGENT_CORE_RECORD_DATABASE": url()}
    assert read_connection_string(env, "AGENT_CORE_RECORD_DATABASE").role == "record_writer"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "AGENT_CORE_RECORD_DATABASE is not set"),
        ({"AGENT_CORE_RECORD_DATABASE": ""}, "AGENT_CORE_RECORD_DATABASE is not set"),
        (
            {"AGENT_CORE_RECORD_DATABASE": url(sslmode="disable")},
            "AGENT_CORE_RECORD_DATABASE does not have the form of section 15",
        ),
    ],
)
def test_read_connection_string_refuses(env: dict[str, str], message: str) -> None:
    with pytest.raises(ConnectionStringError) as refused:
        read_connection_string(env, "AGENT_CORE_RECORD_DATABASE")
    assert str(refused.value) == message
    assert refused.value.__cause__ is None


def migrate_env(owner: str, writer: str) -> dict[str, str]:
    return {"AGENT_CORE_RECORD_OWNER_DATABASE": owner, "AGENT_CORE_RECORD_DATABASE": writer}


def test_migrate_config() -> None:
    owner, writer = migrate_config(
        migrate_env(url(role="record_owner", password=OTHER_PASSWORD), url(port=""))
    )
    assert (owner.role, writer.role) == ("record_owner", "record_writer")


@pytest.mark.parametrize(
    "env",
    [
        {},
        migrate_env("", url()),
        {"AGENT_CORE_RECORD_OWNER_DATABASE": url(role="record_owner")},
        migrate_env(url(role="record_owner"), ""),
        migrate_env(url(role="record_owner", sslmode="prefer"), url()),
        migrate_env(url(role="record_owner"), url(sslmode="prefer")),
        migrate_env(url(password=OTHER_PASSWORD), url()),
        migrate_env(url(role="record_owner", host="db2.example.org"), url()),
        migrate_env(url(role="record_owner", port=":5433"), url()),
        migrate_env(url(role="record_owner", database="record2"), url()),
        migrate_env(url(role="record_owner", sslmode="verify-full"), url()),
        migrate_env(url(role="record_owner"), url(role="public")),
        migrate_env(url(role="public"), url()),
        migrate_env(url(role="record_owner"), url(role="pg_write_all_data")),
        migrate_env(url(role="record_owner", database="public"), url(database="public")),
    ],
    ids=[
        "both missing",
        "owner empty",
        "writer missing",
        "writer empty",
        "owner malformed",
        "writer malformed",
        "the same role",
        "different hosts",
        "different ports",
        "different databases",
        "different sslmode",
        "writer named public",
        "owner named public",
        "writer named with pg_",
        "database named public",
    ],
)
def test_migrate_config_refuses(env: dict[str, str]) -> None:
    with pytest.raises(ConnectionStringError) as refused:
        migrate_config(env)
    assert PASSWORD not in str(refused.value)
    assert OTHER_PASSWORD not in str(refused.value)


# --- libpq --------------------------------------------------------------------------------------


def test_the_libpq_of_psycopg_is_version_16_or_later() -> None:
    assert libpq_version() == psycopg.pq.version()
    assert libpq_version() >= 160000
    assert libpq_supported()


@pytest.mark.parametrize(("version", "supported"), [(150010, False), (159999, False), (160000, True)])
def test_libpq_supported(monkeypatch: pytest.MonkeyPatch, version: int, supported: bool) -> None:
    monkeypatch.setattr(connection, "libpq_version", lambda: version)
    assert libpq_supported() is supported
