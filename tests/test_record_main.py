"""The entry of `python -m agent_core.record` in process (src/agent_core/record/__main__.py).

`main` is called directly, and the module's `__main__` guard is executed with `runpy`, so that
every line and branch of the entry runs in the test process. The same command as a separate
process is tested in test_record_migrate.py.

RECORD-PROTOCOL.md section 11.4 and section 14 state the behaviours of the entry tested here
(decisions 81 and 82): `migrate` with any argument prints `failed config` and exits 1 without
connecting; `export`, `verify` and `restore` (change 3) are dispatched to their commands; and no
command or an unknown command prints exactly the usage line of section 11.4 after change 3,
`usage: python -m agent_core.record {migrate,export,verify,restore}`, and one `\\n` to stderr,
nothing to stdout, reads no environment variable, does not connect and exits 2. Every test of the
usage line compares with the literal text of section 11.4, never with the constant of the module
alone.
"""

from __future__ import annotations

import runpy
import sys
from collections.abc import Iterator, Mapping
from typing import Any

import psycopg
import pytest

from agent_core.record.__main__ import USAGE, main
from tests.conftest import RecordDatabase, schema_exists

PACKAGE = "agent_core.record"
# The usage line of section 11.4 after change 3, literally.
USAGE_LINE = "usage: python -m agent_core.record {migrate,export,verify,restore}"
RECORD_VARIABLES = ("AGENT_CORE_RECORD_DATABASE", "AGENT_CORE_RECORD_OWNER_DATABASE")

# Section 14: "`migrate` with an argument after `migrate` (each of `--help`, `-h`, `x`, `migrate`
# and an empty string, and two arguments)".
MIGRATE_ARGUMENTS = [
    ["migrate", "--help"],
    ["migrate", "-h"],
    ["migrate", "x"],
    ["migrate", "migrate"],
    ["migrate", ""],
    ["migrate", "x", "x"],
]
MIGRATE_ARGUMENT_IDS = ["--help", "-h", "x", "migrate", "an empty string", "two arguments"]

# Section 14: "without an argument, with an empty string as the command, with `-h`, `--help`,
# `Migrate` and an unknown word as the command". After change 3, `export`, `verify` and `restore`
# are commands (test_main_runs_the_commands_of_change_3). Section 11.4 compares the first argument
# byte for byte, so `Export`, a trailing space and an empty first argument before `migrate` are
# unknown commands too.
UNKNOWN_COMMANDS = [
    [],
    [""],
    ["-h"],
    ["--help"],
    ["Migrate"],
    ["unknown"],
    ["import"],
    ["Export"],
    ["migrate "],
    ["", "migrate"],
]
UNKNOWN_COMMAND_IDS = [
    "no argument",
    "an empty string",
    "-h",
    "--help",
    "Migrate",
    "an unknown word",
    "import",
    "Export",
    "trailing space",
    "empty string before migrate",
]


class NoEnvironment(Mapping[str, str]):
    """An environment that fails the test on any read of a variable (section 11.4)."""

    def __getitem__(self, name: str) -> str:
        raise AssertionError(f"the entry read the variable {name!r}")

    def __iter__(self) -> Iterator[str]:
        raise AssertionError("the entry listed the environment")

    def __len__(self) -> int:
        raise AssertionError("the entry measured the environment")


@pytest.fixture
def connect_attempts(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Every attempt to connect, recorded instead of made; every connection goes through
    `psycopg.connect`."""
    attempts: list[dict[str, Any]] = []

    def refuse(*args: Any, **kwargs: Any) -> None:
        attempts.append(kwargs)
        raise psycopg.OperationalError("no connection in this test")

    monkeypatch.setattr(psycopg, "connect", refuse)
    return attempts


def database_unchanged(db: RecordDatabase) -> bool:
    """No schema `record`, and the database keeps the ACL it was created with."""
    with db.admin() as admin:
        row = admin.execute(
            "SELECT datacl IS NULL FROM pg_database WHERE datname = current_database()"
        ).fetchone()
    return not schema_exists(db) and row == (True,)


def run_as_main(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> object:
    """Execute the module as `python -m agent_core.record <args>` does, in this process."""
    monkeypatch.setattr(sys, "argv", [PACKAGE, *args])
    # A fresh execution of the module; an earlier import of it would make runpy warn.
    monkeypatch.delitem(sys.modules, PACKAGE + ".__main__", raising=False)
    with pytest.raises(SystemExit) as exited:
        runpy.run_module(PACKAGE, run_name="__main__")
    return exited.value.code


def test_the_usage_line_names_the_four_commands_of_change_3() -> None:
    assert USAGE == "usage: python -m agent_core.record {migrate,export,verify,restore}"


# --- main -----------------------------------------------------------------------------------------


def test_main_runs_migrate(record_db: RecordDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate"], record_db.migrate_env()) == 0
    assert capsys.readouterr() == ("migrated 1\n", "")
    assert main(["migrate"], record_db.migrate_env()) == 0
    assert capsys.readouterr() == ("unchanged 1\n", "")


@pytest.mark.parametrize("args", MIGRATE_ARGUMENTS, ids=MIGRATE_ARGUMENT_IDS)
def test_main_refuses_an_argument_of_migrate_without_connecting(
    record_db: RecordDatabase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    connect_attempts: list[dict[str, Any]],
    args: list[str],
) -> None:
    # Section 14: prints `failed config` and exits 1; no connection, and the database stays
    # unchanged. With a valid configuration, so that only the argument decides.
    assert main(args, record_db.migrate_env()) == 1
    assert capsys.readouterr() == ("failed config\n", "")
    assert connect_attempts == []
    monkeypatch.undo()
    assert database_unchanged(record_db)


@pytest.mark.parametrize("args", UNKNOWN_COMMANDS, ids=UNKNOWN_COMMAND_IDS)
def test_main_prints_the_usage_for_no_or_an_unknown_command(
    capsys: pytest.CaptureFixture[str], connect_attempts: list[dict[str, Any]], args: list[str]
) -> None:
    # Section 14: stderr is exactly the usage line of section 11.4 for change 3 and one `\n`,
    # stdout is empty, the exit code is 2, and no connection; section 11.4: no variable is read.
    assert main(args, NoEnvironment()) == 2
    assert capsys.readouterr() == ("", USAGE_LINE + "\n")
    assert connect_attempts == []


@pytest.mark.parametrize("command", ["export", "verify", "restore"])
def test_main_runs_the_commands_of_change_3(capsys: pytest.CaptureFixture[str], command: str) -> None:
    # Without arguments and variables each known command refuses its configuration with exit 1.
    assert main([command], {}) == 1
    assert capsys.readouterr() == ("refused config\n", "")


def test_main_reads_argv_and_the_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "argv", [PACKAGE, "migrate"])
    for name in RECORD_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    assert main() == 1
    assert capsys.readouterr() == ("failed config\n", "")


def test_main_reads_the_environment_with_given_arguments(
    record_db: RecordDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in record_db.migrate_env().items():
        monkeypatch.setenv(name, value)
    assert main(["migrate"]) == 0
    assert capsys.readouterr() == ("migrated 1\n", "")


# --- the __main__ guard ---------------------------------------------------------------------------


def test_the_guard_exits_with_the_usage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run_as_main(monkeypatch, []) == 2
    assert capsys.readouterr() == ("", USAGE_LINE + "\n")


def test_the_guard_exits_1_without_the_variables(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in RECORD_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    assert run_as_main(monkeypatch, ["migrate"]) == 1
    assert capsys.readouterr() == ("failed config\n", "")


def test_the_guard_exits_1_for_an_argument_of_migrate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in RECORD_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    assert run_as_main(monkeypatch, ["migrate", "-h"]) == 1
    assert capsys.readouterr() == ("failed config\n", "")


def test_the_guard_runs_migrate(
    record_db: RecordDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in record_db.migrate_env().items():
        monkeypatch.setenv(name, value)
    assert run_as_main(monkeypatch, ["migrate"]) == 0
    assert capsys.readouterr() == ("migrated 1\n", "")
    assert schema_exists(record_db)
