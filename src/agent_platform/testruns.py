"""Test runs of a session as platform operations (ADR-079, 13-work-vm.md section 12; twin of the web's test run card,
the runs of the work VM panel and its log viewer).

- `parse_run`: a decrypted `test.run` body checked against `contracts/events/objects.json#/$defs/body_test_run`
  (every field required, no other field); anything else is None and counted as `invalid`;
- `test list --session|--task`: the runs, the latest `test.run` event per `run_id` (the final one replaces the
  running one), newest start first, without their `tail`. The events are the task's `test.run` events of the
  workgroup scope, opened and verified like the chat; an event of another task the hub slips in is left out;
- `test show RUN_ID`: one run with its `tail` and its log reference;
- `test log RUN_ID [--offset N] [--limit N] [--output FILE]`: the run's log, an encrypted object of the task
  (`GET /v1/tasks/{task}/logs/{object}` and `.../chunks/{index}`, 04-crypto.md section 4.12). The manifest must
  verify with the device that wrote the event and name exactly what the decrypted event names: `ref` the run (or
  work VM job), `object` the log, org, workgroup, task, session and the sizes; otherwise the log is refused
  (`hub_refused`, `reason`). A window of the kept bytes is printed as text (chunks of `runlog.CHUNK_BYTES` bytes, only
  those of the window are fetched); `--output` writes the whole kept log to a file chunk by chunk and checks it
  against the manifest's size and digest. A job ID (`j-…`) reads the log of a work VM job of a `workvm.run` event.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, cast

from agent_platform import workvm
from agent_platform.account import Account, unverified
from agent_platform.commands import Context, Tree
from agent_platform.errors import PlatformError
from agent_platform.eventio import open_all, pull
from agent_platform.hub import segment
from agent_platform.orgs import find_org
from agent_platform.sessions import session_row, task_row
from agent_runner.crypto import runlog
from agent_runner.crypto.encoding import b64u_decode, parse_time
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.log.state import WG

RUN_KIND = "test.run"
KINDS = ("lint", "format", "unit", "integration", "security", "custom")
STATES = ("running", "passed", "failed", "error", "timed_out", "cancelled")
REASONS = ("credit_exhausted", "restart", "start_failed", "transfer_failed", "vm_lost", "cancelled")
SIZES = ("standard", "large", "xlarge")
RUN_ID = re.compile(r"tr-[0-9a-f]{12}")
JOB_ID = re.compile(r"j-[0-9a-f]{12}")
WINDOW = 64 * 1024
MAX_WINDOW = 1024 * 1024

_COUNT = workvm._COUNT
_NULLABLE_COUNT = workvm._nullable(_COUNT)
SUMMARY_KEYS = ("passed", "failed", "skipped", "errors")


def _summary(value: object) -> bool:
    return value is None or (
        isinstance(value, dict)
        and set(value) == set(SUMMARY_KEYS)
        and all(_NULLABLE_COUNT(value[key]) for key in SUMMARY_KEYS)
    )


RUN_SHAPE: Mapping[str, workvm.Check] = {
    "run_id": workvm._pattern(RUN_ID.pattern),
    "operation_id": workvm._UUID,
    "kind": workvm._one_of(*KINDS),
    "command": workvm._text(4096),
    "cwd": workvm._text(4096),
    "size": workvm._one_of(*SIZES),
    "state": workvm._one_of(*STATES),
    "exit_code": workvm._nullable(workvm._integer(0, 255)),
    "started_at": workvm._time,
    "ended_at": workvm._nullable(workvm._time),
    "duration_ms": _COUNT,
    "ready_ms": _COUNT,
    "summary": _summary,
    "log": workvm.LOG_REF,
    "tail": workvm._text(8192),
    "reason": workvm._nullable(workvm._one_of(*REASONS)),
}
LIST_FIELDS = tuple(key for key in RUN_SHAPE if key != "tail")


def parse_run(body: object) -> dict[str, Any] | None:
    """A `test.run` body, or None when it does not follow the contract."""
    return cast(dict[str, Any], body) if workvm._matches(body, RUN_SHAPE) else None


@dataclass
class Found:
    """The runs (or jobs) of a task, latest event per ID in event order, with what the reader left out."""

    task: str
    wg: str
    session: str | None
    items: dict[str, tuple[dict[str, Any], dict[str, Any]]]
    """ID -> (verified event without its body, checked body)"""
    locked: int
    invalid: int
    unverified: int


def _place(account: Account, session: str | None, task: str | None) -> tuple[str, str]:
    """`(task, wg)` of the session or of the task (exactly one of them)."""
    if (session is None) == (task is None):
        raise PlatformError("invalid_argument", reason="session_or_task")
    if session is not None:
        row = session_row(account, session)["session"]
        if not isinstance(row.get("task_id"), str):
            raise unverified("response_invalid")
        return str(row["task_id"]), str(row["wg_id"])
    row = task_row(account, cast(str, task))
    return str(row["task_id"]), str(row["wg_id"])


def _read(account: Account, session: str | None, task: str | None, jobs: bool = False) -> Found:
    """The latest `test.run` (or with `jobs` `workvm.run`) event per ID of the task, of `session` only if named."""
    task_id, wg = _place(account, session, task)
    kind, parse, key = (workvm.RUN_KIND, workvm.parse_run, "job_id") if jobs else (RUN_KIND, parse_run, "run_id")
    items, _last = pull(account, wg, {"kinds": kind, "task": task_id})
    events, skipped = open_all(account, find_org(account, wg), (WG, wg), items)
    found = Found(task_id, wg, session, {}, 0, 0, skipped)
    for event in sorted(events, key=lambda opened: int(opened["seq"])):
        if event.get("locked"):
            found.locked += 1
            continue
        if event["task"] != task_id or event["kind"] != kind:
            found.unverified += 1  # slipped in by the hub
            continue
        body = parse(event.get("body"))
        if body is None:
            found.invalid += 1
            continue
        if session is not None and event["session"] != session:
            continue
        found.items.pop(body[key], None)
        found.items[body[key]] = ({name: value for name, value in event.items() if name != "body"}, body)
    return found


def _row(event: Mapping[str, Any], body: Mapping[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    return {"session_id": event["session"], **{name: body[name] for name in names}}


def _counts(found: Found) -> dict[str, Any]:
    return {"locked": found.locked, "invalid": found.invalid, "unverified": found.unverified}


def run_list(account: Account, session: str | None, task: str | None) -> dict[str, Any]:
    found = _read(account, session, task)
    pairs = list(found.items.values())
    ordered = sorted(enumerate(pairs), key=lambda pair: (-parse_time(pair[1][1]["started_at"]).timestamp(), -pair[0]))
    return {
        "task_id": found.task,
        "session_id": session,
        "runs": [_row(event, body, LIST_FIELDS) for _index, (event, body) in ordered],
        **_counts(found),
    }


def _run_id(value: str) -> None:
    if RUN_ID.fullmatch(value) is None:
        raise PlatformError("invalid_argument", reason="run_id")


def run_show(account: Account, run: str, session: str | None, task: str | None) -> dict[str, Any]:
    _run_id(run)
    found = _read(account, session, task)
    pair = found.items.get(run)
    if pair is None:
        raise PlatformError("not_found", run_id=run)
    return {"task_id": found.task, "run": _row(*pair, tuple(RUN_SHAPE)), **_counts(found)}


# --- the log ---


@dataclass
class Log:
    """A verified log manifest and the key that opens its chunks."""

    task: str
    object_id: str
    key: bytes
    chunks: int
    stored: int
    size: int
    digest: str


def _manifest(account: Account, found: Found, ident: str, event: Mapping[str, Any], ref: Mapping[str, Any]) -> Log:
    """The log of `ref` after the checks of the module docstring."""
    object_id = str(ref["object_id"])
    answer = account.call("GET", f"/v1/tasks/{segment(found.task)}/logs/{segment(object_id)}")
    org = find_org(account, found.wg)
    principal = account.org_log(org).state.principals[str(event["author"])]  # the event verified with it
    try:
        manifest = runlog.verify_manifest(answer.get("manifest"), answer.get("sig"), b64u_decode(principal.sig))
    except CryptoError as error:
        raise unverified(error.code) from None
    expected = {
        "ref": ident,
        "object": object_id,
        "org": org,
        "wg": found.wg,
        "task": found.task,
        "session": event["session"],
        "author": event["author"],
        "bytes": ref["bytes"],
        "stored": ref["stored_bytes"],
    }
    if any(manifest[name] != value for name, value in expected.items()):
        raise unverified("log_mismatch")
    key = account.keys(org).get((WG, found.wg, cast(int, manifest["epoch"])))
    if key is None:
        raise unverified("log_locked")
    return Log(
        found.task,
        object_id,
        runlog.log_key(key, b64u_decode(manifest["salt"]), object_id),
        cast(int, manifest["chunks"]),
        cast(int, manifest["stored"]),
        cast(int, manifest["size"]),
        str(manifest["digest"]),
    )


def _sealed(account: Account, log: Log, index: int) -> bytes:
    answer = account.call("GET", f"/v1/tasks/{segment(log.task)}/logs/{segment(log.object_id)}/chunks/{index}")
    try:
        return b64u_decode(answer.get("ct"))
    except CryptoError as error:
        raise unverified(error.code) from None


def _piece(log: Log, index: int, sealed: bytes) -> bytes:
    """The output bytes of chunk `index`: full chunks before the last one, the rest in the last one."""
    try:
        piece = runlog.open_chunk(log.key, log.object_id, index, sealed)
    except CryptoError as error:
        raise unverified(error.code) from None
    expected = runlog.CHUNK_BYTES if index < log.chunks - 1 else log.stored - index * runlog.CHUNK_BYTES
    if len(piece) != expected:
        raise unverified("header_invalid")
    return piece


def _chunks(account: Account, log: Log, first: int, end: int) -> Iterator[tuple[int, bytes, bytes]]:
    """`(index, sealed, piece)` of the chunks `first` up to before `end`, one at a time."""
    for index in range(first, end):
        sealed = _sealed(account, log, index)
        yield index, sealed, _piece(log, index, sealed)


def _target(account: Account, ident: str, session: str | None, task: str | None) -> tuple[Found, Log, int]:
    """The run (or job), its verified log and the full output length the event names."""
    jobs = JOB_ID.fullmatch(ident) is not None
    if not jobs:
        _run_id(ident)
    found = _read(account, session, task, jobs=jobs)
    pair = found.items.get(ident)
    if pair is None:
        raise PlatformError("not_found", run_id=ident)
    event, body = pair
    ref = body.get("log")
    if ref is None:
        raise PlatformError("not_found", run_id=ident, reason="no_log")
    return found, _manifest(account, found, ident, event, ref), int(ref["bytes"])


def _describe(ident: str, log: Log, ref_bytes: int) -> dict[str, Any]:
    return {
        "run_id": ident,
        "object_id": log.object_id,
        "bytes": ref_bytes,
        "stored_bytes": log.stored,
        "truncated": log.stored < ref_bytes,
    }


def run_log(
    account: Account,
    ident: str,
    session: str | None,
    task: str | None,
    *,
    offset: int | None,
    limit: int | None,
    output: str | None,
) -> dict[str, Any]:
    if output is not None and (offset is not None or limit is not None):
        raise PlatformError("invalid_argument", reason="output_whole_log")
    start = 0 if offset is None else offset
    size = WINDOW if limit is None else limit
    if start < 0 or not 1 <= size <= MAX_WINDOW:
        raise PlatformError("invalid_argument")
    found, log, full = _target(account, ident, session, task)
    described = _describe(ident, log, full)
    if output is not None:
        return {**described, "task_id": found.task, **_write(account, log, output)}
    if start > log.stored:
        raise PlatformError("invalid_argument", reason="offset", stored_bytes=log.stored)
    end = min(log.stored, start + size)
    first = start // runlog.CHUNK_BYTES
    last = -(-end // runlog.CHUNK_BYTES) if end > start else first
    window = b"".join(
        piece[max(0, start - index * runlog.CHUNK_BYTES) : end - index * runlog.CHUNK_BYTES]
        for index, _sealed, piece in _chunks(account, log, first, last)
    )
    return {
        **described,
        "task_id": found.task,
        "offset": start,
        "end": end,
        "next_offset": end if end < log.stored else None,
        "text": window.decode("utf-8", "replace"),
    }


def _write(account: Account, log: Log, output: str) -> dict[str, Any]:
    """The whole kept log into `output` (created or replaced, mode 0600), chunk by chunk; checked against the
    manifest's size and digest, the file removed when anything fails."""
    try:
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise PlatformError("invalid_argument", reason="output_unwritable") from None
    hashes: list[bytes] = []
    size = written = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            for _index, sealed, piece in _chunks(account, log, 0, log.chunks):
                hashes.append(runlog.chunk_hash(sealed))
                size += len(sealed)
                written += handle.write(piece)
        if size != log.size or runlog.digest_of_hashes(hashes) != log.digest:
            raise unverified("header_invalid")
    except BaseException:
        os.unlink(output)
        raise
    return {"output": output, "written_bytes": written}


# --- commands ---


def _list(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return run_list(account, args.session, args.task)


def _show(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return run_show(account, args.run_id, args.session, args.task)


def _log(account: Account, args: Any, context: Context) -> dict[str, Any]:
    return run_log(
        account, args.run_id, args.session, args.task, offset=args.offset, limit=args.limit, output=args.output
    )


def _where(sub: Any) -> None:
    sub.add_argument("--session", help="the runs of this session")
    sub.add_argument("--task", help="the runs of every session of this task")


def register(tree: Tree) -> None:
    group = tree.group("test", "test runs of sessions (lint, formatting, unit and integration tests)")
    listed = tree.command(
        group,
        "list",
        _list,
        "the test runs of a session or task, newest first: state, kind, summary, duration, log size",
        example="agent-platform test list --session <session_id>",
        words="testläufe tests prüfungen lint unit integration ergebnis test runs results",
        then="agent-platform test show <run_id> --session <session_id>",
        trim="first",
    )
    _where(listed)
    shown = tree.command(
        group,
        "show",
        _show,
        "one test run: command, state, exit code, summary, the end of its output and its log",
        example="agent-platform test show <run_id> --session <session_id>",
        words="testlauf ausgabe ende fehler exit code tail test run output",
        then="agent-platform test log <run_id> --session <session_id>",
    )
    shown.add_argument("run_id")
    _where(shown)
    logged = tree.command(
        group,
        "log",
        _log,
        "the complete output of a test run or work VM job, decrypted: a window of it or the whole log into a file",
        example="agent-platform test log <run_id> --session <session_id> [--offset 0] [--limit 65536]",
        words="testprotokoll protokoll logdatei ausgabe herunterladen test log download output file",
    )
    logged.add_argument("run_id", help="tr-… of a test run or j-… of a work VM job")
    _where(logged)
    logged.add_argument("--offset", type=int, help="first byte of the window in the kept log (default 0)")
    logged.add_argument("--limit", type=int, help=f"bytes of the window (default {WINDOW}, at most {MAX_WINDOW})")
    logged.add_argument("--output", help="write the whole kept log to this file instead")
