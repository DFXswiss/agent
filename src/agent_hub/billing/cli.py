"""Operator CLI of the billing area: `agent-hub billing …` (12-billing.md section 8, ADR-059 sections 2 and 5).

Run on the hub host with the hub's environment (like `agent-hub audit verify`); it talks to the hub database
directly. Every command prints one JSON line per result on stdout and exits 0 when done, 1 when refused (the message
names the reason on stderr), 2 on a usage or configuration error and 3 when the database is not reachable.

- `price-list add --file F [--valid-from ISO] [--confirm]`: checks the document (`agent_hub.billing.prices.parse`),
  refuses a list that misses a model the hub's catalog offers through DFX access (platform and subscription
  allow-lists), stores it as the next version valid from `--valid-from` (default: the document's `valid_from`, else
  now; never in the past) and names the organizations whose credit would be used up when it comes into force; while
  there are any it refuses without `--confirm` (a release with start credit 0 cannot stop every organization by
  accident).
- `price-list show`: every version, newest first, and the one in force.
- `credit (--org ID | --all-orgs) (--add USD | --set USD) --reason TEXT`: an operator credit (settles the pending
  charges first; history entry and audit row `credit.changed`). `--add` may be negative to correct.
- `show [--org ID]`: balance, exhausted and account columns per organization.
- `fx set --day D --rate R --source S [--reason TEXT]`: the USD/CHF rate (CHF per 1 USD) of a day for the books; a
  further rate of the same day is a correction and needs `--reason` (bookings made keep their rate; audited
  `billing.fx_set`). `fx show`: the rate a booking made now uses and its age (exit 1 when missing or older than
  `fx.STALE_DAYS` days - for a monitoring job). `fx list [--from D] [--to D]`.
- `export --from D --to D [--org ID] [--format json|csv]`: every booking of the period (days in Europe/Zurich) with
  USD, rate, rate day and CHF equivalent (`agent_hub.billing.bookkeeping`; audited `billing.export`).
- `price-list add` refuses while no rate is set, `credit` as well (every booking keeps its CHF equivalent).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg_pool import PoolTimeout

from agent_hub.billing import binding, bookkeeping, credit, fx, prices
from agent_hub.config import ConfigError, HubConfig, load_config
from agent_hub.crypto.encoding import format_time
from agent_hub.db.pool import open_pool

EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
CONNECT_TIMEOUT_SECONDS = 30.0
MAX_FILE_BYTES = 256 * 1024
_USD_RE = re.compile(r"^-?(?:0|[1-9][0-9]{0,8})(?:\.[0-9]{1,6})?$")
CLOCK: list[Callable[[], datetime]] = [lambda: datetime.now(UTC)]
"""The clock of the commands (tests replace it)."""


class Refused(Exception):
    """A command refused for a reason the operator can fix."""


class Stale(Exception):
    """`fx show`: the rate is missing or stale; carries the answer to print."""

    def __init__(self, found: dict[str, Any]) -> None:
        super().__init__("stale")
        self.found = found


def _print(value: Any) -> None:
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))


def usd(text: str) -> Decimal:
    """A USD amount with at most 6 decimals (argparse type)."""
    if not _USD_RE.fullmatch(text):
        raise argparse.ArgumentTypeError("an amount in USD with at most 6 decimals")
    return Decimal(text)


def org_id(text: str) -> UUID:
    try:
        return UUID(text)
    except ValueError:
        raise argparse.ArgumentTypeError("an organization ID (UUID)") from None


def missing_models(config: HubConfig, prices_list: prices.PriceList) -> list[str]:
    """The models the catalog offers through DFX access without an entry in the price list."""
    offered = set(config.model_catalog.allowed.get("platform", ())) | set(
        config.model_catalog.allowed.get("subscription", ())
    )
    return sorted(offered - set(prices_list.models))


def _read_document(path: str) -> Any:
    file = Path(path)
    if not file.is_file() or file.stat().st_size > MAX_FILE_BYTES:
        raise Refused(f"the price list file must be a readable file of at most {MAX_FILE_BYTES} bytes")
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise Refused("the price list file is not JSON") from None


def price_list_add(connection: Any, config: HubConfig, args: argparse.Namespace, now: datetime) -> dict[str, Any]:
    document = _read_document(args.file)
    try:
        parsed = prices.parse(document)
        start = prices.parse_moment(args.valid_from, "--valid-from") if args.valid_from else parsed.valid_from or now
    except prices.PriceListInvalid as error:
        raise Refused(str(error)) from None
    if not fx.any_rate(connection):
        raise Refused("no USD/CHF rate is set: set one with `agent-hub billing fx set` before billing starts")
    missing = missing_models(config, parsed)
    if missing:
        raise Refused(f"the price list has no entry for the DFX model {missing[0]}")
    orgs = [str(row[0]) for row in connection.execute("SELECT id FROM hub.org ORDER BY id").fetchall()]
    exhausted = sorted(org for org, value in credit.balances(connection, orgs).items() if value <= 0)
    if exhausted and not args.confirm:
        raise Refused(
            f"{len(exhausted)} organizations would have no credit when the list comes into force"
            f" ({', '.join(exhausted[:5])}); set a start credit or credit them first, or repeat with --confirm"
        )
    stored = prices.parse({**document, "valid_from": format_time(start)})
    try:
        version = prices.add(connection, stored, start, now)
    except prices.PriceListInvalid as error:
        raise Refused(str(error)) from None
    return {"version": version, "valid_from": format_time(start), "exhausted_orgs": exhausted}


def price_list_show(connection: Any, now: datetime) -> dict[str, Any]:
    current = prices.in_force(connection, now)
    rows = connection.execute(
        "SELECT version, valid_from, created_at FROM hub.price_list ORDER BY version DESC"
    ).fetchall()
    return {
        "in_force": None if current is None else current.version,
        "versions": [
            {"version": version, "valid_from": format_time(start), "created_at": format_time(created)}
            for version, start, created in rows
        ],
        "body": None if current is None else current.prices.document,
    }


def credit_change(connection: Any, args: argparse.Namespace, now: datetime) -> list[dict[str, Any]]:
    if args.all_orgs:
        orgs = [row[0] for row in connection.execute("SELECT id FROM hub.org ORDER BY id").fetchall()]
    else:
        if connection.execute("SELECT 1 FROM hub.org WHERE id = %s", (args.org,)).fetchone() is None:
            raise Refused("unknown organization")
        orgs = [args.org]
    if not fx.any_rate(connection):
        raise Refused("no USD/CHF rate is set: every credit keeps its CHF equivalent, set a rate first")
    results = []
    for org in orgs:
        try:
            if args.add is not None:
                account = credit.grant(connection, org, args.add, args.reason, now)
            else:
                account = credit.set_balance(connection, org, args.set, args.reason, now)
        except credit.CreditInvalid as error:
            raise Refused(str(error)) from None
        results.append({"org_id": str(org), "balance_micro_usd": account.balance_micro_usd})
    return results


def show(connection: Any, args: argparse.Namespace, now: datetime) -> list[dict[str, Any]]:
    where, params = ("WHERE id = %s", (args.org,)) if args.org else ("", ())
    orgs = [str(row[0]) for row in connection.execute(f"SELECT id FROM hub.org {where} ORDER BY id", params)]
    balances = credit.balances(connection, orgs)
    states = credit.exhausted_orgs(connection, orgs, now)
    accounts = {
        str(org): (int(current), int(booked))
        for org, current, booked in connection.execute(
            "SELECT org_id, balance_micro_usd, booked_micro_usd FROM hub.credit_account WHERE org_id = ANY(%s::uuid[])",
            (orgs,),
        ).fetchall()
    }
    return [
        {
            "org_id": org,
            "balance_micro_usd": balances[org],
            "exhausted": states[org],
            "account": None if org not in accounts else {"balance": accounts[org][0], "booked": accounts[org][1]},
        }
        for org in orgs
    ]


def fx_set(connection: Any, args: argparse.Namespace, now: datetime) -> dict[str, Any]:
    try:
        day = date.fromisoformat(args.day)
        rate = fx.set_rate(connection, day, args.rate, source=args.source, reason=args.reason, now=now)
    except ValueError as error:
        raise Refused(str(error) if isinstance(error, fx.FxInvalid) else "the day must be YYYY-MM-DD") from None
    return {"n": rate.n, "day": rate.day.isoformat(), "chf_per_usd": str(rate.decimal)}


def fx_show(connection: Any, now: datetime) -> dict[str, Any]:
    """The rate a booking made now uses, its age in days and whether it is stale (exit 1 when stale or missing)."""
    rate = fx.rate_for(connection, now)
    age = fx.age_days(connection, now)
    stale = age is None or age > fx.STALE_DAYS
    found = {
        "rate": None if rate is None else {"n": rate.n, "day": rate.day.isoformat(), "chf_per_usd": str(rate.decimal)},
        "age_days": age,
        "stale": stale,
    }
    if stale:
        raise Stale(found)
    return found


def fx_list(connection: Any, args: argparse.Namespace) -> list[dict[str, Any]]:
    try:
        first = None if args.first is None else date.fromisoformat(args.first)
        last = None if args.last is None else date.fromisoformat(args.last)
    except ValueError:
        raise Refused("the days must be YYYY-MM-DD") from None
    return fx.rates(connection, first, last)


def export(connection: Any, args: argparse.Namespace, now: datetime) -> list[dict[str, Any]]:
    try:
        first, last = date.fromisoformat(args.first), date.fromisoformat(args.last)
    except ValueError:
        raise Refused("the days must be YYYY-MM-DD") from None
    rows = bookkeeping.bookings(connection, first, last, args.org)
    bookkeeping.audit_export(connection, (first, last), args.org, len(rows), now)
    return rows


def _run(args: argparse.Namespace, work: Callable[[Any, HubConfig, datetime], Any], *, csv_out: bool = False) -> int:
    try:
        config = load_config(os.environ)
    except ConfigError as error:
        print(f"agent-hub: {error}", file=sys.stderr)
        return EXIT_USAGE
    binding.bind(config)
    try:
        pool = open_pool(
            config.database, min_size=1, max_size=1, timeout=CONNECT_TIMEOUT_SECONDS, name="agent-hub-billing"
        )
    except PoolTimeout:
        print("agent-hub: the hub database is not reachable", file=sys.stderr)
        return EXIT_UNAVAILABLE
    try:
        with pool.connection() as connection, connection.transaction():
            result = work(connection, config, CLOCK[0]())
    except Refused as error:
        print(f"agent-hub: {error}", file=sys.stderr)
        return EXIT_REFUSED
    except Stale as stale:
        _print(stale.found)
        print("agent-hub: the USD/CHF rate is missing or older than the allowed days", file=sys.stderr)
        return EXIT_REFUSED
    finally:
        pool.close()
    if csv_out:
        sys.stdout.write(bookkeeping.as_csv(result))
        return 0
    for line in result if isinstance(result, list) else [result]:
        _print(line)
    return 0


def _export(args: argparse.Namespace) -> int:
    return _run(args, lambda c, _cfg, now: export(c, args, now), csv_out=args.format == "csv")


def register(subparsers: argparse._SubParsersAction[Any]) -> None:
    """Add `billing price-list add|show`, `billing credit`, `billing show`."""
    parser = subparsers.add_parser("billing", help="prices and prepaid credit")
    commands = parser.add_subparsers(dest="billing_command", metavar="command", required=True)

    price_list = commands.add_parser("price-list", help="price list versions")
    lists = price_list.add_subparsers(dest="price_list_command", metavar="command", required=True)
    add = lists.add_parser("add", help="add a price list version")
    add.add_argument("--file", required=True)
    add.add_argument("--valid-from", dest="valid_from")
    add.add_argument("--confirm", action="store_true")
    add.set_defaults(handler=lambda args: _run(args, lambda c, cfg, now: price_list_add(c, cfg, args, now)))
    listed = lists.add_parser("show", help="show the versions and the one in force")
    listed.set_defaults(handler=lambda args: _run(args, lambda c, _cfg, now: price_list_show(c, now)))

    change = commands.add_parser("credit", help="add to or set the credit of organizations")
    target = change.add_mutually_exclusive_group(required=True)
    target.add_argument("--org", type=org_id)
    target.add_argument("--all-orgs", dest="all_orgs", action="store_true")
    amount = change.add_mutually_exclusive_group(required=True)
    amount.add_argument("--add", type=usd)
    amount.add_argument("--set", type=usd)
    change.add_argument("--reason", required=True)
    change.set_defaults(handler=lambda args: _run(args, lambda c, _cfg, now: credit_change(c, args, now)))

    rates = commands.add_parser("fx", help="the USD/CHF rate per day for the books")
    fx_commands = rates.add_subparsers(dest="fx_command", metavar="command", required=True)
    setter = fx_commands.add_parser("set", help="set the rate of a day (again: a correction with --reason)")
    setter.add_argument("--day", required=True)
    setter.add_argument("--rate", required=True, help="CHF per 1 USD, at most 8 decimals")
    setter.add_argument("--source", required=True)
    setter.add_argument("--reason")
    setter.set_defaults(handler=lambda args: _run(args, lambda c, _cfg, now: fx_set(c, args, now)))
    shower = fx_commands.add_parser("show", help="the rate in use now and its age (exit 1 when stale)")
    shower.set_defaults(handler=lambda args: _run(args, lambda c, _cfg, now: fx_show(c, now)))
    lister = fx_commands.add_parser("list", help="the rates of a period, corrections included")
    lister.add_argument("--from", dest="first")
    lister.add_argument("--to", dest="last")
    lister.set_defaults(handler=lambda args: _run(args, lambda c, _cfg, _now: fx_list(c, args)))

    exporter = commands.add_parser("export", help="the bookings of a period for the books (JSON lines or CSV)")
    exporter.add_argument("--from", dest="first", required=True)
    exporter.add_argument("--to", dest="last", required=True)
    exporter.add_argument("--org", type=org_id)
    exporter.add_argument("--format", choices=("json", "csv"), default="json")
    exporter.set_defaults(handler=_export)

    view = commands.add_parser("show", help="balances of the organizations")
    view.add_argument("--org", type=org_id)
    view.set_defaults(handler=lambda args: _run(args, lambda c, _cfg, now: show(c, args, now)))
