"""The entry of `python -m agent_core.record` in process (src/agent_core/record/__main__.py).

`main` is called directly, and the module's `__main__` guard is executed with `runpy`, so that
every line and branch of the entry runs in the test process. The same command as a separate
process is tested in test_record_migrate.py.

Two behaviours here are decisions of the implementation where RECORD-PROTOCOL.md is silent:
`migrate` with any argument prints `failed config` and exits 1 without connecting, and no command
or an unknown command prints the usage line to stderr and exits 2.
"""

from __future__ import annotations

import runpy
import sys

import pytest

from agent_core.record.__main__ import USAGE, main
from tests.conftest import RecordDatabase, schema_exists

PACKAGE = "agent_core.record"
RECORD_VARIABLES = ("AGENT_CORE_RECORD_DATABASE", "AGENT_CORE_RECORD_OWNER_DATABASE")


def run_as_main(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> object:
    """Execute the module as `python -m agent_core.record <args>` does, in this process."""
    monkeypatch.setattr(sys, "argv", [PACKAGE, *args])
    # A fresh execution of the module; an earlier import of it would make runpy warn.
    monkeypatch.delitem(sys.modules, PACKAGE + ".__main__", raising=False)
    with pytest.raises(SystemExit) as exited:
        runpy.run_module(PACKAGE, run_name="__main__")
    return exited.value.code


def test_the_usage_line() -> None:
    assert USAGE == "usage: python -m agent_core.record migrate"


# --- main -----------------------------------------------------------------------------------------


def test_main_runs_migrate(record_db: RecordDatabase, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate"], record_db.migrate_env()) == 0
    assert capsys.readouterr() == ("migrated 1\n", "")
    assert main(["migrate"], record_db.migrate_env()) == 0
    assert capsys.readouterr() == ("unchanged 1\n", "")


@pytest.mark.parametrize(
    "args",
    [["migrate", "--force"], ["migrate", "migrate"], ["migrate", ""], ["migrate", "a", "b"]],
    ids=["an option", "migrate twice", "an empty argument", "two arguments"],
)
def test_main_refuses_an_argument_of_migrate_without_connecting(
    record_db: RecordDatabase, capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    # With a valid configuration, so that only the argument decides.
    assert main(args, record_db.migrate_env()) == 1
    assert capsys.readouterr() == ("failed config\n", "")
    assert not schema_exists(record_db)


@pytest.mark.parametrize(
    "args",
    [[], ["export"], ["verify"], ["restore"], ["Migrate"], ["migrate "], ["--help"], ["", "migrate"]],
    ids=["none", "export", "verify", "restore", "uppercase", "trailing space", "an option", "empty first"],
)
def test_main_prints_the_usage_for_no_or_an_unknown_command(
    capsys: pytest.CaptureFixture[str], args: list[str]
) -> None:
    assert main(args, {}) == 2
    assert capsys.readouterr() == ("", USAGE + "\n")


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
    assert capsys.readouterr() == ("", USAGE + "\n")


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
    assert run_as_main(monkeypatch, ["migrate", "--force"]) == 1
    assert capsys.readouterr() == ("failed config\n", "")


def test_the_guard_runs_migrate(
    record_db: RecordDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name, value in record_db.migrate_env().items():
        monkeypatch.setenv(name, value)
    assert run_as_main(monkeypatch, ["migrate"]) == 0
    assert capsys.readouterr() == ("migrated 1\n", "")
    assert schema_exists(record_db)
