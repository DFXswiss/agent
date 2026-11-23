"""U-HUB-billing (operator CLI): `agent-hub billing price-list add|show`, `billing credit`, `billing show` against the
hub database (12-billing.md section 8, ADR-059 sections 2 and 5)."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from agent_hub import cli as hub_cli
from agent_hub.billing import cli
from agent_hub.crypto.encoding import format_time
from tests.platform.hub.billing.support import Bill, price_list
from tests.platform.hub.conftest import HubDatabase, hub_environ
from tests.platform.hub.orgs.support import new_id


@pytest.fixture
def with_rate() -> bool:
    """A USD/CHF rate exists before the test (a test parametrizes it to start without)."""
    return True


@pytest.fixture
def run(
    bill: Bill,
    migrated_db: HubDatabase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    with_rate: bool,
) -> Any:
    if with_rate:
        bill.set_rate("2026-11-20", 84860000)
    for name in [name for name in os.environ if name.startswith("AGENT_HUB_") and "TEST" not in name]:
        monkeypatch.delenv(name)
    for name, value in hub_environ(migrated_db, AGENT_HUB_BILLING_START_CREDIT_USD="5").items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli, "CLOCK", [bill.hub.clock])

    def invoke(*args: str) -> tuple[int, list[Any], str]:
        code = hub_cli.main(["billing", *args])
        captured = capsys.readouterr()
        return code, [json.loads(line) for line in captured.out.splitlines()], captured.err

    return invoke


def _file(tmp_path: Path, document: Any, name: str = "prices.json") -> str:
    path = tmp_path / name
    path.write_text(json.dumps(document) if not isinstance(document, str) else document, encoding="utf-8")
    return str(path)


def test_price_lists_are_added_and_shown(run: Any, bill: Bill, tmp_path: Path) -> None:
    now = bill.hub.clock()
    document = price_list(valid_from=format_time(now))
    code, out, err = run("price-list", "add", "--file", _file(tmp_path, document))
    assert (code, err) == (0, "")
    assert out == [{"version": 1, "valid_from": format_time(now), "exhausted_orgs": []}]
    later = format_time(now + timedelta(days=1))
    code, out, _err = run("price-list", "add", "--file", _file(tmp_path, price_list()), "--valid-from", later)
    assert (code, out[0]["version"], out[0]["valid_from"]) == (0, 2, later)
    code, out, _err = run("price-list", "show")
    assert code == 0
    assert out[0]["in_force"] == 1
    assert [entry["version"] for entry in out[0]["versions"]] == [2, 1]
    assert out[0]["body"]["valid_from"] == format_time(now)


def test_price_list_refusals(run: Any, bill: Bill, tmp_path: Path) -> None:
    now = bill.hub.clock()
    assert run("price-list", "show")[1] == [{"in_force": None, "versions": [], "body": None}]
    cases = [
        (_file(tmp_path, "not json", "text.json"), "the price list file is not JSON"),
        (str(tmp_path / "missing.json"), "must be a readable file"),
        (_file(tmp_path, price_list(currency="EUR"), "eur.json"), "currency must be USD"),
        (_file(tmp_path, price_list(valid_from=format_time(now - timedelta(days=1))), "past.json"), "must not lie"),
    ]
    incomplete = price_list(valid_from=format_time(now))
    del incomplete["tokens"]["models"]["claude-opus-5-5"]
    cases.append((_file(tmp_path, incomplete, "incomplete.json"), "no entry for the DFX model claude-opus-5-5"))
    for path, message in cases:
        code, out, err = run("price-list", "add", "--file", path)
        assert (code, out) == (1, []), message
        assert message in err
    code, _out, err = run("price-list", "add", "--file", _file(tmp_path, price_list()), "--valid-from", "soon")
    assert code == 1
    assert "--valid-from must be a time with a zone" in err


@pytest.mark.parametrize("start_credit", ["0"])
def test_a_list_that_would_exhaust_orgs_needs_confirm(
    run: Any, bill: Bill, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_HUB_BILLING_START_CREDIT_USD", "0")
    path = _file(tmp_path, price_list(valid_from=format_time(bill.hub.clock())))
    code, out, err = run("price-list", "add", "--file", path)
    assert (code, out) == (1, [])
    assert "organizations would have no credit" in err
    assert bill.org in err
    code, out, _err = run("price-list", "add", "--file", path, "--confirm")
    assert code == 0
    assert bill.org in out[0]["exhausted_orgs"]


def test_credit_and_show(run: Any, bill: Bill) -> None:
    code, out, _err = run("credit", "--org", bill.org, "--add", "12.5", "--reason", "welcome")
    assert (code, out) == (0, [{"org_id": bill.org, "balance_micro_usd": 17_500_000}])
    code, out, _err = run("credit", "--org", bill.org, "--set", "3", "--reason", "reset")
    assert out == [{"org_id": bill.org, "balance_micro_usd": 3_000_000}]
    code, out, _err = run("credit", "--all-orgs", "--add", "-1", "--reason", "all")
    assert {entry["org_id"]: entry["balance_micro_usd"] for entry in out}[bill.org] == 2_000_000
    code, out, _err = run("show", "--org", bill.org)
    assert out == [
        {
            "org_id": bill.org,
            "balance_micro_usd": 2_000_000,
            "exhausted": False,
            "account": {"balance": 2_000_000, "booked": 2_000_000},
        }
    ]
    code, out, _err = run("show")
    assert code == 0
    assert bill.org in {entry["org_id"] for entry in out}


def test_credit_refusals_and_usage_errors(run: Any, bill: Bill, capsys: pytest.CaptureFixture[str]) -> None:
    code, _out, err = run("credit", "--org", new_id(), "--add", "1", "--reason", "x")
    assert (code, err) == (1, "agent-hub: unknown organization\n")
    code, _out, err = run("credit", "--org", bill.org, "--add", "1", "--reason", " ")
    assert code == 1
    assert "the reason must be a text" in err
    for args in (["--org", "nope"], ["--org", bill.org, "--add", "1.1234567"]):
        with pytest.raises(SystemExit) as caught:
            hub_cli.main(["billing", "credit", *args, "--reason", "x"])
        assert caught.value.code == 2
    capsys.readouterr()


def test_configuration_and_database_errors(
    run: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AGENT_HUB_BILLING_START_CREDIT_USD", "-1")
    code, _out, err = run("show")
    assert code == cli.EXIT_USAGE
    assert "AGENT_HUB_BILLING_START_CREDIT_USD" in err
    monkeypatch.setenv("AGENT_HUB_BILLING_START_CREDIT_USD", "1")
    monkeypatch.setenv("AGENT_HUB_DATABASE", "postgresql://writer:" + "0" * 64 + "@127.0.0.1:9/hub?sslmode=disable")
    monkeypatch.setattr(cli, "CONNECT_TIMEOUT_SECONDS", 0.5)
    code, _out, err = run("show")
    assert (code, err) == (cli.EXIT_UNAVAILABLE, "agent-hub: the hub database is not reachable\n")
