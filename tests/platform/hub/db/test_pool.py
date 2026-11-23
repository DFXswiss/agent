"""The writer pool and the transaction helper (agent_hub.db.pool)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest
from psycopg_pool import PoolTimeout

from agent_core.record.connection import parse_connection_string
from agent_hub.db.pool import open_pool, transaction
from tests.platform.hub.conftest import HubDatabase

PRINCIPAL = "0190f0f0-0000-7000-8000-000000000001"


def test_pooled_connections_act_as_the_writer_in_utc(migrated_db: HubDatabase) -> None:
    pool = open_pool(parse_connection_string(migrated_db.writer_url), max_size=2)
    try:
        with transaction(pool) as connection:
            row = connection.execute(
                "SELECT current_user, session_user, current_setting('search_path'), current_setting('TimeZone')"
            ).fetchone()
            # Unqualified names resolve in schema hub.
            assert connection.execute("SELECT count(*) FROM principal").fetchone() == (0,)
        assert row == (migrated_db.writer_role, migrated_db.writer_role, "pg_catalog, hub, pg_temp", "UTC")
    finally:
        pool.close()


def test_transaction_commits_or_rolls_back(migrated_db: HubDatabase, owner: psycopg.Connection[Any]) -> None:
    pool = open_pool(parse_connection_string(migrated_db.writer_url))
    try:

        def insert_then_fail() -> None:
            with transaction(pool) as connection:
                connection.execute(
                    "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', now())", (PRINCIPAL,)
                )
                raise RuntimeError("abort")

        with pytest.raises(RuntimeError):
            insert_then_fail()
        assert owner.execute("SELECT count(*) FROM hub.principal").fetchone() == (0,)
        with transaction(pool) as connection:
            connection.execute(
                "INSERT INTO hub.principal (id, kind, created_at) VALUES (%s, 'user', now())", (PRINCIPAL,)
            )
        assert owner.execute("SELECT count(*) FROM hub.principal").fetchone() == (1,)
    finally:
        pool.close()


def test_the_writer_cannot_change_the_schema(migrated_db: HubDatabase) -> None:
    pool = open_pool(parse_connection_string(migrated_db.writer_url))
    try:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), transaction(pool) as connection:
            connection.execute("CREATE TABLE hub.extra (id integer)")
        with pytest.raises(psycopg.errors.InsufficientPrivilege), transaction(pool) as connection:
            connection.execute("INSERT INTO hub.schema_version VALUES (2, 'x', %s, now())", ("0" * 64,))
    finally:
        pool.close()


def test_an_unreachable_database_times_out_and_closes_the_pool(migrated_db: HubDatabase) -> None:
    dsn = parse_connection_string(migrated_db.url("t_hub_nobody", "f" * 64))
    with pytest.raises(PoolTimeout):
        open_pool(dsn, timeout=0.5)


def test_a_returned_connection_is_reset(migrated_db: HubDatabase) -> None:
    pool = open_pool(parse_connection_string(migrated_db.writer_url), max_size=1)
    try:
        with pool.connection() as connection:
            connection.execute("SET search_path = public")
            connection.execute("SET TIME ZONE 'Europe/Zurich'")
            connection.commit()
        with pool.connection() as connection:
            row = connection.execute("SELECT current_setting('search_path'), current_setting('TimeZone')").fetchone()
        assert row == ("pg_catalog, hub, pg_temp", "UTC")
    finally:
        pool.close()
