"""Vendor-lane launcher (argv builders + subprocess or tmux holder)."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .runtime import kill_process_group_and_reap

LANE_ROLES = ("implementer", "reviewer", "pr-reviewer-quality", "pr-reviewer-logic")
LANE_VENDORS = ("grok", "codex")
WRITE_ROLES = frozenset({"implementer"})
GROK_LANE_MODEL = "grok-4.5"
CODEX_LANE_MODEL = "gpt-5.6-sol"
# Wall-clock cap for a single vendor CLI invocation (direct or tmux-held).
# Large codex-pr diffs have been observed near 1500–1800s; keep headroom.
VENDOR_RUN_TIMEOUT_SEC = 1800
GROK_STRIP_ENV = ("ANTHROPIC_API_KEY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")


def is_credential_shaped_env_key(key: str) -> bool:
    """True for env var names carrying secrets: an AGENT_ERROR_FIX_* prefix, or
    AGENT_PG_DSN exactly. Single source of truth for this predicate -- both
    lane.py's vendor-CLI env stripping (_env_strip_prefix, below) and
    run_core.py's local-check env stripping reuse this function instead of
    duplicating the prefix-matching rule in two places."""
    return key.startswith("AGENT_ERROR_FIX_") or key == "AGENT_PG_DSN"


STATUS_VALUES = ("complete", "partial", "timeout", "unavailable")

_STATUS_RE = re.compile(
    r"(?m)^STATUS:[ \t]*(complete|partial|timeout|unavailable)[ \t]*\r?$",
    re.IGNORECASE,
)
# FINDINGS section: header line, then entries until NOT-VERIFIABLE / GAPS /
# a duplicate FINDINGS header, or end of text. STATUS / REASON / SCOPE /
# DIMENSION are not terminators — they may appear as finding text.
_FINDINGS_HEADER_RE = re.compile(r"(?m)^FINDINGS:[ \t]*(.*)$", re.IGNORECASE)
_FINDINGS_TERMINATOR_RE = re.compile(
    r"(?m)^(?:FINDINGS|NOT-VERIFIABLE|GAPS):([ \t]|$)", re.IGNORECASE
)
_ZERO_TOKENS = frozenset({"", "0", "none", "n/a", "-", "—", "–"})


def findings_header_present(text: str) -> bool:
    """True when a FINDINGS: section header is present (parseable report)."""
    return _FINDINGS_HEADER_RE.search(text) is not None


def has_single_terminal_report(text: str) -> bool:
    """True when STATUS: and FINDINGS: each appear exactly once.

    Multiple STATUS or FINDINGS headers (e.g. an early example block plus a
    real report) are unparseable — callers must not trust parse_status /
    count_findings on such transcripts.

    Multiple GAPS: headers are treated the same way (e.g. an early template-echo
    "GAPS: none" in narration followed by a real GAPS: section with genuine disclosed
    content): trusting only the first GAPS: match would let the real disclosure hide
    behind the harmless early one and silently bypass the retry-on-disclosed-gaps check.
    GAPS: itself is allowed to be absent (0 occurrences) — only duplication is rejected.

    Known limitation, accepted by design: this does not attempt to detect a
    real finding stated only in free-text preamble/reasoning narration ahead
    of an otherwise clean STATUS: complete / FINDINGS: none block. Reasoning
    narration before the terminal report is normal, expected model output
    (the review contract says reports must "end with" this block, not
    "consist solely of" it) — rejecting on any preamble text caused
    near-universal false-fail retries on legitimate reports. Distinguishing
    harmless narration from a stated finding is a semantic judgment on
    natural-language text, not a mechanical one; no regex/keyword heuristic
    here would be reliable, so none is attempted (matches this system's
    "model text is never itself a transition" principle, DESIGN.md §19).
    The structured FINDINGS: section remains the sole source of truth for
    pass/fail.
    """
    status_n = len(list(_STATUS_RE.finditer(text)))
    findings_n = len(list(_FINDINGS_HEADER_RE.finditer(text)))
    gaps_n = len(list(_GAPS_HEADER_RE.finditer(text)))
    return status_n == 1 and findings_n == 1 and gaps_n <= 1


def _findings_body_lines(text: str) -> list[str] | None:
    """Return FINDINGS-section body lines, or None when no FINDINGS: header."""
    match = _FINDINGS_HEADER_RE.search(text)
    if match is None:
        return None
    same_line = (match.group(1) or "").strip()
    after = text[match.end() :]
    body_lines: list[str] = []
    if same_line:
        body_lines.append(same_line)
    for line in after.splitlines():
        if _FINDINGS_TERMINATOR_RE.match(line):
            break
        body_lines.append(line)
    return body_lines


def extract_findings_text(text: str) -> str:
    """Return the raw FINDINGS-section body (no bullet stripping), or ''."""
    body_lines = _findings_body_lines(text)
    if body_lines is None:
        return ""
    return "\n".join(body_lines).strip()


def count_findings(text: str) -> int:
    """Count non-empty FINDINGS entries. Empty / 0 / none → 0.

    Absent FINDINGS: header also returns 0; callers that must distinguish
    "explicitly zero" from "unparseable" should use findings_header_present().

    Calibrated to the grok-reviewer / codex-reviewer report contract:
    STATUS / REASON / SCOPE / DIMENSION / FINDINGS / NOT-VERIFIABLE / GAPS.
    """
    body_lines = _findings_body_lines(text)
    if body_lines is None:
        return 0
    entries = 0
    for raw in body_lines:
        stripped = raw.strip()
        if not stripped:
            continue
        # bullet / numbered prefixes
        for prefix in ("- ", "* ", "• "):
            if stripped.startswith(prefix):
                stripped = stripped[len(prefix) :].strip()
                break
        else:
            if len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".)":
                stripped = stripped[2:].strip()
        if stripped.lower() in _ZERO_TOKENS:
            continue
        entries += 1
    return entries


_GAPS_HEADER_RE = re.compile(r"(?m)^GAPS:[ \t]*(.*)$", re.IGNORECASE)


def _gaps_section_body_lines(match: re.Match[str], text: str) -> list[str]:
    """Body lines for one already-located GAPS: header match, mirroring
    _findings_body_lines' same-line + until-terminator logic exactly. A helper
    (rather than only operating on the first match) because gaps_disclosed()
    below must scan every GAPS: header, not just the first."""
    same_line = (match.group(1) or "").strip()
    after = text[match.end() :]
    body_lines: list[str] = []
    if same_line:
        body_lines.append(same_line)
    for line in after.splitlines():
        if _FINDINGS_TERMINATOR_RE.match(line):
            break
        body_lines.append(line)
    return body_lines


def gaps_disclosed(text: str) -> bool:
    """True when ANY GAPS: section in the text has genuine, non-trivial disclosed
    content (not one of _ZERO_TOKENS, and not just bullet/number prefixes around a
    zero token). Scans every GAPS: header via finditer, not just the first via
    search — an early template-echo "GAPS: none" in narration/preamble followed by
    a later, real GAPS: section with actual disclosed content must still be
    detected; checking only the first match let that later disclosure silently
    bypass the retry-on-disclosed-gaps check. Absent GAPS: header returns False.
    Mirrors count_findings' entry-parsing exactly (bullet prefixes "- "/"* "/"• ",
    numbered "1." / "1)")."""
    for match in _GAPS_HEADER_RE.finditer(text):
        body_lines = _gaps_section_body_lines(match, text)
        for raw in body_lines:
            stripped = raw.strip()
            if not stripped:
                continue
            for prefix in ("- ", "* ", "• "):
                if stripped.startswith(prefix):
                    stripped = stripped[len(prefix) :].strip()
                    break
            else:
                if len(stripped) > 2 and stripped[0].isdigit() and stripped[1] in ".)":
                    stripped = stripped[2:].strip()
            if stripped.lower() in _ZERO_TOKENS:
                continue
            return True
    return False


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
    for key in sorted(os.environ):
        if key in GROK_STRIP_ENV:
            continue
        if is_credential_shaped_env_key(key):
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
    matched = matches[-1].group(1).lower() if matches else None
    # Trust embedded STATUS only when it already implies failure, or when the
    # process actually exited 0. A crash/timeout after printing STATUS: complete
    # must not auto-approve.
    if matched in ("timeout", "unavailable"):
        return matched
    if matched is not None and returncode == 0:
        return matched
    if returncode == 124:
        return "timeout"
    if returncode != 0:
        return "unavailable"
    return matched if matched is not None else "partial"


def _default_runner(
    argv: list[str],
    stdin_text: str | None,
    *,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    limit = VENDOR_RUN_TIMEOUT_SEC if timeout is None else timeout
    # start_new_session=True ≡ setsid without post-fork Python (preexec_fn is
    # unsafe in a multithreaded parent). RLIMIT_NPROC was only defense-in-depth.
    proc = subprocess.Popen(  # noqa: S603
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(input=stdin_text, timeout=limit)
    except subprocess.TimeoutExpired:
        stdout, stderr = kill_process_group_and_reap(proc)
        # returncode 124 matches parse_status's external-timeout convention.
        return subprocess.CompletedProcess(argv, 124, stdout, stderr)
    return subprocess.CompletedProcess(
        argv,
        proc.returncode if proc.returncode is not None else 1,
        stdout or "",
        stderr or "",
    )


def _tmux_call(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _run_in_tmux(
    inner: list[str],
    *,
    name: str,
    cwd: str,
    stdin_text: str | None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """Hold the vendor process in tmux, wait for the pane to die, capture output."""
    limit = VENDOR_RUN_TIMEOUT_SEC if timeout is None else timeout
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
    deadline = time.monotonic() + limit
    while True:
        dead = _tmux_call(["tmux", "display-message", "-p", "-t", name, "#{pane_dead}"])
        if dead.returncode != 0:
            _tmux_call(["tmux", "kill-session", "-t", name])
            return subprocess.CompletedProcess(
                wrap, dead.returncode or 1, "", dead.stderr or ""
            )
        if dead.stdout.strip() == "1":
            break
        if time.monotonic() >= deadline:
            _tmux_call(["tmux", "kill-session", "-t", name])
            return subprocess.CompletedProcess(
                wrap, 124, "", "vendor lane timed out"
            )
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
    spec_file = str(path.resolve())
    cwd = str(Path(cwd).resolve())

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

    tmux_session: str | None = None
    if tmux:
        unique = "" if dry_run else uuid.uuid4().hex[:8]
        tmux_session = lane_tmux_name(vendor=vendor, role=role, unique=unique)
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

    stdin_text: str | None = spec_text if vendor == "codex" else None
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
            completed = _default_runner(argv, stdin_text)
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
