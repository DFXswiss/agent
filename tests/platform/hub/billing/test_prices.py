"""U-HUB-billing (prices): the checks of a price list document, the charge formulas and their rounding, the version in
force and the storing of a version (12-billing.md section 2, ADR-059 section 2)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent_hub.billing import prices
from tests.platform.hub.billing.support import FLASH, Bill, price_list

COUNTS = {"input_tokens": 1_000_000, "output_tokens": 100_000, "cache_read_tokens": 500_000, "cache_write_tokens": 0}


def test_a_valid_list_is_parsed() -> None:
    parsed = prices.parse(price_list())
    assert parsed.valid_from == datetime(2026, 11, 21, tzinfo=UTC)
    assert parsed.factors == {"platform": Decimal("1.06"), "operator": Decimal(1)}
    assert parsed.tokens_for(FLASH).output == Decimal("1.2")
    assert parsed.tokens_for("unknown/model") == parsed.default_tokens
    assert parsed.tokens_for(None) == parsed.default_tokens
    assert parsed.hourly("large") == Decimal("0.72")
    assert parsed.hourly("huge") == Decimal("1.44")
    assert parsed.document["note"] == "test"
    # without valid_from, note and models
    minimal = price_list()
    for key in ("valid_from", "note"):
        del minimal[key]
    del minimal["tokens"]["models"]
    parsed = prices.parse(minimal)
    assert parsed.valid_from is None
    assert parsed.models == {}


def _broken(change: Any) -> dict[str, Any]:
    document = price_list()
    change(document)
    return document


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update(currency="CHF"), "currency must be USD"),
        (lambda d: d.update(extra=1), "the price list has the unknown key extra"),
        (lambda d: d.update(note=7), "note must be a text"),
        (lambda d: d.update(note="x" * 501), "note must be a text"),
        (lambda d: d.update(tokens=[]), "tokens must be an object"),
        (lambda d: d["tokens"]["factors"].pop("platform"), "tokens.factors.platform is missing"),
        (lambda d: d["tokens"]["factors"].update(platform=101), "tokens.factors.platform must be a number from 0"),
        (lambda d: d["tokens"]["factors"].update(platform=True), "tokens.factors.platform must be a number"),
        (lambda d: d["tokens"]["factors"].update(platform=None), "tokens.factors.platform must be a number"),
        (lambda d: d["tokens"]["factors"].update(platform="abc"), "tokens.factors.platform must be a number"),
        (lambda d: d["tokens"]["factors"].update(platform="NaN"), "tokens.factors.platform must be a number from"),
        (lambda d: d["tokens"]["factors"].update(platform=-1), "tokens.factors.platform must be a number from"),
        (lambda d: d["tokens"]["models"].update({"bad id": {}}), "tokens.models has an invalid model ID"),
        (lambda d: d["tokens"]["models"][FLASH].pop("output"), f"tokens.models.{FLASH}.output is missing"),
        (lambda d: d["tokens"]["models"][FLASH].update(x=1), f"tokens.models.{FLASH} has the unknown key x"),
        (lambda d: d["tokens"]["models"][FLASH].update(input=1001), "must be a number from 0 to 1000"),
        (lambda d: d["tokens"].update(models={f"m{i}": {} for i in range(201)}), "more than 200 models"),
        (lambda d: d["tokens"].pop("default"), "tokens.default is missing"),
        (lambda d: d["runtime"]["per_hour"].pop("default"), "runtime.per_hour.default is missing"),
        (lambda d: d["runtime"]["per_hour"].update(tiny=1), "runtime.per_hour has the unknown key tiny"),
        (lambda d: d.update(valid_from=5), "valid_from must be a time with a zone"),
        (lambda d: d.update(valid_from="yesterday"), "valid_from must be a time with a zone"),
        (lambda d: d.update(valid_from="2026-11-21T00:00:00"), "valid_from must be a time with a zone"),
    ],
)
def test_a_broken_list_is_refused_naming_the_place(change: Any, message: str) -> None:
    with pytest.raises(prices.PriceListInvalid, match=message):
        prices.parse(_broken(change))


def test_a_list_that_is_no_object_is_refused() -> None:
    with pytest.raises(prices.PriceListInvalid, match="the price list must be an object"):
        prices.parse([])


def test_token_charges_use_the_factor_and_round_half_even_once() -> None:
    parsed = prices.parse(price_list())
    # (1M x 0.3 + 0.1M x 1.2 + 0.5M x 0.006) x 1.06 = (300000 + 120000 + 3000) x 1.06 = 448380
    assert prices.token_charge(parsed, "platform", FLASH, COUNTS) == 448_380
    assert prices.token_charge(parsed, "operator", FLASH, COUNTS) == 423_000
    # 1 input token of flash: 0.3 x 1.06 = 0.318 -> 0; 2 tokens: 0.636 -> 1; half-even: 0.5 -> 0, 1.5 -> 2
    assert prices.token_charge(parsed, "platform", FLASH, {"input_tokens": 1}) == 0
    assert prices.token_charge(parsed, "platform", FLASH, {"input_tokens": 2}) == 1
    half = prices.parse(price_list(tokens={**price_list()["tokens"], "factors": {"platform": 1, "operator": 1}}))
    half_price = {"input": 0.5, "output": 1.5, "cache_read": 0, "cache_write": 0}
    half = prices.parse(
        price_list(tokens={"factors": {"platform": 1, "operator": 1}, "models": {}, "default": half_price})
    )
    assert prices.token_charge(half, "platform", "m", {"input_tokens": 1}) == 0
    assert prices.token_charge(half, "platform", "m", {"output_tokens": 1}) == 2


def test_runtime_charges_per_millisecond() -> None:
    parsed = prices.parse(price_list())
    # one hour of a session VM: 0.36 USD
    assert prices.runtime_charge(parsed, "session", 3_600_000) == 360_000
    # one second: 100 micro-USD; 30 s of xlarge: 1.44 x 30 / 3600 = 0.012
    assert prices.runtime_charge(parsed, "session", 1000) == 100
    assert prices.runtime_charge(parsed, "xlarge", 30_000) == 12_000
    assert prices.runtime_charge(parsed, "unknown", 1000) == 400
    # 5 ms of a session VM = 0.5 micro-USD -> 0 (half-even), 15 ms = 1.5 -> 2
    assert prices.runtime_charge(parsed, "session", 5) == 0
    assert prices.runtime_charge(parsed, "session", 15) == 2


def test_versions_in_force_and_storing(bill: Bill) -> None:
    now = bill.hub.clock()
    with bill.hub.pool.connection() as connection, connection.transaction():
        assert prices.in_force(connection, now) is None
        first = prices.add(connection, prices.parse(price_list()), now, now)
        later = now + timedelta(hours=1)
        second = prices.add(connection, prices.parse(price_list(note="second")), later, now)
        assert (first, second) == (1, 2)
        found = prices.in_force(connection, now)
        assert found is not None
        assert (found.version, found.prices.document["note"]) == (1, "test")
        found = prices.in_force(connection, later)
        assert found is not None
        assert (found.version, found.prices.document["note"]) == (2, "second")
        # the same valid_from: the higher version wins
        third = prices.add(connection, prices.parse(price_list(note="third")), later, now)
        found = prices.in_force(connection, later + timedelta(seconds=1))
        assert found is not None
        assert (third, found.version) == (3, 3)
        with pytest.raises(prices.PriceListInvalid, match="must not lie in the past"):
            prices.add(connection, prices.parse(price_list()), now - timedelta(seconds=1), now)


def test_parse_moment() -> None:
    assert prices.parse_moment("2026-11-24T01:00:00+01:00", "x") == datetime(2026, 11, 24, tzinfo=UTC)
    with pytest.raises(prices.PriceListInvalid, match="x must be a time with a zone"):
        prices.parse_moment("2" * 41, "x")


def test_the_first_price_list_of_the_repository_is_valid() -> None:
    path = Path(__file__).resolve().parents[4] / "docs/platform/billing/price-list-2026-11-24.json"
    parsed = prices.parse(json.loads(path.read_text(encoding="utf-8")))
    assert parsed.factors == {"platform": Decimal("1.06"), "operator": Decimal("1.0")}
    assert prices.runtime_charge(parsed, "session", 3_600_000) == 360_000
