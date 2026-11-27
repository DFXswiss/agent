"""Blocks of rows bounded by bytes (`agent_hub.db.blocks`, HOT-27)."""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from agent_hub.db.blocks import ROW_BYTES, blocks, size_sql


@pytest.mark.parametrize(
    ("sizes", "max_rows", "max_bytes", "expected"),
    [
        ([], 3, 10, []),
        ([1, 1, 1, 1, 1, 1, 1], 3, 100, [3, 3, 1]),  # the row limit
        ([4, 4, 4, 4], 10, 8, [2, 2]),  # the byte limit, exactly reached
        ([4, 3, 5], 10, 8, [2, 1]),
        ([4, 5, 4], 10, 8, [1, 1, 1]),
        ([1, 50, 1, 1], 10, 8, [1, 1, 2]),  # a row larger than the limit is read alone
        ([50, 50], 10, 8, [1, 1]),
        ([50], 1, 8, [1]),
    ],
)
def test_blocks(sizes: list[int], max_rows: int, max_bytes: int, expected: list[int]) -> None:
    found = list(blocks(iter(sizes), max_rows=max_rows, max_bytes=max_bytes))
    assert found == expected
    assert sum(found) == len(sizes)


def test_size_sql_counts_the_variable_values() -> None:
    text = size_sql([("id", "uuid"), ("ct", "bytea"), ("NULL::bytea", "bytea"), ("doc", "jsonb"), ("x", "money")])
    assert text == (
        f"{ROW_BYTES} + COALESCE(octet_length(ct)::bigint, 0) + COALESCE(octet_length(NULL::bytea)::bigint, 0)"
        " + COALESCE(octet_length((doc)::text)::bigint, 0) + COALESCE(octet_length((x)::text)::bigint, 0)"
    )
    assert size_sql([("id", "uuid")]) == str(ROW_BYTES)


def test_size_sql_measures_stored_rows(writer: psycopg.Connection[Any]) -> None:
    expression = size_sql([("b", "bytea"), ("t", "text"), ("j", "jsonb"), ("a", "ARRAY"), ("n", "integer")])
    found = writer.execute(
        f"SELECT {expression} FROM (VALUES (decode(repeat('00', 100000), 'hex'), 'abc', '{{\"k\": 1}}'::jsonb,"
        " ARRAY['x', 'yz'], 7), (NULL::bytea, NULL::text, NULL::jsonb, NULL::text[], NULL::integer))"
        " AS v(b, t, j, a, n)"
    ).fetchall()
    assert found == [(ROW_BYTES + 100000 + 3 + len('{"k": 1}') + len("{x,yz}"),), (ROW_BYTES,)]
