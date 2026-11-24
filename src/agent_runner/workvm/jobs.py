"""The runner's side of the job helper in the work VM (docs/platform/spec/13-work-vm.md sections 3 and 5).

Commands run in the work VM through `/usr/local/sbin/workvm-job` over Lima's SSH (`Shell`, injected). The VM belongs to
the session and its root may have replaced the helper: every answer is untrusted. `parse_wait` therefore accepts only
one JSON header line of at most `HEADER_LIMIT` bytes with a closed set of fields and types, followed by exactly
`body_bytes` bytes (at most the requested limit); anything else is `WorkVmError("workvm_answer_invalid")`, which the
tools report as `unavailable`. Output becomes text with invalid UTF-8 and NUL replaced by U+FFFD (the journal and
the event store refuse NUL), and is never interpreted.
"""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from agent_runner.errors import RunnerError

HELPER = "/usr/local/sbin/workvm-job"
HEADER_LIMIT = 4096
OUTPUT_LIMIT = 32768
JOB_ID_RE = re.compile(r"^j-[0-9a-f]{12}$")
STATES = frozenset({"running", "exited", "killed", "timed_out"})
SIGNALS = frozenset({"TERM", "INT", "HUP", "KILL"})
MAX_SAFE = 2**53 - 1
LIST_LIMIT = 256 * 1024


class WorkVmError(RunnerError):
    """A work VM failure with a closed code (`workvm_*`)."""


@dataclass(frozen=True)
class ShellResult:
    returncode: int
    stdout: bytes
    truncated: bool


class Shell(Protocol):
    """Runs `argv` in the work VM as root (`sudo` inside the VM) and returns its stdout, at most `limit` bytes."""

    async def __call__(
        self,
        argv: Sequence[str],
        *,
        stdin: bytes | Path | None,
        timeout: float,
        limit: int,
        out: Path | None = None,
    ) -> ShellResult: ...


@dataclass(frozen=True)
class JobView:
    """One job as the tools return it (`mcp/objects.json#/$defs/output_workvm_exec` without `synced`)."""

    job_id: str
    command: str
    state: str
    exit_code: int | None
    output: str
    output_start: int
    output_bytes: int
    duration_ms: int

    def result(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "command": self.command,
            "state": self.state,
            "exit_code": self.exit_code,
            "output": self.output,
            "output_start": self.output_start,
            "output_bytes": self.output_bytes,
            "duration_ms": self.duration_ms,
        }


def new_job_id() -> str:
    return "j-" + secrets.token_hex(6)


def text(data: bytes) -> str:
    """Output bytes as text: invalid UTF-8 and NUL become U+FFFD; lone surrogates cannot occur after decoding."""
    return data.decode("utf-8", errors="replace").replace("\x00", "�")


def command_label(command: str) -> str:
    """The first 256 characters of a command (the job's label in answers and lists)."""
    return text(command.encode("utf-8", errors="replace"))[:256]


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SAFE:
        raise WorkVmError("workvm_answer_invalid")
    return value


def _exit_code(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int or not 0 <= value <= 255:
        raise WorkVmError("workvm_answer_invalid")
    return value


def _header(data: bytes) -> tuple[dict[str, Any], bytes]:
    newline = data.find(b"\n", 0, HEADER_LIMIT + 1)
    if newline < 0:
        raise WorkVmError("workvm_answer_invalid")
    try:
        header = json.loads(data[:newline].decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WorkVmError("workvm_answer_invalid") from None
    if not isinstance(header, dict):
        raise WorkVmError("workvm_answer_invalid")
    return header, data[newline + 1 :]


def parse_wait(result: ShellResult, *, job_id: str, command: str, limit: int) -> JobView:
    """The job of a `workvm-job wait` answer (`not_found` for an unknown job)."""
    piece = parse_piece(result, limit=limit)
    return JobView(
        job_id=job_id,
        command=command_label(command),
        state=piece.state,
        exit_code=piece.exit_code,
        output=text(piece.body),
        output_start=piece.start,
        output_bytes=piece.output_bytes,
        duration_ms=piece.duration_ms,
    )


@dataclass(frozen=True)
class Piece:
    """Raw output bytes of a job from `start` (a `wait` answer read as bytes, for a log)."""

    state: str
    exit_code: int | None
    output_bytes: int
    start: int
    body: bytes
    duration_ms: int


def parse_piece(result: ShellResult, *, limit: int) -> Piece:
    """The raw bytes of a `workvm-job wait` answer (the same checks as `parse_wait`, the body stays bytes)."""
    if result.returncode != 0 or result.truncated:
        raise WorkVmError("workvm_answer_invalid")
    header, body = _header(result.stdout)
    if header == {"error": "not_found"}:
        raise WorkVmError("workvm_job_unknown")
    if set(header) != {"state", "exit_code", "output_bytes", "output_start", "body_bytes", "duration_ms"}:
        raise WorkVmError("workvm_answer_invalid")
    state = header["state"]
    if not isinstance(state, str) or state not in STATES:
        raise WorkVmError("workvm_answer_invalid")
    output_bytes, start = _count(header["output_bytes"]), _count(header["output_start"])
    body_bytes, duration = _count(header["body_bytes"]), _count(header["duration_ms"])
    if body_bytes != len(body) or body_bytes > limit or start + body_bytes > output_bytes:
        raise WorkVmError("workvm_answer_invalid")
    return Piece(str(state), _exit_code(header["exit_code"]), output_bytes, start, body, duration)


def start_argv(job_id: str, cwd: str, timeout: int | None) -> list[str]:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError(job_id)
    return ["sudo", "-n", HELPER, "start", job_id, cwd, str(timeout or 0)]


def start_input(command: str, env: Sequence[Mapping[str, str]], stdin: str | None) -> bytes:
    spec: dict[str, Any] = {"command": command, "env": {item["name"]: item["value"] for item in env}}
    if stdin is not None:
        spec["stdin"] = stdin
    return json.dumps(spec).encode("utf-8")


def wait_argv(job_id: str, offset: int | None, limit: int, wait: float) -> list[str]:
    if not JOB_ID_RE.fullmatch(job_id):
        raise ValueError(job_id)
    return ["sudo", "-n", HELPER, "wait", job_id, str(-1 if offset is None else offset), str(limit), f"{wait:g}"]


def kill_argv(job_id: str, signal: str) -> list[str]:
    if not JOB_ID_RE.fullmatch(job_id) or signal not in SIGNALS:
        raise ValueError(job_id)
    return ["sudo", "-n", HELPER, "kill", job_id, signal]


def list_argv() -> list[str]:
    return ["sudo", "-n", HELPER, "list"]


def parse_list(result: ShellResult) -> list[dict[str, Any]]:
    """The jobs of a `workvm-job list` answer, each checked (an invalid entry makes the whole answer invalid)."""
    if result.returncode != 0 or result.truncated:
        raise WorkVmError("workvm_answer_invalid")
    try:
        items = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise WorkVmError("workvm_answer_invalid") from None
    if not isinstance(items, list) or len(items) > 50:
        raise WorkVmError("workvm_answer_invalid")
    jobs: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict) or set(item) != {
            "job_id",
            "command",
            "state",
            "exit_code",
            "started_at",
            "duration_ms",
        }:
            raise WorkVmError("workvm_answer_invalid")
        job_id, command, state = item["job_id"], item["command"], item["state"]
        if not isinstance(job_id, str) or not JOB_ID_RE.fullmatch(job_id) or not isinstance(command, str):
            raise WorkVmError("workvm_answer_invalid")
        if not isinstance(state, str) or state not in STATES:
            raise WorkVmError("workvm_answer_invalid")
        jobs.append(
            {
                "job_id": job_id,
                "command": command_label(command),
                "state": state,
                "exit_code": _exit_code(item["exit_code"]),
                "started_at": _count(item["started_at"]),
                "duration_ms": _count(item["duration_ms"]),
            }
        )
    return jobs
