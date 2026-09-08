"""Script-owned bounded source lanes; no native argv or tmux fallback."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

LANE_ROLES = ("implementer", "reviewer", "pr-reviewer-quality", "pr-reviewer-logic")
LANE_VENDORS = ("grok", "codex")
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


def parse_status(output: str, returncode: int) -> str:
    matches = list(_STATUS_RE.finditer(output))
    if matches:
        return matches[-1].group(1).lower()
    if returncode == 124:
        return "timeout"
    if returncode != 0:
        return "unavailable"
    return "partial"


def parse_lane_status(role: str, output: str, returncode: int) -> str:
    """Respect the implementation RESULT and independent review VERDICT contracts."""
    from .coordinator_common import parse_model_result
    if role == "implementer":
        status, result = parse_model_result(output, returncode)
        return "partial" if status == "complete" and result != "done" else status
    if returncode:
        return "timeout" if returncode == 124 else "unavailable"
    if len(re.findall(r"(?im)^STATUS:.*$", output)) != 1:
        return "partial"
    statuses = _STATUS_RE.findall(output)
    if len(statuses) != 1:
        return "partial"
    status = statuses[0].lower()
    if status != "complete":
        return status
    if len(re.findall(r"(?im)^(?:RESULT|VERDICT):.*$", output)) != 1:
        return "partial"
    verdicts = re.findall(r"(?m)^(?:RESULT|VERDICT):[ \t]*(approved|rejected)[ \t]*\r?$",
                          output, re.IGNORECASE)
    return "complete" if len(verdicts) == 1 else "partial"


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
        raise SystemExit("vendor must be grok|codex")
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

    from .lane_executor import execute
    from .lane_protocol import ProtocolError
    if selected.account.lane_runtime is None:
        raise ProtocolError("AI account lane_runtime is unconfigured")
    if runner is not None:
        raise ProtocolError("legacy argv lane runners are unsupported; use the bounded source executor")
    if dry_run:
        import json
        return LaneResult(role, vendor, "", [], 0, json.dumps({
            "executor": "bounded-source", "account": selected.account.name,
            "model": selected.model, "access": selected.access,
            "runtime": selected.account.lane_runtime.binary,
            "sha256": selected.account.lane_runtime.sha256,
            "spec_file": spec_file, "cwd": cwd,
        }), "")
    from .lane_workspace import local_manifest
    completed = execute(selected, cwd=cwd, manifest=local_manifest(cwd), spec=spec_text, timeout=1800)
    status = parse_lane_status(role, completed.stdout, completed.returncode)
    return LaneResult(role, vendor, status, [], completed.returncode,
                      completed.stdout, completed.stderr)
