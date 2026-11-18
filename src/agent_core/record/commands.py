"""The commands `export`, `verify` and `restore` (RECORD-PROTOCOL.md section 12.3).

Each command returns the one line it prints and its exit code. A line holds only the fixed words of
section 12.3, positions, record hashes and failure values: never a value of the configuration, a
path, a stored value or the message of an error (section 13).
"""

from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..keys import KeyRing, load_keys
from ..signing import b64url_decode, ed25519_public_key_valid, system_clock, valid_key_id
from ..teams import load_roster
from .connection import (
    WRITER_VARIABLE,
    ConnectionString,
    ConnectionStringError,
    libpq_supported,
    read_connection_string,
)
from .export import ExportInvalid, ValueUnwritable, parse_export, verify_export, write_export
from .failures import FILE_INVALID
from .state import Head
from .storage import (
    SCHEMA_MISSING,
    StorageError,
    check_schema,
    read_snapshot,
    restore_rows,
    writer_connection,
    writer_rights_hold,
)
from .verify import FoldResult, verify_record

KEYS_VARIABLE = "AGENT_CORE_KEYS"
TEAMS_VARIABLE = "AGENT_CORE_TEAMS"

OPTION_OUT = "--out"
OPTION_KEYS = "--keys"
OPTION_TEAMS = "--teams"
OPTION_IN = "--in"
OPTION_HUB_KEY = "--hub-key"

# Section 12.3: an export file is created with this mode.
EXPORT_MODE = 0o600

REFUSED_CONFIG = "refused config"
REFUSED_FILE_EXISTS = "refused file_exists"
REFUSED_FILE_WRITE = "refused file_write"
REFUSED_DATABASE = "refused database"
REFUSED_VALUE_UNWRITABLE = "refused value_unwritable"
REFUSED_HUB_KEY_MISSING = "refused hub_key_missing"
REFUSED_SCHEMA_MISSING = "refused schema_missing (run python -m agent_core.record migrate first)"
REFUSED_SCHEMA_VERSION = "refused schema_version"
REFUSED_WRITER_RIGHTS = "refused writer_rights"
REFUSED_NOT_EMPTY = "refused not_empty"

Outcome = tuple[str, int]
Clock = Callable[[], int]


class _ConfigFault(Exception):
    """A fault of the arguments, a variable or an input file: `refused config`."""


def _refused(line: str) -> Outcome:
    return line, 1


def _head_words(head: Head | None) -> str:
    """`<position> <record hash or ->`, `0 -` for an empty record."""
    return "0 -" if head is None else f"{head.position} {head.record_hash}"


def _options(args: Sequence[str], once: frozenset[str], repeated: str | None) -> tuple[dict[str, str], list[str]]:
    """The arguments as pairs of an option and its value (section 12.3).

    Every option of `once` is given exactly once; the option `repeated`, when the command has one,
    any number of times; no other argument exists. Returns the values of `once` and the values of
    `repeated` in their order.
    """
    if len(args) % 2:
        raise _ConfigFault()
    values: dict[str, str] = {}
    repeats: list[str] = []
    for name, value in zip(args[::2], args[1::2]):
        if name == repeated:
            repeats.append(value)
        elif name not in once or name in values:
            raise _ConfigFault()
        else:
            values[name] = value
    if values.keys() != once:
        raise _ConfigFault()
    return values, repeats


def _hub_keys(values: Sequence[str]) -> dict[str, bytes]:
    """`ID=PUBLIC_KEY` values: a hub key id by the `id` rule, `=`, and an Ed25519 public key as in
    KEY-PROTOCOL.md section 2 (canonical base64url of 32 bytes, no small-order point); each `ID` at
    most once."""
    keys: dict[str, bytes] = {}
    for value in values:
        key_id, _, text = value.partition("=")
        raw = b64url_decode(text)
        if not valid_key_id(key_id) or key_id in keys or raw is None or not ed25519_public_key_valid(raw):
            raise _ConfigFault()
        keys[key_id] = raw
    return keys


def _load_keys(keys_path: str, teams_path: str, now: int) -> KeyRing:
    """The keys file loaded with the teams file by the loader of KEY-PROTOCOL.md section 3."""
    try:
        return load_keys(Path(keys_path), load_roster(Path(teams_path)), now)
    except (OSError, ValueError, TypeError):
        # ValueError covers the refusals of both loaders and a file that is not UTF-8; TypeError a
        # YAML mapping key that cannot be compared.
        raise _ConfigFault() from None


def _variable(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise _ConfigFault()
    return value


def _read_file(path: str) -> bytes:
    try:
        return Path(path).read_bytes()
    except OSError:
        raise _ConfigFault() from None


def _verification_outcome(result: FoldResult, success: str) -> Outcome:
    """`failed <position> <failure>` (exit 1), or `<success> <position> <record hash or ->`."""
    failure = result.failure
    if failure is not None:
        return _refused(f"failed {failure.position} {failure.failure}")
    return f"{success} {_head_words(result.state.head)}", 0


# --- export --------------------------------------------------------------------------------------


def _write_all(descriptor: int, data: bytes) -> None:
    """Write all of `data` to `descriptor` without a buffer, so that every failure of the system
    call surfaces here and nothing is left to be written by a later close."""
    view = memoryview(data)
    while view:
        count = os.write(descriptor, view)
        if count == 0:
            # A write that makes no progress would loop forever; it is a failed write.
            raise OSError("write made no progress")
        view = view[count:]


def _export_into(descriptor: int, writer: ConnectionString, clock: Clock) -> Outcome:
    """Read the snapshot and write the file into the open, new `descriptor`; the caller closes it."""
    try:
        with writer_connection(writer) as connection:
            snapshot = read_snapshot(connection)
    except StorageError:
        return _refused(REFUSED_DATABASE)
    try:
        data = write_export(snapshot.events, snapshot.checkpoints, clock())
    except ValueUnwritable:
        return _refused(REFUSED_VALUE_UNWRITABLE)
    try:
        # The mode of os.open is narrowed by the umask; the file gets exactly 0600.
        os.fchmod(descriptor, EXPORT_MODE)
        _write_all(descriptor, data)
        os.fsync(descriptor)
    except OSError:
        return _refused(REFUSED_FILE_WRITE)
    last = snapshot.events[-1] if snapshot.events else None
    return f"exported {_head_words(None if last is None else Head(last.position, last.received_at, last.record_hash))}", 0


def _closed(descriptor: int) -> bool:
    """Close `descriptor` exactly once; whether the close succeeded. The descriptor is released
    also when the close fails, so it is never closed a second time."""
    try:
        os.close(descriptor)
    except OSError:
        return False
    return True


def _remove(path: str) -> None:
    """Remove the FILE this run created.

    When the removal itself fails, the line of the failure that stopped the export is still the
    line printed: section 12.3 gives each refusal by its cause and no line for a failed removal,
    and section 13 allows no other output. The exit code is 1 and no `exported` is printed, so the
    file that remains is no export (sections 12.3 and 12.4), as for a FILE left by a killed run.
    """
    with suppress(OSError):
        os.unlink(path)


def run_export(args: Sequence[str], environ: Mapping[str, str], clock: Clock = system_clock) -> Outcome:
    """`python -m agent_core.record export --out FILE` (section 12.3).

    The configuration first; then FILE is created exclusively with mode 0600, before any
    connection, so that an existing FILE (also a symbolic link) is refused and left unchanged;
    then the snapshot is read and written without a buffer. Whatever happens after FILE was
    created, the descriptor is closed exactly once, and FILE stays only when the outcome is
    `exported`: on every refusal FILE is removed before the line is printed, and on an unexpected
    exception it is removed before the exception propagates. A failed close is `refused
    file_write`; a close that fails after an earlier refusal keeps that refusal.
    """
    try:
        values, _ = _options(args, frozenset({OPTION_OUT}), None)
        writer = read_connection_string(environ, WRITER_VARIABLE)
    except (_ConfigFault, ConnectionStringError):
        return _refused(REFUSED_CONFIG)
    if not libpq_supported():
        return _refused(REFUSED_CONFIG)
    path = values[OPTION_OUT]
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, EXPORT_MODE)
    except FileExistsError:
        return _refused(REFUSED_FILE_EXISTS)
    except OSError:
        return _refused(REFUSED_FILE_WRITE)
    try:
        outcome = _export_into(descriptor, writer, clock)
    except BaseException:
        # No line of section 12.3 fits an unexpected exception (also an interrupt), so it
        # propagates; the descriptor is closed and FILE removed first. A failing close here does
        # not replace the exception.
        _closed(descriptor)
        _remove(path)
        raise
    if not _closed(descriptor) and outcome[1] == 0:
        outcome = _refused(REFUSED_FILE_WRITE)
    if outcome[1] != 0:
        _remove(path)
    return outcome


# --- verify --------------------------------------------------------------------------------------


def run_verify(args: Sequence[str], environ: Mapping[str, str] | None = None, clock: Clock = system_clock) -> Outcome:
    """`python -m agent_core.record verify --keys KEYS --teams TEAMS --in FILE [--hub-key
    ID=PUBLIC_KEY]...` (section 12.3): offline, without any variable and without a database."""
    try:
        values, hub_values = _options(args, frozenset({OPTION_KEYS, OPTION_TEAMS, OPTION_IN}), OPTION_HUB_KEY)
        hub_keys = _hub_keys(hub_values)
        keys = _load_keys(values[OPTION_KEYS], values[OPTION_TEAMS], clock())
        data = _read_file(values[OPTION_IN])
    except _ConfigFault:
        return _refused(REFUSED_CONFIG)
    return _verification_outcome(verify_export(data, keys, hub_keys), "verified")


# --- restore -------------------------------------------------------------------------------------


def run_restore(args: Sequence[str], environ: Mapping[str, str], clock: Clock = system_clock) -> Outcome:
    """`python -m agent_core.record restore --in FILE [--hub-key ID=PUBLIC_KEY]...` (section 12.3).

    Before step 1 the configuration; step 1 checks FILE ((a) the file format, (b) a public key
    for the hub key id of every checkpoint, (c) the rest of the verification against the keys file
    of AGENT_CORE_KEYS loaded with the teams file of AGENT_CORE_TEAMS); step 2 connects as the
    writer and runs the start steps `schema` and `writer`; steps 3 and 4 insert into an empty
    record in one transaction. Nothing is written unless step 4 commits.
    """
    try:
        values, hub_values = _options(args, frozenset({OPTION_IN}), OPTION_HUB_KEY)
        hub_keys = _hub_keys(hub_values)
        writer = read_connection_string(environ, WRITER_VARIABLE)
        keys = _load_keys(_variable(environ, KEYS_VARIABLE), _variable(environ, TEAMS_VARIABLE), clock())
        data = _read_file(values[OPTION_IN])
    except (_ConfigFault, ConnectionStringError):
        return _refused(REFUSED_CONFIG)
    if not libpq_supported():
        return _refused(REFUSED_CONFIG)
    try:
        export = parse_export(data)
    except ExportInvalid as invalid:
        return _refused(f"failed {invalid.position} {FILE_INVALID}")
    if any(checkpoint.hub_key_id not in hub_keys for checkpoint in export.checkpoints):
        return _refused(REFUSED_HUB_KEY_MISSING)
    result = verify_record(export.events, export.checkpoints, keys, hub_keys, None)
    if result.failure is not None:
        return _verification_outcome(result, "restored")
    try:
        with writer_connection(writer) as connection:
            schema = check_schema(connection)
            if schema is not None:
                return _refused(REFUSED_SCHEMA_MISSING if schema == SCHEMA_MISSING else REFUSED_SCHEMA_VERSION)
            if not writer_rights_hold(connection, writer.role):
                return _refused(REFUSED_WRITER_RIGHTS)
            if not restore_rows(connection, export.events, export.checkpoints):
                return _refused(REFUSED_NOT_EMPTY)
    except StorageError:
        return _refused(REFUSED_DATABASE)
    return _verification_outcome(result, "restored")
