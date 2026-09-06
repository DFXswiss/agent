"""Generic argv-list command adapter with failure-only and advisory steps."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

from .common import (
    COMMON_KEYS,
    DIAGNOSTIC_TIMEOUT_S,
    JobError,
    JobRuntime,
    CommonConfig,
    expand_argv,
    expand_placeholders,
    loads_strict_json,
    parse_common_config,
    reject_unknown_keys,
    require_argv,
    require_mapping,
    require_str,
    resolve_artifacts_path,
    run_lifecycle,
    validate_argv_placeholders,
    validate_placeholders,
)

COMMANDS_KEYS = COMMON_KEYS | frozenset({"steps", "failure_steps", "advisory_steps"})


def _parse_step(raw: Any, label: str) -> dict[str, Any]:
    if isinstance(raw, list):
        argv = require_argv(raw, label)
        validate_argv_placeholders(argv, label=label)
        return {"argv": argv, "stdout": None}
    obj = require_mapping(raw, label)
    reject_unknown_keys(obj, frozenset({"argv", "stdout"}), label)
    if "argv" not in obj:
        raise JobError(f"{label} requires argv")
    argv = require_argv(obj["argv"], f"{label}.argv")
    validate_argv_placeholders(argv, label=f"{label}.argv")
    stdout = None
    if "stdout" in obj:
        stdout = require_str(obj["stdout"], f"{label}.stdout")
        validate_placeholders(stdout, label=f"{label}.stdout")
    return {"argv": argv, "stdout": stdout}


def parse_commands_config(text: str) -> tuple[CommonConfig, dict[str, Any]]:
    raw = loads_strict_json(text)
    obj = require_mapping(raw, "config")
    reject_unknown_keys(obj, COMMANDS_KEYS, "commands config")
    if "steps" not in obj:
        raise JobError("commands config requires steps")
    if not isinstance(obj["steps"], list) or not obj["steps"]:
        raise JobError("steps must be a non-empty array")
    steps = [_parse_step(item, f"steps[{index}]") for index, item in enumerate(obj["steps"])]
    failure_steps = []
    if "failure_steps" in obj:
        if not isinstance(obj["failure_steps"], list):
            raise JobError("failure_steps must be an array")
        failure_steps = [
            _parse_step(item, f"failure_steps[{index}]")
            for index, item in enumerate(obj["failure_steps"])
        ]
    advisory_steps = []
    if "advisory_steps" in obj:
        if not isinstance(obj["advisory_steps"], list):
            raise JobError("advisory_steps must be an array")
        advisory_steps = [
            _parse_step(item, f"advisory_steps[{index}]")
            for index, item in enumerate(obj["advisory_steps"])
        ]
    common = parse_common_config(obj, allow_companion=False)
    return common, {"steps": steps, "failure_steps": failure_steps, "advisory_steps": advisory_steps}


def _run_step(
    runtime: JobRuntime,
    step: Mapping[str, Any],
    *,
    warn_only: bool = False,
    timeout_s: float | None = None,
) -> int:
    try:
        argv = expand_argv(step["argv"], mapping=runtime.mapping, images=runtime.images)
        stdout_handle = None
        try:
            if step.get("stdout"):
                expanded = expand_placeholders(
                    step["stdout"], mapping=runtime.mapping, images=runtime.images
                )
                assert runtime.artifacts is not None
                stdout_path = resolve_artifacts_path(expanded, runtime.artifacts)
                stdout_path.parent.mkdir(parents=True, exist_ok=True)
                stdout_handle = stdout_path.open("w", encoding="utf-8")
                if timeout_s is not None:
                    completed = runtime.bounded(
                        timeout_s, argv, stdout=stdout_handle, stderr=sys.stderr
                    )
                else:
                    completed = runtime.run_argv(
                        argv, stdout=stdout_handle, stderr=sys.stderr, check=False
                    )
            else:
                if timeout_s is not None:
                    completed = runtime.bounded(
                        timeout_s, argv, stdout=sys.stdout, stderr=sys.stderr
                    )
                else:
                    completed = runtime.run_argv(
                        argv, stdout=sys.stdout, stderr=sys.stderr, check=False
                    )
        finally:
            if stdout_handle is not None:
                stdout_handle.close()
    except OSError as exc:
        if warn_only:
            print(
                f"a38: warning: advisory step OSError: {exc}; inspect the artifacts",
                file=sys.stderr,
            )
            return 0
        print(f"a38: step OSError: {exc}", file=sys.stderr)
        return 1
    except JobError as exc:
        if warn_only:
            print(
                f"a38: warning: advisory step failed: {exc}; inspect the artifacts",
                file=sys.stderr,
            )
            return 0
        print(f"a38: {exc}", file=sys.stderr)
        return 1
    code = completed.returncode
    if code != 0 and warn_only:
        print(
            f"a38: warning: advisory step failed with exit {code}; inspect the artifacts",
            file=sys.stderr,
        )
        return 0
    return code


def _body(runtime: JobRuntime, parsed: Mapping[str, Any]) -> int:
    def cleanup(original: int) -> int:
        # Interruption always skips failure/advisory diagnostics.
        if runtime.interrupted:
            return 0
        failed = 0
        if original != 0:
            for step in parsed["failure_steps"]:
                if _run_step(runtime, step, timeout_s=DIAGNOSTIC_TIMEOUT_S) != 0:
                    failed = 1
        for step in parsed["advisory_steps"]:
            # Advisory exits/exceptions are warn-only; never replace primary nonzero.
            _run_step(runtime, step, warn_only=True, timeout_s=DIAGNOSTIC_TIMEOUT_S)
        return failed

    # Install before the first step so failures/exceptions still run diagnostics.
    runtime.set_job_cleanup(cleanup)

    primary = 0
    try:
        for step in parsed["steps"]:
            code = _run_step(runtime, step)
            if code != 0:
                primary = code
                break
    except OSError as exc:
        print(f"a38: OSError: {exc}", file=sys.stderr)
        primary = 1
    return primary


def run_commands(
    config_text: str,
    *,
    cwd: Path | None = None,
    lock_root: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    # Strict nested validation before any npm/Docker/temp mutation in run_lifecycle.
    common, parsed = parse_commands_config(config_text)
    texts: list[str] = list(common.env.values())
    for group in (parsed["steps"], parsed["failure_steps"], parsed["advisory_steps"]):
        for step in group:
            texts.extend(step["argv"])
            if step.get("stdout"):
                texts.append(step["stdout"])

    def body(runtime: JobRuntime) -> int:
        runtime.ensure_image_placeholders(texts)
        runtime.refresh_configured_env()
        return _body(runtime, parsed)

    return run_lifecycle(
        adapter="commands",
        common=common,
        body=body,
        cwd=cwd,
        lock_root=lock_root,
        environ=environ,
        allow_companion=False,
    )
