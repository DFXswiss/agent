"""The price list: parsing, versions and the price of one booking (12-billing.md section 2, ADR-059 section 2).

A price list is configuration (a JSON document the operator adds as a new version of `hub.price_list`), never code:

- `parse(document)` checks a document and returns a `PriceList`; anything unexpected - an unknown key, a negative or
  oversized price, a currency other than USD, a missing `default` - is `PriceListInvalid` naming the place, so a typo
  never prices something at 0.
- `in_force(connection, at)` is the version with the greatest `valid_from` at or before `at` (ties: the highest
  version), None before the first version: then nothing is charged and nothing is blocked.
- `token_charge` and `runtime_charge` price one booking in micro-USD, rounded once, half-even, with exact decimals:
  token prices are USD per million tokens (= micro-USD per token) times the factor of the DFX access kind; runtime
  prices are USD per VM hour (micro-USD = runtime_ms x per_hour / 3.6).
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, cast

import psycopg
from psycopg.types.json import Jsonb

Connection = psycopg.Connection[Any]
CURRENCY = "USD"
ACCESS_KINDS = ("platform", "operator")
TOKEN_FIELDS = {
    "input": "input_tokens",
    "output": "output_tokens",
    "cache_read": "cache_read_tokens",
    "cache_write": "cache_write_tokens",
}
SIZES = ("session", "standard", "large", "xlarge")
DEFAULT = "default"
MAX_PRICE = Decimal(1000)
MAX_FACTOR = Decimal(100)
MAX_NOTE = 500
MAX_MODELS = 200
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
_TOP = frozenset({"currency", "valid_from", "tokens", "runtime", "note"})
_HOUR_MS = Decimal(3600) / Decimal(1000)


class PriceListInvalid(ValueError):
    """The document is not a valid price list; the message names the place (never a secret: prices are public)."""


@dataclass(frozen=True)
class TokenPrice:
    """Purchase prices in USD per million tokens."""

    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write: Decimal


@dataclass(frozen=True)
class PriceList:
    """One checked price list (module docstring)."""

    valid_from: datetime | None
    factors: Mapping[str, Decimal]
    models: Mapping[str, TokenPrice]
    default_tokens: TokenPrice
    per_hour: Mapping[str, Decimal]
    document: Mapping[str, Any]

    def tokens_for(self, model: str | None) -> TokenPrice:
        """The purchase prices of the model; the `default` entry for a model without its own."""
        return self.models.get(model or "", self.default_tokens)

    def hourly(self, size: str) -> Decimal:
        """The price of one VM hour of the size; the `default` entry for an unknown size."""
        return self.per_hour.get(size, self.per_hour[DEFAULT])


@dataclass(frozen=True)
class Version:
    """A stored price list version."""

    version: int
    valid_from: datetime
    prices: PriceList


def _object(value: object, where: str, allowed: frozenset[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise PriceListInvalid(f"{where} must be an object")
    if allowed is not None:
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise PriceListInvalid(f"{where} has the unknown key {unknown[0]}")
    return cast(Mapping[str, Any], value)


def _amount(value: object, where: str, maximum: Decimal) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise PriceListInvalid(f"{where} must be a number")
    try:
        amount = Decimal(str(value))
    except ArithmeticError:
        raise PriceListInvalid(f"{where} must be a number") from None
    if not amount.is_finite() or amount < 0 or amount > maximum:
        raise PriceListInvalid(f"{where} must be a number from 0 to {maximum}")
    return amount


def _token_price(value: object, where: str) -> TokenPrice:
    found = _object(value, where, frozenset(TOKEN_FIELDS))
    missing = [name for name in TOKEN_FIELDS if name not in found]
    if missing:
        raise PriceListInvalid(f"{where}.{missing[0]} is missing")
    return TokenPrice(**{name: _amount(found[name], f"{where}.{name}", MAX_PRICE) for name in TOKEN_FIELDS})


def _valid_from(value: object) -> datetime | None:
    if value is None:
        return None
    return parse_moment(value, "valid_from")


def parse_moment(value: object, where: str) -> datetime:
    """An ISO 8601 time with a zone (`2026-11-24T00:00:00Z`)."""
    if not isinstance(value, str) or len(value) > 40:
        raise PriceListInvalid(f"{where} must be a time with a zone")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise PriceListInvalid(f"{where} must be a time with a zone") from None
    if moment.tzinfo is None:
        raise PriceListInvalid(f"{where} must be a time with a zone")
    return moment.astimezone(UTC)


def parse(document: object) -> PriceList:
    """Check a price list document (module docstring)."""
    top = _object(document, "the price list", _TOP)
    if top.get("currency") != CURRENCY:
        raise PriceListInvalid(f"currency must be {CURRENCY}")
    note = top.get("note")
    if note is not None and (not isinstance(note, str) or len(note) > MAX_NOTE):
        raise PriceListInvalid(f"note must be a text of at most {MAX_NOTE} characters")
    tokens = _object(top.get("tokens"), "tokens", frozenset({"factors", "models", DEFAULT}))
    factors_in = _object(tokens.get("factors"), "tokens.factors", frozenset(ACCESS_KINDS))
    factors = {}
    for kind in ACCESS_KINDS:
        if kind not in factors_in:
            raise PriceListInvalid(f"tokens.factors.{kind} is missing")
        factors[kind] = _amount(factors_in[kind], f"tokens.factors.{kind}", MAX_FACTOR)
    models_in = _object(tokens.get("models", {}), "tokens.models")
    if len(models_in) > MAX_MODELS:
        raise PriceListInvalid(f"tokens.models has more than {MAX_MODELS} models")
    models = {}
    for model, price in sorted(models_in.items()):
        if not _MODEL_RE.fullmatch(model):
            raise PriceListInvalid("tokens.models has an invalid model ID")
        models[model] = _token_price(price, f"tokens.models.{model}")
    if DEFAULT not in tokens:
        raise PriceListInvalid("tokens.default is missing")
    default_tokens = _token_price(tokens[DEFAULT], "tokens.default")
    runtime = _object(top.get("runtime"), "runtime", frozenset({"per_hour"}))
    hours_in = _object(runtime.get("per_hour"), "runtime.per_hour", frozenset((*SIZES, DEFAULT)))
    if DEFAULT not in hours_in:
        raise PriceListInvalid("runtime.per_hour.default is missing")
    per_hour = {size: _amount(price, f"runtime.per_hour.{size}", MAX_PRICE) for size, price in hours_in.items()}
    return PriceList(
        valid_from=_valid_from(top.get("valid_from")),
        factors=factors,
        models=models,
        default_tokens=default_tokens,
        per_hour=per_hour,
        document=json.loads(json.dumps(top)),
    )


def _micro(value: Decimal) -> int:
    return int(value.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def token_charge(prices: PriceList, access: str, model: str | None, counts: Mapping[str, int]) -> int:
    """micro-USD of one request with DFX access `access` (`platform` or `operator`)."""
    price = prices.tokens_for(model)
    total = sum(
        (Decimal(counts.get(count, 0)) * getattr(price, name) for name, count in TOKEN_FIELDS.items()),
        Decimal(0),
    )
    return _micro(total * prices.factors[access])


def runtime_charge(prices: PriceList, size: str, runtime_ms: int) -> int:
    """micro-USD of `runtime_ms` of a VM of the size."""
    return _micro(Decimal(runtime_ms) * prices.hourly(size) / _HOUR_MS)


def in_force(connection: Connection, at: datetime) -> Version | None:
    """The version in force at `at` (module docstring)."""
    row = connection.execute(
        "SELECT version, valid_from, document FROM hub.price_list WHERE valid_from <= %s"
        " ORDER BY valid_from DESC, version DESC LIMIT 1",
        (at,),
    ).fetchone()
    if row is None:
        return None
    return Version(version=int(row[0]), valid_from=row[1], prices=parse(row[2]))


def add(connection: Connection, prices: PriceList, valid_from: datetime, now: datetime) -> int:
    """Store the price list as the next version valid from `valid_from` (not before `now`); its version number."""
    if valid_from < now:
        raise PriceListInvalid("valid_from must not lie in the past")
    connection.execute("SELECT pg_advisory_xact_lock(hashtext('hub.price_list'))")
    row = cast(tuple[int], connection.execute("SELECT COALESCE(max(version), 0) + 1 FROM hub.price_list").fetchone())
    version = int(row[0])
    connection.execute(
        "INSERT INTO hub.price_list (version, valid_from, document, created_at) VALUES (%s, %s, %s, %s)",
        (version, valid_from, Jsonb(dict(prices.document)), now),
    )
    return version
