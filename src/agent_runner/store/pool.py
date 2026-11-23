"""Connection pool of the daemon (psycopg 3 async pool) for the store database."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool, PoolTimeout

from agent_runner.errors import RunnerError

MIN_SIZE = 1
MAX_SIZE = 8
OPEN_TIMEOUT_S = 10.0


async def open_pool(
    dsn: str, *, min_size: int = MIN_SIZE, max_size: int = MAX_SIZE, timeout: float = OPEN_TIMEOUT_S
) -> AsyncConnectionPool:
    """Open a pool and wait until `min_size` connections work; failure is `store_unavailable`."""
    pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False, name="agent-runner")
    try:
        await pool.open(wait=True, timeout=timeout)
    except PoolTimeout:
        await pool.close()
        raise RunnerError("store_unavailable") from None
    return pool
