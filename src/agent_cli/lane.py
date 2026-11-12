"""Vendor-lane launcher (argv builders + subprocess or tmux holder)."""

from __future__ import annotations

import json
import os
import re
import resource
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING

from .model_slots import PROVIDER_CHOICES, SUPPORTED_PROVIDERS
from .text_sanitize import (
    redact_terminal_interleaved_secret,
    sanitize_terminal_text,
)

if TYPE_CHECKING:
    from .ai_accounts import AIRole

LANE_ROLES = ("implementer", "reviewer", "pr-reviewer-quality", "pr-reviewer-logic")
LANE_VENDORS = tuple(sorted(SUPPORTED_PROVIDERS))
# Providers that read the spec from stdin instead of a --prompt-file argument.
SPEC_VIA_STDIN = frozenset({"codex", "claude", "openrouter"})
# Providers whose final message is collected from an output file.
OUTPUT_FILE_VENDORS = frozenset({"codex", "openrouter"})
OPENROUTER_BASE_URL = "https://openrouter.ai/api"
NPROC_CAP = 800
LANE_MODEL_LIMIT = 200
LANE_TOOL_LIMIT = 200
LANE_USAGE_VALUE_LIMIT = 1_000_000_000_000_000
LANE_GITHUB_TOKEN_ENV = (
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
)
# Stripped from every lane and every lane pane: a lane runs on its own provider
# login, never on the orchestrator's ambient Claude credentials, its API route
# (gateway, Bedrock, Vertex, Foundry), Claude Code session markers, or ambient
# proxy/CA overrides (an intercepting proxy must never see a lane's credentials).
LANE_STRIP_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "OPENROUTER_API_KEY",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
    "ANTHROPIC_FOUNDRY_API_KEY",
    "ANTHROPIC_FOUNDRY_BASE_URL",
    "ANTHROPIC_FOUNDRY_RESOURCE",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "HTTP_PROXY",
    "http_proxy",
    "HTTPS_PROXY",
    "https_proxy",
    "ALL_PROXY",
    "all_proxy",
    "NO_PROXY",
    "no_proxy",
    "NODE_EXTRA_CA_CERTS",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)
_CLAUDE_READ_TOOLS = ("Read", "Grep", "Glob")
_CLAUDE_WRITE_TOOLS = ("Edit", "Write")


@dataclass(frozen=True)
class LaneUsage:
    model: str | None
    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    num_turns: int | None
    duration_ms: int | None


@dataclass(frozen=True)
class LaneOutput:
    text: str
    usage: LaneUsage | None
    tools: tuple[dict, ...]
    tools_truncated: bool
    source_text: str = field(default="", repr=False, compare=False)


@dataclass
class LaneResult:
    role: str
    vendor: str
    model: str
    status: str
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str
    tmux_session: str | None = None
    config_dir: str | None = field(default=None, repr=False)
    usage: LaneUsage | None = None
    tools: tuple[dict, ...] = ()
    tools_truncated: bool = False
    activity_error: str | None = None
    # Parsed exactly once from the unredacted final lane text. Persisted text
    # must never be reparsed because redaction can remove hidden duplicate
    # STATUS/RESULT records.
    protocol_result: str = ""


# runner(argv, stdin_text) -> object with returncode, stdout, stderr
Runner = Callable[[list[str], str | None], object]


@contextmanager
def sanitized_lane_argv(argv: list[str]):
    """Yield lane argv with no ambient GitHub login available to the child."""
    with tempfile.TemporaryDirectory(prefix="agent-lane-empty-gh-") as profile:
        prefix = ["env"]
        for key in LANE_GITHUB_TOKEN_ENV:
            prefix.extend(["-u", key])
        prefix.append(f"GH_CONFIG_DIR={profile}")
        yield [*prefix, *argv]


def _env_strip_prefix() -> list[str]:
    argv = ["env"]
    for key in LANE_STRIP_ENV:
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


def claude_argv(
    *, cwd: str, write: bool, model: str, read_dirs: tuple[str, ...] = ()
) -> list[str]:
    """Restricted Claude Code lane with only explicit file tools."""
    if write and read_dirs:
        raise SystemExit("write lane takes no extra directories")
    argv = _env_strip_prefix()
    argv.extend(
        [
            "claude",
            "--print",
            "--verbose",
            "--output-format",
            "stream-json",
            "--model",
            model,
            # Restricted mode ignores settings and confines file tools to the
            # process cwd plus explicitly supplied read-only directories.
            "--restricted",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--tools",
            ",".join((*_CLAUDE_READ_TOOLS, *_CLAUDE_WRITE_TOOLS) if write else _CLAUDE_READ_TOOLS),
            "--permission-mode",
            "acceptEdits" if write else "default",
        ]
    )
    for directory in read_dirs:
        argv.extend(["--add-dir", directory])
    return argv


_OPENROUTER_WRAPPER = (
    'out=$1; shift; key_file=${CLAUDE_CONFIG_DIR:+$CLAUDE_CONFIG_DIR/openrouter-api-key}; '
    'if [ -z "$key_file" ] || [ ! -r "$key_file" ]; then '
    "printf '%s\\n' 'STATUS: unavailable' 'openrouter key missing' >&2; exit 2; fi; "
    'key=$(cat "$key_file" 2>/dev/null) || { '
    "printf '%s\\n' 'STATUS: unavailable' 'openrouter key missing' >&2; exit 2; }; "
    'if [ -z "$key" ]; then '
    "printf '%s\\n' 'STATUS: unavailable' 'openrouter key missing' >&2; exit 2; fi; "
    'export ANTHROPIC_AUTH_TOKEN="$key"; exec "$@" > "$out"'
)


def openrouter_argv(
    *,
    cwd: str,
    write: bool,
    model: str,
    output_file: str,
    read_dirs: tuple[str, ...] = (),
) -> list[str]:
    claude = claude_argv(cwd=cwd, write=write, model=model, read_dirs=read_dirs)
    inner = claude[len(_env_strip_prefix()) :]
    return [
        *_env_strip_prefix(),
        f"ANTHROPIC_BASE_URL={OPENROUTER_BASE_URL}",
        "ANTHROPIC_API_KEY=",
        "sh",
        "-c",
        _OPENROUTER_WRAPPER,
        "sh",
        output_file,
        *inner,
    ]


def replace_path_roots(
    text: str, replacements: tuple[tuple[str, str], ...]
) -> str:
    """Replace raw/resolved local roots despite interleaved terminal controls."""
    variants: list[tuple[str, str]] = []
    for value, replacement in replacements:
        if not value or value in ("/", "."):
            continue
        raw = str(Path(value).expanduser())
        try:
            resolved = str(Path(raw).resolve())
        except (OSError, RuntimeError, ValueError):
            resolved = raw
        variants.extend((path, replacement) for path in {raw, resolved})
    for path, replacement in sorted(
        variants, key=lambda item: len(item[0]), reverse=True
    ):
        text = redact_terminal_interleaved_secret(
            text, path, replacement=replacement
        )
    return text


def hide_config_dir(text: str, config_dir: str | None) -> str:
    """Replace raw/resolved account paths despite interleaved terminal controls."""
    return replace_path_roots(
        text,
        ((config_dir, "<config_dir>"),) if config_dir else (),
    )


def openrouter_path_error(
    config_dir: str, cwd: str, read_dirs: tuple[str, ...] = ()
) -> str | None:
    """Return an error when restricted lane roots overlap the key directory."""
    config = Path(config_dir).expanduser().resolve()
    for label, raw in (("worktree", cwd), *(("read directory", d) for d in read_dirs)):
        path = Path(raw).expanduser().resolve()
        try:
            path.relative_to(config)
            return f"openrouter {label} overlaps account config_dir"
        except ValueError:
            pass
        try:
            config.relative_to(path)
            return f"openrouter account config_dir overlaps {label}"
        except ValueError:
            pass
    return None


def provider_argv(
    vendor: str,
    *,
    spec_file: str,
    cwd: str,
    write: bool,
    model: str,
    output_file: str | None = None,
    read_dirs: tuple[str, ...] = (),
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
        return claude_argv(cwd=cwd, write=write, model=model, read_dirs=read_dirs)
    if vendor == "openrouter":
        if not output_file:
            raise SystemExit("openrouter lane requires an output file")
        return openrouter_argv(
            cwd=cwd,
            write=write,
            model=model,
            output_file=output_file,
            read_dirs=read_dirs,
        )
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


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


_REGEX_ESCAPE_RE = re.compile(r"\\([^A-Za-z0-9])")
_ENCODED_ESCAPE_RE = re.compile(r"\\(?:x|u|U|N\{|[0-7]|c)")
_PERCENT_ESCAPE_RE = re.compile(r"%[0-9A-Fa-f]{2}")
_PLAIN_PATH_REGEX_META_RE = re.compile(r"[\^$+{}|()]")
_DRIVE_PREFIX_RE = re.compile(r"(?:^|(?<=[^A-Za-z0-9._-]))[A-Za-z]:")
_ABSOLUTE_FRAGMENT_RE = re.compile(r"(?:^|(?<=[^A-Za-z0-9._-]))/")
_CONTROL_BYTE_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _normalize_tool_target(value: str) -> str:
    """Normalize terminal text, regex escapes and platform separators once."""
    sanitized = sanitize_terminal_text(value)
    regex_unescaped = _REGEX_ESCAPE_RE.sub(r"\1", sanitized)
    return regex_unescaped.replace("\\", "/")


def _contains_disallowed_path_fragment(value: str) -> bool:
    """Apply the one fail-closed traversal and host-path rule."""
    return (
        ".." in value
        or "~" in value
        or _DRIVE_PREFIX_RE.search(value) is not None
        or _ABSOLUTE_FRAGMENT_RE.search(value) is not None
    )


def _plain_inside_worktree_target(value: str, root: Path) -> str | None:
    """Relativize only a whole, plain absolute target inside the worktree."""
    normalized = _normalize_tool_target(value)
    root_text = root.as_posix().rstrip("/")
    if not normalized.startswith(root_text + "/"):
        return None
    if re.search(r"\s", normalized) or _PLAIN_PATH_REGEX_META_RE.search(normalized):
        return None
    remainder = normalized[len(root_text) + 1 :]
    if not remainder or _contains_disallowed_path_fragment(remainder):
        return None
    return remainder


def _tool_target(target: object, cwd: str, kind: str = "path") -> str:
    """Keep worktree paths useful while retaining redaction outside it."""
    from .coordinator_common import redact

    raw = str(target)
    if _CONTROL_BYTE_RE.search(raw) is not None:
        return "[redacted]"
    if raw == "":
        return ""
    if kind not in ("file_path", "path", "pattern"):
        return redact(raw, limit=300)
    if _PERCENT_ESCAPE_RE.search(raw) is not None:
        return "[redacted]"
    if _ENCODED_ESCAPE_RE.search(raw) is not None:
        return "[redacted]"
    try:
        root = Path(cwd).expanduser().resolve()
    except (OSError, RuntimeError, ValueError):
        return "[redacted]"
    inside = _plain_inside_worktree_target(raw, root)
    if inside is not None:
        return redact(inside, limit=300)
    normalized = _normalize_tool_target(raw)
    if _contains_disallowed_path_fragment(normalized):
        return "[redacted]"
    if kind in ("file_path", "path"):
        try:
            path = Path(normalized).expanduser()
            resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
        except (OSError, RuntimeError, ValueError):
            return "[redacted]"
        try:
            normalized = str(resolved.relative_to(root))
        except ValueError:
            # Any path outside the worktree is a host detail, whatever its prefix.
            return "[redacted]"
    return redact(normalized, limit=300)


DIAGNOSTIC_LIMIT = 4000


def redact_lane_text(
    text: str, *, config_dir: str | None = None, limit: int | None
) -> str:
    """Hide config paths and secrets before and after terminal sanitization."""
    from .coordinator_common import (
        _apply_redaction_bound,
        _prebound_redaction_input,
        redact,
    )

    bounded, source_was_cut = _prebound_redaction_input(text or "", limit)
    safe = hide_config_dir(bounded, config_dir)
    safe = redact(safe, limit=None)
    safe = hide_config_dir(safe, config_dir)
    safe = redact(safe, limit=None)
    return _apply_redaction_bound(
        safe,
        limit,
        source_was_cut=source_was_cut,
    )


def redact_diagnostic(text: str, *, config_dir: str | None = None) -> str:
    """Bounded, shared-redacted, config-dir-hidden lane diagnostic text."""
    return redact_lane_text(
        text, config_dir=config_dir, limit=DIAGNOSTIC_LIMIT
    )


def output_read_failure(exc: BaseException) -> str:
    """Bounded, redacted diagnostic for an unreadable lane output file."""
    if isinstance(exc, UnicodeDecodeError):
        return "lane output file is not valid UTF-8"
    return "lane output file is unavailable"


def unreadable_output_result(
    *,
    role: str,
    vendor: str,
    model: str,
    argv: list[str],
    returncode: int,
    stderr: str,
    exc: BaseException,
    config_dir: str | None,
    tmux_session: str | None = None,
) -> LaneResult:
    """Unavailable result for a lane whose output file cannot be read or decoded."""
    message = output_read_failure(exc)
    diagnostic = f"{stderr.rstrip()}\n{message}" if stderr.strip() else message
    # Provider stderr can carry credentials; the failure diagnostic is stored
    # and printed, so it is bounded and redacted here, not only at the last print.
    diagnostic = redact_diagnostic(diagnostic, config_dir=config_dir)
    return LaneResult(
        role=role,
        vendor=vendor,
        model=model,
        status="unavailable",
        argv=argv,
        returncode=returncode,
        stdout="",
        stderr=diagnostic,
        tmux_session=tmux_session,
        config_dir=config_dir,
    )


def finalize_lane_output(
    vendor: str,
    stdout: str,
    output_file_text: str,
    *,
    cwd: str,
    stream_from_output_file: bool = False,
    config_dir: str | None = None,
) -> LaneOutput:
    """Final text, usage and tools of one lane run, with the account directory hidden."""
    out = _finalize_lane_output(
        vendor, stdout, output_file_text, cwd=cwd, stream_from_output_file=stream_from_output_file
    )
    usage = out.usage
    if usage is not None and usage.model is not None:
        usage = replace(
            usage,
            model=redact_lane_text(
                usage.model, config_dir=config_dir, limit=None
            ),
        )
    return replace(
        out,
        text=redact_lane_text(out.text, config_dir=config_dir, limit=None),
        usage=usage,
        # Callers that persist output use ``text``.  The coordinator needs the
        # validated source briefly so schema punctuation consumed by secret
        # redaction can still be restored around already-redacted fields.
        source_text=out.text,
    )


def _finalize_lane_output(
    vendor: str,
    stdout: str,
    output_file_text: str,
    *,
    cwd: str,
    stream_from_output_file: bool = False,
) -> LaneOutput:
    """Normalize final text, reported usage, and the bounded tool transcript."""
    if vendor == "grok":
        return LaneOutput(stdout, None, (), False)
    if vendor == "codex":
        return LaneOutput(output_file_text or stdout, None, (), False)
    if vendor not in ("claude", "openrouter"):
        return LaneOutput("", None, (), False)

    stream = (
        output_file_text
        if vendor == "openrouter" or stream_from_output_file
        else stdout
    )
    events: list[dict] = []
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(event, dict):
            events.append(event)

    result_event: dict | None = None
    init_model: str | None = None
    tool_rows: list[tuple[str | None, dict]] = []
    tool_errors: dict[str, bool] = {}
    for event in events:
        if event.get("type") == "system" and event.get("subtype") == "init":
            model = event.get("model")
            if isinstance(model, str):
                init_model = model
        if event.get("type") == "result":
            result_event = event
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        if event.get("type") == "assistant":
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name = block.get("name")
                inputs = block.get("input")
                target: object = ""
                target_kind = ""
                if isinstance(inputs, dict):
                    for key in ("file_path", "path", "pattern"):
                        if key in inputs:
                            target = inputs[key]
                            target_kind = key
                            break
                tool_name = str(name) if name is not None else ""
                row = {
                    "tool": tool_name,
                    "target": _tool_target(target, cwd, target_kind),
                    "error": False,
                }
                use_id = block.get("id")
                tool_rows.append((use_id if isinstance(use_id, str) else None, row))
        elif event.get("type") == "user":
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                use_id = block.get("tool_use_id")
                if isinstance(use_id, str):
                    tool_errors[use_id] = block.get("is_error") is True

    transcript: list[dict] = []
    for use_id, row in tool_rows[:200]:
        if use_id is not None:
            row["error"] = tool_errors.get(use_id, False)
        transcript.append(row)
    truncated = len(tool_rows) > 200
    if result_event is None:
        return LaneOutput("", None, tuple(transcript), truncated)

    result_text = result_event.get("result")
    subtype = result_event.get("subtype")
    failed = result_event.get("is_error") is True or (subtype is not None and subtype != "success")
    # A failed run never reports its text, so a STATUS marker in it cannot pass as complete.
    text = result_text if isinstance(result_text, str) and not failed else ""
    usage_raw = result_event.get("usage")
    usage: LaneUsage | None = None
    if isinstance(usage_raw, dict):
        model: str | None = None
        model_usage = result_event.get("modelUsage")
        if isinstance(model_usage, dict) and len(model_usage) == 1:
            only = next(iter(model_usage))
            if isinstance(only, str):
                model = only
        if model is None:
            model = init_model
        usage = LaneUsage(
            model=model,
            input_tokens=_int_or_zero(usage_raw.get("input_tokens")),
            output_tokens=_int_or_zero(usage_raw.get("output_tokens")),
            cache_read_input_tokens=_int_or_zero(usage_raw.get("cache_read_input_tokens")),
            cache_creation_input_tokens=_int_or_zero(
                usage_raw.get("cache_creation_input_tokens")
            ),
            num_turns=_int_or_none(result_event.get("num_turns")),
            duration_ms=_int_or_none(result_event.get("duration_ms")),
        )
    return LaneOutput(text, usage, tuple(transcript), truncated)


def record_lane_run(
    store: object,
    *,
    session_id: str,
    result: LaneResult,
    task_id: str | None = None,
    agent_id: str | None = None,
    round_num: int | None = None,
    activity_id: str | None = None,
) -> str:
    """Write the script-owned, redacted lane audit activity."""
    metadata = bounded_lane_metadata(result)
    activity_id = activity_id or str(uuid.uuid4())
    final_text = result.stdout
    if not final_text and result.vendor in ("claude", "openrouter"):
        final_text = result.stderr
    payload = {
        "role": result.role,
        "vendor": result.vendor,
        **metadata,
        "task_id": task_id,
        "agent_id": agent_id,
        "round": round_num,
        "status": result.status,
        "returncode": result.returncode,
        "final_text": redact_lane_text(
            final_text, config_dir=result.config_dir, limit=4000
        ),
    }
    store.write(  # type: ignore[attr-defined]
        "activity",
        "insert",
        activity_id,
        {
            "id": activity_id,
            "session_id": session_id,
            "type": "lane.run",
            "payload": payload,
            "execution_status": "done",
        },
    )
    return activity_id


def _bounded_usage_value(value: int | None) -> int | None:
    if value is None:
        return None
    return max(0, min(value, LANE_USAGE_VALUE_LIMIT))


def bounded_lane_metadata(result: LaneResult) -> dict[str, object]:
    """Return the shared bounded/redacted model, usage, and tool audit fields."""
    usage = result.usage
    usage_payload = None
    if usage is not None:
        usage_payload = {
            "input_tokens": _bounded_usage_value(usage.input_tokens),
            "output_tokens": _bounded_usage_value(usage.output_tokens),
            "cache_read_input_tokens": _bounded_usage_value(
                usage.cache_read_input_tokens
            ),
            "cache_creation_input_tokens": _bounded_usage_value(
                usage.cache_creation_input_tokens
            ),
            "num_turns": _bounded_usage_value(usage.num_turns),
            "duration_ms": _bounded_usage_value(usage.duration_ms),
        }
    safe_tools = []
    for item in result.tools[:LANE_TOOL_LIMIT]:
        if not isinstance(item, dict):
            continue
        safe_tools.append(
            {
                "tool": redact_lane_text(
                    str(item.get("tool") or ""),
                    config_dir=result.config_dir,
                    limit=100,
                ),
                "target": (
                    "[redacted]"
                    if _CONTROL_BYTE_RE.search(str(item.get("target") or ""))
                    else redact_lane_text(
                        str(item.get("target") or ""),
                        config_dir=result.config_dir,
                        limit=300,
                    )
                ),
                "error": item.get("error") is True,
            }
        )
    return {
        "model": redact_lane_text(
            result.model, config_dir=result.config_dir, limit=LANE_MODEL_LIMIT
        ),
        "reported_model": (
            redact_lane_text(
                usage.model,
                config_dir=result.config_dir,
                limit=LANE_MODEL_LIMIT,
            )
            if usage is not None and usage.model is not None
            else None
        ),
        "usage": usage_payload,
        "tools": safe_tools,
        "tools_truncated": (
            result.tools_truncated or len(result.tools) > LANE_TOOL_LIMIT
        ),
    }


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
    captured = _tmux_call(
        ["tmux", "capture-pane", "-t", name, "-p", "-J", "-S", "-"]
    )
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
    read_dirs: tuple[str, ...] = (),
    binding: AIRole | None = None,
) -> LaneResult:
    if role not in LANE_ROLES:
        raise SystemExit(f"role must be {'|'.join(LANE_ROLES)}")
    if vendor not in LANE_VENDORS:
        raise SystemExit(f"vendor must be {PROVIDER_CHOICES}")
    if not session_id or config_home is None:
        raise SystemExit("lane requires an explicit session and AI configuration home")
    from .ai_accounts import load_ai_accounts

    ai = load_ai_accounts(config_home, defer_model_configuration=True)
    if binding is None:
        selected = ai.for_lane(session_id, role, vendor, require_execution=False)
    else:
        selected = binding
        if selected.account.provider != vendor:
            raise SystemExit("lane binding provider does not match requested vendor")

    path = Path(spec_file)
    if not path.is_file():
        raise SystemExit(f"spec-file not found: {spec_file}")
    spec_text = path.read_text(encoding="utf-8")
    if not spec_text.strip():
        raise SystemExit(f"spec-file is empty: {spec_file}")
    spec_file = str(path.resolve())
    cwd = str(Path(cwd).resolve())
    read_dirs = tuple(str(Path(directory).resolve()) for directory in read_dirs)

    ai.require_model_configuration()
    write = selected.access == "workspace-write"
    if vendor in ("claude", "openrouter") and write and read_dirs:
        raise SystemExit("write lane takes no extra directories")
    if vendor == "openrouter":
        overlap = openrouter_path_error(selected.account.config_dir, cwd, read_dirs)
        if overlap is not None:
            raise SystemExit(overlap)
    if not dry_run:
        ai.require_lane_execution()
    lane_output_file: str | None = None
    output_file: str | None = None
    if vendor in OUTPUT_FILE_VENDORS or (vendor == "claude" and tmux):
        if dry_run:
            output_file = f"/tmp/agent-lane-{vendor}-dry-run.txt"
        else:
            fd, lane_output_file = tempfile.mkstemp(
                prefix=f"agent-lane-{vendor}-",
                suffix=".txt",
            )
            os.close(fd)
            output_file = lane_output_file
    argv = provider_argv(
        vendor,
        spec_file=spec_file,
        cwd=cwd,
        write=write,
        model=selected.model,
        output_file=output_file,
        read_dirs=read_dirs,
    )

    # Apply the selected provider home inside the tmux child too. The builders'
    # credential-clearing prefix is nested, never a process-global mutation.
    argv = [*selected.env_prefix(), *argv]
    github_environment = sanitized_lane_argv(argv)
    argv = github_environment.__enter__()

    tmux_session: str | None = None
    claude_redirect = False
    try:
        if tmux:
            unique = "" if dry_run else uuid.uuid4().hex[:8]
            tmux_session = lane_tmux_name(vendor=vendor, role=role, unique=unique)
            if vendor == "claude":
                # Claude's stream-json lines can exceed a tmux pane width and its
                # history. Keep the stream in a file and use pane capture only as
                # process diagnostics, never as the parser input.
                if output_file is None:
                    raise SystemExit("claude tmux lane requires an output file")
                argv = [
                    "sh",
                    "-c",
                    'spec=$1; out=$2; shift 2; exec "$@" < "$spec" > "$out"',
                    "sh",
                    spec_file,
                    output_file,
                    *argv,
                ]
                claude_redirect = True
            elif vendor == "openrouter":
                # Claude Code reads the prompt from stdin only when stdin is not a
                # TTY; in a tmux pane it is. Redirect this adapter's spec file.
                argv = ["sh", "-c", 'f=$1; shift; exec "$@" < "$f"', "sh", spec_file, *argv]
                claude_redirect = True
            argv = tmux_wrap_argv(argv, name=tmux_session, cwd=cwd)
    except BaseException:
        github_environment.__exit__(None, None, None)
        raise

    if dry_run:
        try:
            return LaneResult(
                role=role,
                vendor=vendor,
                model=selected.model,
                status="",
                argv=argv,
                returncode=0,
                stdout="",
                stderr="",
                tmux_session=tmux_session,
                config_dir=selected.account.config_dir,
            )
        finally:
            github_environment.__exit__(None, None, None)

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

        output_file_text = ""
        if lane_output_file is not None:
            try:
                output_file_text = Path(lane_output_file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                # A completed child whose output cannot be read or decoded has no
                # usable verdict; that is an unavailable infrastructure result.
                return unreadable_output_result(
                    role=role,
                    vendor=vendor,
                    model=selected.model,
                    argv=argv,
                    returncode=returncode,
                    stderr=stderr,
                    exc=exc,
                    config_dir=selected.account.config_dir,
                    tmux_session=tmux_session,
                )

        stream_from_output_file = vendor in OUTPUT_FILE_VENDORS or (
            vendor == "claude" and tmux
        )
        finalized = finalize_lane_output(
            vendor,
            stdout,
            output_file_text,
            cwd=cwd,
            stream_from_output_file=stream_from_output_file,
            config_dir=selected.account.config_dir,
        )
        # The protocol is counted on the raw source exactly once. Only the
        # redacted body below is returned to callers or persisted.
        from .coordinator_common import parse_model_result

        protocol_status, protocol_result = parse_model_result(
            finalized.source_text, returncode
        )
        if stream_from_output_file and not finalized.text and stdout and not stderr:
            # In tmux the pane merges stderr into stdout; keep that diagnostic as stderr
            # so the CLI and lane.run show why a lane produced no stream output.
            stderr = stdout
        return LaneResult(
            role=role,
            vendor=vendor,
            model=selected.model,
            status=protocol_status,
            argv=argv,
            returncode=returncode,
            stdout=finalized.text,
            stderr=redact_lane_text(
                stderr, config_dir=selected.account.config_dir, limit=None
            ),
            tmux_session=tmux_session,
            config_dir=selected.account.config_dir,
            usage=finalized.usage,
            tools=finalized.tools,
            tools_truncated=finalized.tools_truncated,
            protocol_result=protocol_result,
        )
    finally:
        if lane_output_file is not None:
            try:
                os.unlink(lane_output_file)
            except OSError:
                pass
        github_environment.__exit__(None, None, None)
