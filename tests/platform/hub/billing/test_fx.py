"""U-HUB-billing (fx): the USD/CHF rate for the books - rate format, the CHF equivalent and its rounding over many small
bookings, the rate of a booking around midnight in Europe/Zurich, a missing day, a correction that leaves earlier
bookings alone, the stale warning, the export and its totals, and the operator commands (owner's decisions 18 and
19; 12-billing.md section 10, ADR-062 section 9)."""

from __future__ import annotations

import csv
import io
import json
import logging
import os
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

import pytest

from agent_hub import cli as hub_cli
from agent_hub.billing import bookkeeping, cli, credit, fx, jobs
from tests.platform.hub.billing.support import FLASH, Bill, price_list
from tests.platform.hub.conftest import HubDatabase, hub_environ

TOKENS = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_tokens": 0, "cache_write_tokens": 0}


def test_rates_parse_exactly() -> None:
    assert fx.parse_rate("0.8486") == 84_860_000
    assert fx.parse_rate("0.12345678") == 12_345_678
    assert fx.parse_rate("1") == 100_000_000
    assert fx.parse_rate("100") == 10_000_000_000
    for wrong in ("0", "0.0", "-1", "1e2", "0.123456789", "abc", "101", ".5", "1,2"):
        with pytest.raises(fx.FxInvalid):
            fx.parse_rate(wrong)
    assert fx.Rate(1, date(2026, 11, 24), 84_860_000).decimal == Decimal("0.8486")


def test_the_equivalent_is_rounded_once_half_even_and_sums_are_traceable() -> None:
    assert fx.chf(1_000_000, 84_860_000) == 848_600
    assert fx.chf(1, 50_000_000) == 0  # 0.5 -> 0
    assert fx.chf(3, 50_000_000) == 2  # 1.5 -> 2
    assert fx.chf(-1_500_000, 84_860_000) == -1_272_900
    # many small bookings: every booking is rounded once; the total is the sum of the booked equivalents, and it
    # stays within half a micro-CHF per booking of the exact value
    amounts = [97 + (i % 13) for i in range(10_000)]
    booked = sum(fx.chf(amount, 84_861_234) for amount in amounts)
    exact = sum(Decimal(amount) * Decimal(84_861_234) / Decimal(10**8) for amount in amounts)
    assert abs(Decimal(booked) - exact) <= Decimal(len(amounts)) / 2
    assert booked == int(
        sum(
            (Decimal(a) * Decimal(84_861_234) / Decimal(10**8)).quantize(Decimal(1), rounding=ROUND_HALF_EVEN)
            for a in amounts
        )
    )


def test_the_rate_of_a_booking_follows_the_day_in_zurich(bill: Bill) -> None:
    bill.set_rate("2026-11-20", 85_244_000)
    bill.set_rate("2026-11-21", 85_194_000)
    bill.set_rate("2026-11-24", 84_860_000)
    with bill.hub.pool.connection() as connection:
        # 23:30 UTC on the 20th is 00:30 on the 21st in Zurich
        assert fx.rate_for(connection, datetime(2026, 11, 20, 22, 59, 59, tzinfo=UTC)).day == date(2026, 11, 20)  # type: ignore[union-attr]
        assert fx.rate_for(connection, datetime(2026, 11, 20, 23, 0, 0, tzinfo=UTC)).day == date(2026, 11, 21)  # type: ignore[union-attr]
        # Sunday and Monday without a rate: the newest one before them
        monday = fx.rate_for(connection, datetime(2026, 11, 23, 12, 0, tzinfo=UTC))
        assert monday is not None
        assert (monday.day, monday.chf_per_usd_e8) == (date(2026, 11, 21), 85_194_000)
        assert fx.rate_for(connection, datetime(2026, 11, 19, 12, 0, tzinfo=UTC)) is None
        assert fx.equivalent(connection, datetime(2026, 11, 19, 12, 0, tzinfo=UTC), 5) == (None, None, None, None)
        assert fx.zurich_day(connection, datetime(2026, 11, 20, 23, 30, tzinfo=UTC)) == date(2026, 11, 21)


def test_charges_and_credits_keep_their_equivalent_and_a_correction_changes_nothing_booked(bill: Bill) -> None:
    bill.platform()
    bill.add_prices()
    bill.set_rate("2026-11-21", 84_860_000)
    _task, session = bill.meter.session("platform")
    assert bill.usage(session, model=FLASH, access_origin="platform", tokens=TOKENS) == ("ok", None)
    now = bill.hub.clock()
    with bill.hub.pool.connection() as connection, connection.transaction():
        credit.grant(connection, bill.org, Decimal(10), "welcome", now)
        corrected = fx.set_rate(connection, date(2026, 11, 21), "0.9", source="BAZG", reason="typo", now=now)
    assert corrected.n == 2
    assert bill.usage(session, model=FLASH, access_origin="platform", tokens=TOKENS) == ("ok", None)
    rows = bill.rows(
        "SELECT amount_micro_usd, fx_n, fx_day, chf_per_usd_e8, amount_micro_chf FROM hub.charge ORDER BY at"
    )
    assert rows == [
        (445_200, 1, date(2026, 11, 21), 84_860_000, 377_797),
        (445_200, 2, date(2026, 11, 21), 90_000_000, 400_680),
    ]
    entries = bill.rows(
        "SELECT kind, fx_n, chf_per_usd_e8, amount_micro_chf FROM hub.credit_entry WHERE org_id = %s ORDER BY n",
        bill.org,
    )
    assert entries == [("start", 1, 84_860_000, 4_243_000), ("grant", 1, 84_860_000, 8_486_000)]


def test_setting_rates(bill: Bill) -> None:
    now = bill.hub.clock()
    with bill.hub.pool.connection() as connection, connection.transaction():
        assert not fx.any_rate(connection)
        assert fx.age_days(connection, now) is None
        first = fx.set_rate(connection, date(2026, 11, 20), "0.85244", source="BAZG", reason=None, now=now)
        assert (first.n, first.chf_per_usd_e8) == (1, 85_244_000)
        assert fx.any_rate(connection)
        assert fx.age_days(connection, now) == 1
        with pytest.raises(fx.FxInvalid, match="needs --reason"):
            fx.set_rate(connection, date(2026, 11, 20), "0.9", source="BAZG", reason=None, now=now)
        for source, reason, message in (
            ("two words", None, "the source"),
            ("BAZG", " ", "the reason"),
            ("BAZG", "x" * 501, "the reason"),
        ):
            with pytest.raises(fx.FxInvalid, match=message):
                fx.set_rate(connection, date(2026, 11, 22), "0.9", source=source, reason=reason, now=now)
        fx.set_rate(connection, date(2026, 11, 20), "0.85", source="BAZG", reason="correction", now=now)
        listed = fx.rates(connection)
        assert [(row["n"], row["day"], row["chf_per_usd"], row["reason"]) for row in listed] == [
            (1, "2026-11-20", "0.85244", None),
            (2, "2026-11-20", "0.85", "correction"),
        ]
        assert fx.rates(connection, date(2026, 11, 21), None) == []
        assert fx.rates(connection, None, date(2026, 11, 20)) == listed
    details = bill.rows("SELECT detail FROM hub.audit WHERE action = 'billing.fx_set' ORDER BY seq")
    assert [row[0]["kind"] for row in details] == ["first", "correction"]


def test_the_stale_warning(bill: Bill, caplog: pytest.LogCaptureFixture) -> None:
    now = bill.hub.clock()
    with bill.hub.pool.connection() as connection:
        assert not jobs.warn_stale(connection, now)  # billing inactive
        bill.add_prices()
        with caplog.at_level(logging.WARNING, logger="agent_hub.billing"):
            assert jobs.warn_stale(connection, now)  # no rate at all
            assert not jobs.warn_stale(connection, now + timedelta(minutes=59))  # once an hour
            assert jobs.warn_stale(connection, now + timedelta(hours=1))
        assert "billing.fx stale age_days=None" in caplog.text
        bill.set_rate("2026-11-21", 84_860_000)
        assert not jobs.warn_stale(connection, now + timedelta(hours=3))  # fresh
        assert jobs.warn_stale(connection, now + timedelta(days=5))  # 5 days old


def test_the_export_lists_every_booking_with_totals(bill: Bill) -> None:
    bill.platform()
    bill.add_prices()
    bill.set_rate("2026-11-21", 84_860_000)
    _task, session = bill.meter.session("platform")
    assert bill.usage(session, model=FLASH, access_origin="platform", tokens=TOKENS) == ("ok", None)
    assert bill.runtime(session, 30_000) == ("ok", None)
    bill.settle()
    now = bill.hub.clock()
    with bill.hub.pool.connection() as connection, connection.transaction():
        rows = bookkeeping.bookings(connection, date(2026, 11, 21), date(2026, 11, 21), None)
        assert {row["kind"] for row in rows} == {"runtime", "start", "tokens"}
        assert all(row["chf_per_usd"] == "0.84860000" and row["fx_day"] == "2026-11-21" for row in rows)
        charged = {row["kind"]: row for row in rows}
        assert charged["tokens"]["amount_micro_chf"] == fx.chf(445_200, 84_860_000)
        assert charged["tokens"]["reference"].startswith("charge:")
        assert charged["start"]["reference"] == "credit:1"
        # the CHF total of the period is the sum of the booked equivalents
        total = bill.rows(
            "SELECT (SELECT sum(amount_micro_chf) FROM hub.charge)"
            " + (SELECT sum(amount_micro_chf) FROM hub.credit_entry)"
        )[0][0]
        assert sum(row["amount_micro_chf"] for row in rows) == total
        assert bookkeeping.bookings(connection, date(2026, 11, 22), date(2026, 11, 30), None) == []
        other = bookkeeping.bookings(connection, date(2026, 11, 21), date(2026, 11, 21), bill.scene.stranger.id)
        assert other == []
        text = bookkeeping.as_csv(rows)
        assert text.splitlines()[0] == ",".join(bookkeeping.FIELDS)
        assert len(text.splitlines()) == len(rows) + 1
        bookkeeping.audit_export(connection, (date(2026, 11, 21), date(2026, 11, 21)), None, len(rows), now)
    empty = bookkeeping.as_csv([{**rows[0], "chf_per_usd": None, "fx_day": None, "amount_micro_chf": None}])
    parsed = next(csv.DictReader(io.StringIO(empty)))
    assert (parsed["chf_per_usd"], parsed["fx_day"], parsed["amount_micro_chf"]) == ("", "", "")


@pytest.fixture
def run(
    bill: Bill, migrated_db: HubDatabase, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Any:
    for name in [name for name in os.environ if name.startswith("AGENT_HUB_") and "TEST" not in name]:
        monkeypatch.delenv(name)
    for name, value in hub_environ(migrated_db, AGENT_HUB_BILLING_START_CREDIT_USD="5").items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli, "CLOCK", [bill.hub.clock])

    def invoke(*args: str) -> tuple[int, str, str]:
        code = hub_cli.main(["billing", *args])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return invoke


def test_the_operator_commands(run: Any, bill: Bill, tmp_path: Any) -> None:
    # without a rate: billing cannot start and no credit is booked
    path = tmp_path / "prices.json"
    path.write_text(json.dumps({**price_list(), "valid_from": None}), encoding="utf-8")
    code, _out, err = run("price-list", "add", "--file", str(path))
    assert code == 1
    assert "no USD/CHF rate is set" in err
    code, _out, err = run("credit", "--org", bill.org, "--add", "1", "--reason", "x")
    assert (code, "set a rate first" in err) == (1, True)
    code, out, err = run("fx", "show")
    assert code == 1
    assert json.loads(out) == {"rate": None, "age_days": None, "stale": True}
    assert "older than the allowed days" in err
    # set, correct, show, list
    assert run("fx", "set", "--day", "2026-11-21", "--rate", "0.84860", "--source", "BAZG")[0] == 0
    code, out, err = run("fx", "set", "--day", "2026-11-21", "--rate", "0.9", "--source", "BAZG")
    assert (code, "needs --reason" in err) == (1, True)
    code, out, _err = run("fx", "set", "--day", "2026-11-21", "--rate", "0.9", "--source", "BAZG", "--reason", "typo")
    assert json.loads(out) == {"n": 2, "day": "2026-11-21", "chf_per_usd": "0.9"}
    assert run("fx", "set", "--day", "21.11.2026", "--rate", "0.9", "--source", "BAZG")[0] == 1
    code, out, _err = run("fx", "show")
    assert (code, json.loads(out)["age_days"], json.loads(out)["stale"]) == (0, 0, False)
    code, out, _err = run("fx", "list", "--from", "2026-11-01", "--to", "2026-11-30")
    assert [json.loads(line)["n"] for line in out.splitlines()] == [1, 2]
    assert run("fx", "list", "--from", "x")[0] == 1
    # the export as JSON lines and as CSV, audited
    assert run("credit", "--org", bill.org, "--add", "1", "--reason", "x")[0] == 0
    code, out, _err = run("export", "--from", "2026-11-21", "--to", "2026-11-21")
    lines = [json.loads(line) for line in out.splitlines()]
    assert code == 0
    assert {line["kind"] for line in lines} >= {"grant"}
    code, out, _err = run("export", "--from", "2026-11-21", "--to", "2026-11-21", "--format", "csv", "--org", bill.org)
    assert code == 0
    assert out.splitlines()[0].startswith("at,org_id,kind")
    assert run("export", "--from", "2026", "--to", "2026-11-21")[0] == 1
    assert len(bill.rows("SELECT 1 FROM hub.audit WHERE action = 'billing.export'")) == 2
