"""Vendor-lane launcher (argv builders + subprocess or tmux holder)."""

from __future__ import annotations

import os
import re
import resource
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .model_slots import PROVIDER_CHOICES, SUPPORTED_PROVIDERS

LANE_ROLES = ("implementer", "reviewer", "pr-reviewer-quality", "pr-reviewer-logic")
LANE_VENDORS = tuple(sorted(SUPPORTED_PROVIDERS))
# Providers that read the spec from stdin instead of a --prompt-file argument.
SPEC_VIA_STDIN = frozenset({"codex", "claude"})
# Providers whose final message is collected from an output file.
OUTPUT_FILE_VENDORS = frozenset({"codex"})
NPROC_CAP = 800
# Stripped from every lane: a lane runs on its own provider login, never on the
# orchestrator's ambient API key or Claude Code session markers.
GROK_STRIP_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
)
_CLAUDE_READ_TOOLS = ("Read", "Grep", "Glob")
_CLAUDE_WRITE_TOOLS = ("Edit", "Write", "NotebookEdit")
# Never for a lane: executing software, spawning subagents, reaching the web.
_CLAUDE_FORBIDDEN_TOOLS = ("Bash", "Agent", "Task", "WebSearch", "WebFetch")
STATUS_VALUES = ("complete", "partial", "timeout", "unavailable")

_STATUS_RE = re.compile(
    r"(?m)^STATUS:[ \t]*(complete|partial|timeout|unavailable)[ \t]*\r?$",
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
    tmux_session: str | None = None


# runner(argv, stdin_text) -> object with returncode, stdout, stderr
Runner = Callable[[list[str], str | None], object]


def _env_strip_prefix() -> list[str]:
    argv = ["env"]
    for key in GROK_STRIP_ENV:
        argv.extend(["-u", key])
    return argv


def grok_argv(*, spec_file: str, cwd: str, write: bool, model: str) -> list[str]:
    argv = _env_strip_prefix()
    argv.extend(["grok", "--prompt-file", spec_file, "-m", model])
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


def codex_argv(*, cwd: str, write: bool, output_file: str, model: str) -> list[str]:
    sandbox = "workspace-write" if write else "read-only"
    argv = _env_strip_prefix()
    argv.extend(
        [
            "codex",
            "exec",
            "--model",
            model,
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


def claude_argv(*, cwd: str, write: bool, model: str) -> list[str]:
    """Headless Claude Code lane: ``claude --print`` with the spec on stdin."""
    argv = _env_strip_prefix()
    argv.extend(
        [
            "claude",
            "--print",
            "--model",
            model,
            "--output-format",
            "text",
            "--add-dir",
            cwd,
            # Only the user's own settings: a worktree's .claude/settings.json
            # (hooks run shell commands) and .mcp.json must not reach a lane.
            "--setting-sources",
            "user",
            "--strict-mcp-config",
        ]
    )
    if write:
        argv.extend(["--permission-mode", "acceptEdits"])
        argv.extend(["--allowedTools", *_CLAUDE_READ_TOOLS, *_CLAUDE_WRITE_TOOLS])
        argv.extend(["--disallowedTools", *_CLAUDE_FORBIDDEN_TOOLS])
    else:
        # Explicit mode: a user-level permissions.defaultMode (e.g. bypassPermissions)
        # must not reach a read-only lane.
        argv.extend(["--permission-mode", "default"])
        argv.extend(["--allowedTools", *_CLAUDE_READ_TOOLS])
        argv.extend(["--disallowedTools", *_CLAUDE_WRITE_TOOLS, *_CLAUDE_FORBIDDEN_TOOLS])
    return argv


def provider_argv(
    vendor: str,
    *,
    spec_file: str,
    cwd: str,
    write: bool,
    model: str,
    output_file: str | None = None,
) -> list[str]:
    """Build the lane argv for one supported provider.

    ``output_file`` is required for providers in ``OUTPUT_FILE_VENDORS``.
    """
    if vendor == "grok":
        return grok_argv(spec_file=spec_file, cwd=cwd, write=write, model=model)
    if vendor == "codex":
        if not output_file:
            raise SystemExit("codex lane requires an output file")
        return codex_argv(cwd=cwd, write=write, output_file=output_file, model=model)
    if vendor == "claude":
        return claude_argv(cwd=cwd, write=write, model=model)
    raise SystemExit(f"vendor must be {PROVIDER_CHOICES}")


def lane_tmux_name(*, vendor: str, role: str, unique: str = "") -> str:
    base = f"agent-lane-{vendor}-{role}"
    if unique:
        base = f"{base}-{unique}"
    cleaned = re.sub(r"[^A-Za-z0-9_-]", "-", base)[:50]
    if cleaned == "":
        raise SystemExit("lane tmux name is empty")
    return cleaned


def tmux_wrap_argv(inner: list[str], *, name: str, cwd: str) -> list[str]:
    return ["tmux", "new-session", "-d", "-s", name, "-c", cwd, "--", *inner]


def parse_status(output: str, returncode: int) -> str:
    matches = list(_STATUS_RE.finditer(output))
    if matches:
        return matches[-1].group(1).lower()
    if returncode == 124:
        return "timeout"
    if returncode != 0:
        return "unavailable"
    return "partial"


def _default_runner(
    argv: list[str], stdin_text: str | None, cwd: str | None = None
) -> subprocess.CompletedProcess[str]:
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
        cwd=cwd,
        preexec_fn=_preexec,
    )


def _tmux_call(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _run_in_tmux(
    inner: list[str],
    *,
    name: str,
    cwd: str,
    stdin_text: str | None,
) -> subprocess.CompletedProcess[str]:
    """Hold the vendor process in tmux, wait for the pane to die, capture output."""
    wrap = tmux_wrap_argv(inner, name=name, cwd=cwd)
    created = _tmux_call(wrap)
    if created.returncode != 0:
        return created
    remain = _tmux_call(["tmux", "set-option", "-t", name, "remain-on-exit", "on"])
    if remain.returncode != 0:
        _tmux_call(["tmux", "kill-session", "-t", name])
        return remain
    if stdin_text:
        payload = stdin_text if stdin_text.endswith("\n") else stdin_text + "\n"
        typed = _tmux_call(["tmux", "send-keys", "-t", name, "-l", "--", payload])
        if typed.returncode != 0:
            _tmux_call(["tmux", "kill-session", "-t", name])
            return typed
        eof = _tmux_call(["tmux", "send-keys", "-t", name, "C-d"])
        if eof.returncode != 0:
            _tmux_call(["tmux", "kill-session", "-t", name])
            return eof
    while True:
        dead = _tmux_call(["tmux", "display-message", "-p", "-t", name, "#{pane_dead}"])
        if dead.returncode != 0:
            _tmux_call(["tmux", "kill-session", "-t", name])
            return subprocess.CompletedProcess(
                wrap, dead.returncode or 1, "", dead.stderr or ""
            )
        if dead.stdout.strip() == "1":
            break
        time.sleep(0.2)
    status = _tmux_call(["tmux", "display-message", "-p", "-t", name, "#{pane_dead_status}"])
    returncode = 1
    if status.returncode == 0:
        raw = status.stdout.strip()
        if raw.isdigit():
            returncode = int(raw)
    captured = _tmux_call(["tmux", "capture-pane", "-t", name, "-p", "-S", "-"])
    _tmux_call(["tmux", "kill-session", "-t", name])
    return subprocess.CompletedProcess(
        wrap,
        returncode,
        captured.stdout or "",
        captured.stderr or "",
    )


def launch(
    *,
    role: str,
    vendor: str,
    spec_file: str,
    cwd: str,
    runner: Runner | None = None,
    dry_run: bool = False,
    tmux: bool = True,
    config_home: Path | None = None,
    session_id: str | None = None,
) -> LaneResult:
    if role not in LANE_ROLES:
        raise SystemExit(f"role must be {'|'.join(LANE_ROLES)}")
    if vendor not in LANE_VENDORS:
        raise SystemExit(f"vendor must be {PROVIDER_CHOICES}")
    from .ai_accounts import load_ai_accounts
    if not session_id or config_home is None:
        raise SystemExit("lane requires an explicit session and AI configuration home")
    selected = load_ai_accounts(config_home).for_lane(session_id, role, vendor)

    path = Path(spec_file)
    if not path.is_file():
        raise SystemExit(f"spec-file not found: {spec_file}")
    spec_text = path.read_text(encoding="utf-8")
    if not spec_text.strip():
        raise SystemExit(f"spec-file is empty: {spec_file}")
    spec_file = str(path.resolve())
    cwd = str(Path(cwd).resolve())

    write = selected.access == "workspace-write"
    codex_output_file: str | None = None
    output_file: str | None = None
    if vendor in OUTPUT_FILE_VENDORS:
        if dry_run:
            output_file = f"/tmp/agent-lane-{vendor}-dry-run.txt"
        else:
            fd, codex_output_file = tempfile.mkstemp(
                prefix=f"agent-lane-{vendor}-",
                suffix=".txt",
            )
            os.close(fd)
            output_file = codex_output_file
    argv = provider_argv(
        vendor,
        spec_file=spec_file,
        cwd=cwd,
        write=write,
        model=selected.model,
        output_file=output_file,
    )

    # Apply the selected provider home inside the tmux child too. The builders'
    # credential-clearing prefix is nested, never a process-global mutation.
    argv = [*selected.env_prefix(), *argv]

    tmux_session: str | None = None
    claude_redirect = False
    if tmux:
        unique = "" if dry_run else uuid.uuid4().hex[:8]
        tmux_session = lane_tmux_name(vendor=vendor, role=role, unique=unique)
        if vendor == "claude":
            # claude --print reads the prompt from stdin only when stdin is not a
            # TTY; in a tmux pane it is. Redirect the spec file instead of typing it.
            argv = ["sh", "-c", 'f=$1; shift; exec "$@" < "$f"', "sh", spec_file, *argv]
            claude_redirect = True
        argv = tmux_wrap_argv(argv, name=tmux_session, cwd=cwd)

    if dry_run:
        return LaneResult(
            role=role,
            vendor=vendor,
            status="",
            argv=argv,
            returncode=0,
            stdout="",
            stderr="",
            tmux_session=tmux_session,
        )

    stdin_text: str | None = spec_text if vendor in SPEC_VIA_STDIN else None
    if claude_redirect:
        stdin_text = None
    try:
        if runner is not None:
            completed = runner(argv, None if tmux else stdin_text)
        elif tmux:
            if "--" not in argv:
                raise SystemExit("tmux argv missing command separator")
            if tmux_session is None:
                raise SystemExit("tmux session name missing")
            inner = argv[argv.index("--") + 1 :]
            completed = _run_in_tmux(
                inner, name=tmux_session, cwd=cwd, stdin_text=stdin_text
            )
        else:
            completed = _default_runner(argv, stdin_text, cwd)
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
            tmux_session=tmux_session,
        )
    finally:
        if codex_output_file is not None:
            try:
                os.unlink(codex_output_file)
            except OSError:
                pass
