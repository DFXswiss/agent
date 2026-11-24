"""Connection pool of the writer role and the transaction helper (02-data-model.md section 1.4).

The hub process connects only as the writer (`AGENT_HUB_DATABASE`). Every pooled connection acts as its login role,
resolves names in `pg_catalog`, then `hub`, with `pg_temp` last, and works in UTC.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from agent_core.record.connection import ConnectionString

SESSION_STATEMENTS = (
    "SET ROLE NONE",
    "SET search_path = pg_catalog, hub, pg_temp",
    "SET TIME ZONE 'UTC'",
)


def configure(connection: psycopg.Connection[Any]) -> None:
    """Session settings of every new pooled connection; leaves the connection idle."""
    for statement in SESSION_STATEMENTS:
        connection.execute(statement)
    connection.commit()


def reset(connection: psycopg.Connection[Any]) -> None:
    """Undo what a borrower changed in the session (settings, role) before the connection is lent again."""
    connection.execute("RESET ALL")
    configure(connection)


def open_pool(
    dsn: ConnectionString,
    *,
    min_size: int = 1,
    max_size: int = 10,
    timeout: float = 30.0,
    name: str = "agent-hub",
) -> ConnectionPool[psycopg.Connection[Any]]:
    """An open pool for `dsn` (the writer's connection string); waits until `min_size` connections exist."""
    pool: ConnectionPool[psycopg.Connection[Any]] = ConnectionPool(
        "",
        kwargs=dsn.connect_kwargs(),
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        name=name,
        configure=configure,
        reset=reset,
        check=ConnectionPool.check_connection,
        open=False,
    )
    try:
        pool.open(wait=True, timeout=timeout)
    except BaseException:
        pool.close()
        raise
    return pool


@contextmanager
def transaction(pool: ConnectionPool[psycopg.Connection[Any]]) -> Iterator[psycopg.Connection[Any]]:
    """A pooled connection inside one transaction: committed on normal exit, rolled back on an exception."""
    with pool.connection() as connection, connection.transaction():
        yield connection
