"""Parse and verify the DFX local-CI report block in a pull-request comment.

The on-the-wire format is frozen as ``dfx-local-ci/v1``. A comment may contain
exactly one pair of markers. Between them sits one fenced JSON object. The
script never trusts a ``verdict`` field in the payload; it computes pass/fail.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

SCHEMA_ID = "dfx-local-ci/v1"
BEGIN_MARK = "<!-- DFX-LOCAL-CI:v1 -->"
END_MARK = "<!-- /DFX-LOCAL-CI:v1 -->"
FENCE_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
HEAD_RE = re.compile(r"^[0-9a-f]{40}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
RECORDED_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
RESULTS = frozenset({"pass", "fail", "error", "timeout"})
PAYLOAD_KEYS = frozenset({"schema", "repo", "head", "private", "recorded_at", "required", "runs"})
RUN_KEYS = frozenset({"id", "name", "command", "result", "exit_code", "duration_s", "timeout_s"})


class LocalCiError(ValueError):
    """The comment is not a valid v1 local-CI report."""


@dataclass(frozen=True)
class LocalCiRun:
    id: str
    name: str
    command: str
    result: str
    exit_code: int
    duration_s: float
    timeout_s: float


@dataclass(frozen=True)
class LocalCiReport:
    schema: str
    repo: str
    head: str
    private: bool
    recorded_at: str
    required: tuple[str, ...]
    runs: tuple[LocalCiRun, ...]


@dataclass(frozen=True)
class LocalCiVerdict:
    ok: bool
    status: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    report: LocalCiReport | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": self.ok,
            "status": self.status,
            "reasons": list(self.reasons),
        }
        if self.report is not None:
            out["repo"] = self.report.repo
            out["head"] = self.report.head
            out["private"] = self.report.private
            out["required"] = list(self.report.required)
        return out


def extract_json_text(comment: str) -> str:
    begins = list(re.finditer(re.escape(BEGIN_MARK), comment))
    ends = list(re.finditer(re.escape(END_MARK), comment))
    if len(begins) != 1 or len(ends) != 1:
        raise LocalCiError("comment must contain exactly one DFX-LOCAL-CI:v1 marker pair")
    start = begins[0].end()
    stop = ends[0].start()
    if stop < start:
        raise LocalCiError("DFX-LOCAL-CI:v1 end marker precedes begin marker")
    inner = comment[start:stop]
    match = FENCE_RE.search(inner)
    if match is None:
        raise LocalCiError("DFX-LOCAL-CI:v1 block must contain one fenced json object")
    if FENCE_RE.search(inner, match.end()) is not None:
        raise LocalCiError("DFX-LOCAL-CI:v1 block must contain exactly one fenced json object")
    leftover = (inner[: match.start()] + inner[match.end() :]).strip()
    if leftover:
        raise LocalCiError("DFX-LOCAL-CI:v1 block must contain only the fenced json object")
    return match.group(1)


def _require_keys(obj: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    keys = set(obj)
    extra = keys - allowed
    missing = allowed - keys
    if extra:
        raise LocalCiError(f"{label} has unknown keys: {', '.join(sorted(extra))}")
    if missing:
        raise LocalCiError(f"{label} missing keys: {', '.join(sorted(missing))}")


def _as_str(value: Any, label: str) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise LocalCiError(f"{label} must be a non-empty string")
    return value


def _as_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise LocalCiError(f"{label} must be a boolean")
    return value


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise LocalCiError(f"{label} must be an integer")
    return value


def _as_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocalCiError(f"{label} must be a number")
    return float(value)


def parse_payload(raw: Mapping[str, Any]) -> LocalCiReport:
    _require_keys(raw, PAYLOAD_KEYS, "payload")
    schema = _as_str(raw["schema"], "schema")
    if schema != SCHEMA_ID:
        raise LocalCiError(f"schema must be {SCHEMA_ID}")
    repo = _as_str(raw["repo"], "repo")
    if REPO_RE.match(repo) is None:
        raise LocalCiError("repo must be owner/name")
    head = _as_str(raw["head"], "head").lower()
    if HEAD_RE.match(head) is None:
        raise LocalCiError("head must be a 40-character lowercase hex SHA")
    private = _as_bool(raw["private"], "private")
    recorded_at = _as_str(raw["recorded_at"], "recorded_at")
    if RECORDED_RE.match(recorded_at) is None:
        raise LocalCiError("recorded_at must be UTC ISO-8601 YYYY-MM-DDTHH:MM:SSZ")
    required_raw = raw["required"]
    if not isinstance(required_raw, list):
        raise LocalCiError("required must be an array of ids")
    required: list[str] = []
    seen: set[str] = set()
    for item in required_raw:
        ident = _as_str(item, "required id")
        if ID_RE.match(ident) is None:
            raise LocalCiError(f"required id {ident!r} is not kebab-case")
        if ident in seen:
            raise LocalCiError(f"required id {ident!r} is duplicated")
        seen.add(ident)
        required.append(ident)
    runs_raw = raw["runs"]
    if not isinstance(runs_raw, list):
        raise LocalCiError("runs must be an array")
    if bool(required_raw) != bool(runs_raw):
        raise LocalCiError("required and runs must both be empty or both be non-empty")
    runs: list[LocalCiRun] = []
    run_ids: set[str] = set()
    for index, item in enumerate(runs_raw):
        if not isinstance(item, dict):
            raise LocalCiError(f"runs[{index}] must be an object")
        _require_keys(item, RUN_KEYS, f"runs[{index}]")
        ident = _as_str(item["id"], f"runs[{index}].id")
        if ID_RE.match(ident) is None:
            raise LocalCiError(f"runs[{index}].id {ident!r} is not kebab-case")
        if ident not in seen:
            raise LocalCiError(f"runs[{index}].id {ident!r} is not in required")
        if ident in run_ids:
            raise LocalCiError(f"runs id {ident!r} is duplicated")
        run_ids.add(ident)
        result = _as_str(item["result"], f"runs[{index}].result")
        if result not in RESULTS:
            raise LocalCiError(f"runs[{index}].result must be pass|fail|error|timeout")
        duration_s = _as_number(item["duration_s"], f"runs[{index}].duration_s")
        timeout_s = _as_number(item["timeout_s"], f"runs[{index}].timeout_s")
        if duration_s < 0:
            raise LocalCiError(f"runs[{index}].duration_s must be >= 0")
        if timeout_s <= 0:
            raise LocalCiError(f"runs[{index}].timeout_s must be > 0")
        runs.append(
            LocalCiRun(
                id=ident,
                name=_as_str(item["name"], f"runs[{index}].name"),
                command=_as_str(item["command"], f"runs[{index}].command"),
                result=result,
                exit_code=_as_int(item["exit_code"], f"runs[{index}].exit_code"),
                duration_s=duration_s,
                timeout_s=timeout_s,
            )
        )
    missing_runs = sorted(seen - run_ids)
    if missing_runs:
        raise LocalCiError("runs missing required ids: " + ",".join(missing_runs))
    return LocalCiReport(
        schema=schema,
        repo=repo,
        head=head,
        private=private,
        recorded_at=recorded_at,
        required=tuple(required),
        runs=tuple(runs),
    )


def parse_comment(comment: str) -> LocalCiReport:
    text = extract_json_text(comment)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise LocalCiError(f"JSON is invalid: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise LocalCiError("payload must be a JSON object")
    return parse_payload(payload)


def evaluate(report: LocalCiReport, *, require_ids: frozenset[str] | None = None) -> LocalCiVerdict:
    if not report.private:
        return LocalCiVerdict(ok=True, status="not_applicable", reasons=("private is false",), report=report)
    reasons: list[str] = []
    required = set(report.required)
    if require_ids is not None:
        if required != set(require_ids):
            missing = sorted(set(require_ids) - required)
            extra = sorted(required - set(require_ids))
            if missing:
                reasons.append("required missing ids: " + ",".join(missing))
            if extra:
                reasons.append("required extra ids: " + ",".join(extra))
    by_id = {run.id: run for run in report.runs}
    for ident in report.required:
        run = by_id.get(ident)
        if run is None:
            reasons.append(f"{ident}: missing run")
            continue
        if run.result != "pass":
            reasons.append(f"{ident}: result is {run.result}")
        if run.exit_code != 0:
            reasons.append(f"{ident}: exit_code is {run.exit_code}")
        if run.duration_s > run.timeout_s:
            reasons.append(f"{ident}: duration_s {run.duration_s} exceeds timeout_s {run.timeout_s}")
    if reasons:
        return LocalCiVerdict(ok=False, status="fail", reasons=tuple(reasons), report=report)
    return LocalCiVerdict(ok=True, status="pass", report=report)


def verify_comment(comment: str, *, require_ids: frozenset[str] | None = None) -> LocalCiVerdict:
    report = parse_comment(comment)
    return evaluate(report, require_ids=require_ids)


def render_block(report: LocalCiReport) -> str:
    payload = {
        "schema": report.schema,
        "repo": report.repo,
        "head": report.head,
        "private": report.private,
        "recorded_at": report.recorded_at,
        "required": list(report.required),
        "runs": [
            {
                "id": run.id,
                "name": run.name,
                "command": run.command,
                "result": run.result,
                "exit_code": run.exit_code,
                "duration_s": run.duration_s,
                "timeout_s": run.timeout_s,
            }
            for run in report.runs
        ],
    }
    body = json.dumps(payload, indent=2, sort_keys=True)
    return f"{BEGIN_MARK}\n```json\n{body}\n```\n{END_MARK}\n"
