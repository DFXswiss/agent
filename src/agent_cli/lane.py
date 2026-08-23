"""Headless vendor-lane launcher (argv builders + subprocess runner)."""

from __future__ import annotations

import os
import re
import resource
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

LANE_ROLES = ("implementer", "reviewer", "pr-reviewer-quality", "pr-reviewer-logic")
LANE_VENDORS = ("grok", "codex")
WRITE_ROLES = frozenset({"implementer"})
GROK_LANE_MODEL = "grok-4.5"
CODEX_LANE_MODEL = "gpt-5.6-sol"
NPROC_CAP = 800
GROK_STRIP_ENV = ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")
STATUS_VALUES = ("complete", "partial", "timeout", "unavailable")

_STATUS_RE = re.compile(
    r"(?m)^STATUS:\s*(complete|partial|timeout|unavailable)\s*$",
    re.IGNORECASE,
)


@dataclass
class LaneResult:
    role: str
    vendor: str
    status: str
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str


# runner(argv, stdin_text) -> object with returncode, stdout, stderr
Runner = Callable[[list[str], str | None], object]


def _env_strip_prefix() -> list[str]:
    argv = ["env"]
    for key in GROK_STRIP_ENV:
        argv.extend(["-u", key])
    return argv


def grok_argv(*, spec_file: str, cwd: str, write: bool) -> list[str]:
    argv = _env_strip_prefix()
    argv.extend(["grok", "--prompt-file", spec_file, "-m", GROK_LANE_MODEL])
    if write:
        argv.extend(
            [
                "--permission-mode",
                "acceptEdits",
                "--allow",
                "Write",
                "--allow",
                "Edit",
                "--output-format",
                "plain",
                "--cwd",
                cwd,
            ]
        )
    else:
        argv.extend(
            [
                "--allow",
                "Read",
                "--allow",
                "Grep",
                "--allow",
                "Glob",
                "--deny",
                "Write",
                "--deny",
                "Edit",
                "--deny",
                "Bash",
                "--no-subagents",
                "--disable-web-search",
                "--output-format",
                "plain",
                "--cwd",
                cwd,
            ]
        )
    return argv


def codex_argv(*, cwd: str, write: bool, output_file: str) -> list[str]:
    sandbox = "workspace-write" if write else "read-only"
    argv = _env_strip_prefix()
    argv.extend(
        [
            "codex",
            "exec",
            "--model",
            CODEX_LANE_MODEL,
            "-c",
            "model_reasoning_effort=high",
            "--sandbox",
            sandbox,
            "--skip-git-repo-check",
            "--cd",
            cwd,
            "--output-last-message",
            output_file,
            "-",
        ]
    )
    return argv


def parse_status(output: str, returncode: int) -> str:
    matches = list(_STATUS_RE.finditer(output))
    if matches:
        return matches[-1].group(1).lower()
    if returncode == 124:
        return "timeout"
    if returncode != 0:
        return "unavailable"
    return "partial"


def _default_runner(argv: list[str], stdin_text: str | None) -> subprocess.CompletedProcess[str]:
    def _preexec() -> None:
        try:
            resource.setrlimit(resource.RLIMIT_NPROC, (NPROC_CAP, NPROC_CAP))
        except (ValueError, OSError, AttributeError):
            raise SystemExit("nproc cap not settable") from None

    return subprocess.run(
        argv,
        input=stdin_text,
        capture_output=True,
        text=True,
        check=False,
        preexec_fn=_preexec,
    )


def launch(
    *,
    role: str,
    vendor: str,
    spec_file: str,
    cwd: str,
    runner: Runner | None = None,
    dry_run: bool = False,
) -> LaneResult:
    if role not in LANE_ROLES:
        raise SystemExit(f"role must be {'|'.join(LANE_ROLES)}")
    if vendor not in LANE_VENDORS:
        raise SystemExit("vendor must be grok|codex")

    path = Path(spec_file)
    if not path.is_file():
        raise SystemExit(f"spec-file not found: {spec_file}")
    spec_text = path.read_text(encoding="utf-8")
    if not spec_text.strip():
        raise SystemExit(f"spec-file is empty: {spec_file}")

    write = role in WRITE_ROLES
    codex_output_file: str | None = None
    if vendor == "grok":
        argv = grok_argv(spec_file=spec_file, cwd=cwd, write=write)
    else:
        if dry_run:
            argv = codex_argv(
                cwd=cwd,
                write=write,
                output_file="/tmp/agent-lane-codex-dry-run.txt",
            )
        else:
            fd, codex_output_file = tempfile.mkstemp(
                prefix="agent-lane-codex-",
                suffix=".txt",
            )
            os.close(fd)
            argv = codex_argv(cwd=cwd, write=write, output_file=codex_output_file)

    if dry_run:
        return LaneResult(
            role=role,
            vendor=vendor,
            status="",
            argv=argv,
            returncode=0,
            stdout="",
            stderr="",
        )

    stdin_text: str | None = spec_text if vendor == "codex" else None
    active = runner if runner is not None else _default_runner
    try:
        completed = active(argv, stdin_text)
        returncode = int(getattr(completed, "returncode"))
        stdout = str(getattr(completed, "stdout") or "")
        stderr = str(getattr(completed, "stderr") or "")

        if codex_output_file is not None:
            try:
                file_text = Path(codex_output_file).read_text(encoding="utf-8")
            except OSError:
                file_text = ""
            if file_text:
                stdout = file_text

        status = parse_status(stdout, returncode)
        return LaneResult(
            role=role,
            vendor=vendor,
            status=status,
            argv=argv,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )
    finally:
        if codex_output_file is not None:
            try:
                os.unlink(codex_output_file)
            except OSError:
                pass
