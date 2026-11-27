"""Blocks of rows bounded by bytes, for the long reads of the operator commands (HOT-27).

`agent-hub export` and `agent-hub audit verify` read whole tables. A block of a fixed number of rows holds as much
memory as its largest rows: 500 events with a large ciphertext each are several hundred MB. So these readers first ask
the database for the stored size of every row (a small number per row) and cut the rows into blocks with `blocks`:
consecutive rows whose sizes add up to at most `max_bytes`, and at most `max_rows` of them. A single row larger than
`max_bytes` forms a block of its own - its value has to be in memory once anyway - so the memory of a reader is
bounded by the larger of `max_bytes` and its largest row, never by the number of rows.

`size_sql` is the SQL expression of a row's size: the byte length of every value whose size varies (`bytea` and `text`
read their stored length without decompressing it, every other variable type the length of its text form) plus a
constant per row for the fixed-size columns and the Python objects of the row.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Sequence

ROW_BYTES = 512
"""What a row costs beyond its variable values: the fixed-size columns and the objects that carry the row."""
FIXED_TYPES = frozenset(
    {"uuid", "integer", "smallint", "bigint", "boolean", "timestamp with time zone", "date", "real"}
)
"""Column types whose values have a small fixed size (counted in `ROW_BYTES`)."""
RAW_LENGTH_TYPES = frozenset({"bytea", "text"})
"""Column types whose `octet_length` is the stored length, read without decompressing the value."""


def size_sql(columns: Sequence[tuple[str, str]]) -> str:
    """The SQL expression of a row's size in bytes (module docstring); `columns` are `(expression, data type)`."""
    parts = [str(ROW_BYTES)]
    for expression, data_type in columns:
        if data_type in FIXED_TYPES:
            continue
        value = expression if data_type in RAW_LENGTH_TYPES else f"({expression})::text"
        parts.append(f"COALESCE(octet_length({value})::bigint, 0)")
    return " + ".join(parts)


def blocks(sizes: Iterable[int], *, max_rows: int, max_bytes: int) -> Iterator[int]:
    """The lengths of consecutive blocks of rows with these sizes (module docstring); together they cover every row."""
    count = total = 0
    for size in sizes:
        if count and (count == max_rows or total + size > max_bytes):
            yield count
            count = total = 0
        count += 1
        total += size
    if count:
        yield count
