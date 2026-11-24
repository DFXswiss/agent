"""The scenario script of the model double: format, loading, request matching and template variables.

Ported from the Spike C prototype (``modeldouble/scenario.py``) and extended by header expectations, repetitions,
default variables, OpenRouter extras and the ``has_tools``/``has_tool_result`` matchers. A scenario is a JSON
object::

    {
      "name": "write-read",
      "description": "free text",
      "capture": {"cwd": {"regex": "Primary working directory: (\\\\S+)", "source": "messages"}},
      "variables": {"foreign_work": "/srv/sessions/b2/work"},
      "openrouter": {"provider": "DeepInfra"},
      "steps": [
        {"id": "write",
         "expect": {"user_text_contains": "notes.txt", "tools_include": ["Write"],
                    "headers": {"anthropic-version": "2023-06-01"}},
         "reply": {"content": [
            {"type": "text", "text": "Creating the file."},
            {"type": "tool_use", "name": "Write",
             "input": {"file_path": "${cwd}/notes.txt", "content": "hello"}}]}},
        {"id": "busy", "repeat": 2,
         "error": {"status": 529, "type": "overloaded_error", "message": "Overloaded"}},
        {"id": "done", "expect": {"tool_result_contains": "notes.txt"},
         "reply": {"content": [{"type": "text", "text": "Done."}], "cost": 0.0004}}
      ],
      "side": [{"id": "title", "expect": {"has_tools": false}, "reply": {"content": [{"type": "text", "text": "T"}]}}]
    }

Steps are consumed strictly in order, one per request; a step with ``repeat: n`` answers the next n requests. A
request that does not satisfy the next step's ``expect`` is a failure: it is answered with an
``invalid_request_error`` naming the mismatch, so Claude Code stops instead of drifting. ``max_messages`` bounds
the length of ``messages`` (a fresh conversation has exactly one); ``text_absent`` lists texts that must appear
nowhere in the request, neither in the system prompt nor in any message (text or tool result) - together they show
that a request carries no history of another conversation. ``side`` steps are never
consumed: a request that matches one of them (checked before the ordered steps) is answered by it; they serve
background requests (title generation and the like) that are not part of the conversation under test.

``capture`` (top level or per step) extracts named values from requests with a regular expression (first group).
Strings in a reply or error may refer to them, and to ``variables`` (defaults that the caller may override), as
``${name}``. Without a top-level ``capture`` the scenario captures ``cwd`` from the environment block Claude Code
sends.

``openrouter`` switches on the OpenRouter-shaped extras (``provider`` in the message, ``usage.cost`` with
``cost_details``, header ``x-generation-id``); a reply may then fix ``cost`` and ``provider``.

Pure module: no clock, no network, no file access except `load_scenario`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from agent_doubles.common.journal import SECRET_HEADERS

# A path ends at white space, a quote or an angle bracket (the environment block may be wrapped in tags).
CWD_REGEX = r"Primary working directory: ([^\s<>\"']+)"
DEFAULT_CAPTURE = {"cwd": {"regex": CWD_REGEX, "source": "messages"}}
CAPTURE_SOURCES = ("system", "messages", "all")
MATCHER_KEYS = frozenset(
    {
        "user_text_contains",
        "tool_result_contains",
        "tool_result_is_error",
        "has_tool_result",
        "has_tools",
        "tools_include",
        "tools_exclude",
        "system_contains",
        "model",
        "stream",
        "min_messages",
        "max_messages",
        "text_absent",
        "headers",
        "headers_absent",
    }
)
SCENARIO_KEYS = frozenset({"name", "description", "capture", "variables", "openrouter", "steps", "side"})
STEP_KEYS = frozenset({"id", "expect", "capture", "reply", "error", "delay_ms", "repeat"})
REPLY_KEYS = frozenset({"content", "stop_reason", "usage", "model", "chunk_size", "cost", "provider"})
ERROR_KEYS = frozenset({"status", "type", "message", "headers", "in_stream"})
BLOCK_TYPES = ("text", "tool_use", "thinking")
STOP_REASONS = ("end_turn", "tool_use", "max_tokens", "stop_sequence", "refusal", "pause_turn")
DEFAULT_PROVIDER = "double-provider"
DEFAULT_CHUNK_SIZE = 24
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_PACKAGED = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


class ScenarioError(ValueError):
    """A scenario that does not follow the format, or a template naming an unknown value."""


@dataclass(frozen=True)
class Capture:
    """One named value taken from a request: the first group of `regex` in `source`."""

    name: str
    regex: re.Pattern[str]
    source: str


@dataclass(frozen=True)
class Matcher:
    """What a request must satisfy. Every given field must hold; an empty matcher holds always."""

    user_text_contains: str | None = None
    tool_result_contains: str | None = None
    tool_result_is_error: bool | None = None
    has_tool_result: bool | None = None
    has_tools: bool | None = None
    tools_include: tuple[str, ...] = ()
    tools_exclude: tuple[str, ...] = ()
    system_contains: str | None = None
    model: str | None = None
    stream: bool | None = None
    min_messages: int | None = None
    max_messages: int | None = None
    text_absent: tuple[str, ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    headers_absent: tuple[str, ...] = ()

    def mismatches(self, body: dict[str, Any], headers: Mapping[str, str]) -> list[str]:
        """Every condition the request fails, as readable text; empty when it matches.

        `headers` holds lower-case names and the unredacted values; texts never repeat a credential value.
        """
        return self._content_mismatches(body) + self._request_mismatches(body) + self._header_mismatches(headers)

    def _content_mismatches(self, body: dict[str, Any]) -> list[str]:
        out: list[str] = []
        last = last_user_message(body)
        results = tool_results(last)
        if self.user_text_contains is not None and self.user_text_contains not in "\n".join(user_texts(last)):
            out.append(f"user text lacks {self.user_text_contains!r}")
        if self.tool_result_contains is not None and not any(self.tool_result_contains in r.text for r in results):
            out.append(f"no tool_result contains {self.tool_result_contains!r}")
        if self.tool_result_is_error is not None and (
            not results or any(r.is_error != self.tool_result_is_error for r in results)
        ):
            out.append(f"tool_result is_error is not {self.tool_result_is_error}")
        if self.has_tool_result is not None and bool(results) != self.has_tool_result:
            out.append("tool_result missing" if self.has_tool_result else "unexpected tool_result")
        if self.system_contains is not None and self.system_contains not in system_text(body):
            out.append(f"system prompt lacks {self.system_contains!r}")
        if self.text_absent:
            whole = system_text(body) + "\n" + messages_text(body)
            present = [text for text in self.text_absent if text in whole]
            if present:
                out.append("request holds " + ", ".join(repr(text) for text in present))
        return out

    def _request_mismatches(self, body: dict[str, Any]) -> list[str]:
        out: list[str] = []
        names = tool_names(body)
        if self.has_tools is not None and bool(names) != self.has_tools:
            out.append("no tools offered" if self.has_tools else "tools offered")
        out.extend(f"tool {name!r} not offered" for name in self.tools_include if name not in names)
        out.extend(f"tool {name!r} offered" for name in self.tools_exclude if name in names)
        if self.model is not None and body.get("model") != self.model:
            out.append(f"model is {body.get('model')!r}, expected {self.model!r}")
        if self.stream is not None and bool(body.get("stream")) != self.stream:
            out.append(f"stream is {bool(body.get('stream'))}, expected {self.stream}")
        if self.min_messages is not None and len(body.get("messages") or []) < self.min_messages:
            out.append(f"fewer than {self.min_messages} messages")
        if self.max_messages is not None and len(body.get("messages") or []) > self.max_messages:
            out.append(f"more than {self.max_messages} messages")
        return out

    def _header_mismatches(self, headers: Mapping[str, str]) -> list[str]:
        out: list[str] = []
        for name, expected in self.headers:
            if expected not in headers.get(name, ""):
                shown = "the expected value" if name in SECRET_HEADERS else repr(expected)
                out.append(f"header {name} lacks {shown}")
        out.extend(f"header {name} present" for name in self.headers_absent if name in headers)
        return out


@dataclass(frozen=True)
class Reply:
    """A successful answer. `content` holds the blocks as written in the script."""

    content: tuple[dict[str, Any], ...]
    stop_reason: str | None = None
    usage: dict[str, int] | None = None
    model: str | None = None
    chunk_size: int = DEFAULT_CHUNK_SIZE
    cost: float | None = None
    provider: str | None = None


@dataclass(frozen=True)
class ErrorReply:
    """An API error: an HTTP status with an Anthropic error body, or an ``error`` event mid-stream."""

    status: int
    type: str
    message: str
    headers: tuple[tuple[str, str], ...] = ()
    in_stream: bool = False


@dataclass(frozen=True)
class Step:
    """One scripted answer, the request it expects and how many requests it answers."""

    id: str
    expect: Matcher
    captures: tuple[Capture, ...]
    reply: Reply | None
    error: ErrorReply | None
    delay_ms: int = 0
    repeat: int = 1


@dataclass(frozen=True)
class OpenRouter:
    """The OpenRouter-shaped extras of a scenario: the default upstream provider name."""

    provider: str = DEFAULT_PROVIDER


@dataclass(frozen=True)
class Scenario:
    """A loaded script."""

    name: str
    description: str
    captures: tuple[Capture, ...]
    steps: tuple[Step, ...]
    side: tuple[Step, ...] = ()
    variables: Mapping[str, str] = field(default_factory=dict)
    openrouter: OpenRouter | None = None


@dataclass(frozen=True)
class ToolResult:
    """One ``tool_result`` block of a user message, with its content flattened to text."""

    tool_use_id: str
    text: str
    is_error: bool


# --- reading a request ------------------------------------------------------------------------


def last_user_message(body: dict[str, Any]) -> dict[str, Any] | None:
    """The last message with role ``user``, or None."""
    for message in reversed(body.get("messages") or []):
        if isinstance(message, dict) and message.get("role") == "user":
            return message
    return None


def _blocks(message: dict[str, Any] | None) -> list[dict[str, Any]]:
    if message is None:
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def user_texts(message: dict[str, Any] | None) -> list[str]:
    """The text blocks of a message, without tool results."""
    return [str(b.get("text", "")) for b in _blocks(message) if b.get("type") == "text"]


def tool_results(message: dict[str, Any] | None) -> list[ToolResult]:
    """The ``tool_result`` blocks of a message."""
    out = []
    for block in _blocks(message):
        if block.get("type") != "tool_result":
            continue
        content = block.get("content")
        if isinstance(content, list):
            text = "\n".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
        else:
            text = str(content or "")
        out.append(ToolResult(str(block.get("tool_use_id")), text, bool(block.get("is_error"))))
    return out


def tool_names(body: dict[str, Any]) -> list[str]:
    """The names of the tools the request offers."""
    return [str(t.get("name", "")) for t in body.get("tools") or [] if isinstance(t, dict)]


def system_text(body: dict[str, Any]) -> str:
    """The system prompt as one string (string form or text blocks)."""
    system = body.get("system")
    if isinstance(system, str):
        return system
    return "\n".join(str(b.get("text", "")) for b in system or [] if isinstance(b, dict))


def messages_text(body: dict[str, Any]) -> str:
    """All text and tool result text of all messages, in order."""
    parts: list[str] = []
    for message in body.get("messages") or []:
        if not isinstance(message, dict):
            continue
        parts.extend(user_texts(message))
        parts.extend(r.text for r in tool_results(message))
    return "\n".join(parts)


def capture_values(captures: tuple[Capture, ...], body: dict[str, Any]) -> dict[str, str]:
    """The values `captures` find in the request; a capture that finds nothing is left out."""
    sources = {"system": system_text(body), "messages": messages_text(body)}
    sources["all"] = sources["system"] + "\n" + sources["messages"]
    found: dict[str, str] = {}
    for capture in captures:
        match = capture.regex.search(sources[capture.source])
        if match:
            found[capture.name] = match.group(1)
    return found


def expand(value: Any, variables: Mapping[str, str]) -> Any:
    """`value` with every ``${name}`` in its strings replaced; unknown names raise ScenarioError."""
    if isinstance(value, str):

        def sub(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                raise ScenarioError(f"template names unknown value ${{{name}}}")
            return variables[name]

        return _VAR.sub(sub, value)
    if isinstance(value, list):
        return [expand(v, variables) for v in value]
    if isinstance(value, dict):
        return {k: expand(v, variables) for k, v in value.items()}
    return value


# --- loading ----------------------------------------------------------------------------------


def _object(value: Any, path: str, allowed: frozenset[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ScenarioError(f"{path} must be an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ScenarioError(f"{path} has unknown keys {unknown}")
    return value


def _str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ScenarioError(f"{path} must be a string")
    return value


def _bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise ScenarioError(f"{path} must be a boolean")
    return value


def _count(value: Any, path: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ScenarioError(f"{path} must be an integer of at least {minimum}")
    return value


def _strings(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ScenarioError(f"{path} must be a list of strings")
    return tuple(value)


def _string_map(value: Any, path: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(isinstance(v, str) for v in value.values()):
        raise ScenarioError(f"{path} must map names to strings")
    return value


def _captures(raw: Any, path: str) -> tuple[Capture, ...]:
    if not isinstance(raw, dict):
        raise ScenarioError(f"{path} must be an object")
    out = []
    for name, value in raw.items():
        spec = _object(value, f"{path}.{name}", frozenset({"regex", "source"}))
        source = spec.get("source", "all")
        if source not in CAPTURE_SOURCES:
            raise ScenarioError(f"{path}.{name}.source must be one of {CAPTURE_SOURCES}")
        try:
            regex = re.compile(_str(spec.get("regex"), f"{path}.{name}.regex"))
        except re.error as exc:
            raise ScenarioError(f"{path}.{name}.regex: {exc}") from exc
        if regex.groups < 1:
            raise ScenarioError(f"{path}.{name}.regex needs a group")
        out.append(Capture(name, regex, source))
    return tuple(out)


def _matcher(raw: Any, path: str) -> Matcher:
    raw = _object(raw, path, MATCHER_KEYS)
    kwargs: dict[str, Any] = {}
    for key in ("user_text_contains", "tool_result_contains", "system_contains", "model"):
        if key in raw:
            kwargs[key] = _str(raw[key], f"{path}.{key}")
    for key in ("tools_include", "tools_exclude", "text_absent"):
        if key in raw:
            kwargs[key] = _strings(raw[key], f"{path}.{key}")
    for key in ("tool_result_is_error", "has_tool_result", "has_tools", "stream"):
        if key in raw:
            kwargs[key] = _bool(raw[key], f"{path}.{key}")
    if "min_messages" in raw:
        kwargs["min_messages"] = _count(raw["min_messages"], f"{path}.min_messages", 0)
    if "max_messages" in raw:
        kwargs["max_messages"] = _count(raw["max_messages"], f"{path}.max_messages", 1)
    if "headers" in raw:
        headers = _string_map(raw["headers"], f"{path}.headers")
        kwargs["headers"] = tuple((name.lower(), value) for name, value in headers.items())
    if "headers_absent" in raw:
        kwargs["headers_absent"] = tuple(n.lower() for n in _strings(raw["headers_absent"], f"{path}.headers_absent"))
    return Matcher(**kwargs)


def _block(raw: Any, path: str) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("type") not in BLOCK_TYPES:
        raise ScenarioError(f"{path} must be a block of type {BLOCK_TYPES}")
    kind = raw["type"]
    if kind == "text":
        _object(raw, path, frozenset({"type", "text"}))
        _str(raw.get("text"), f"{path}.text")
    elif kind == "tool_use":
        _object(raw, path, frozenset({"type", "name", "input", "id"}))
        _str(raw.get("name"), f"{path}.name")
        if not isinstance(raw.get("input", {}), dict):
            raise ScenarioError(f"{path}.input must be an object")
        if "id" in raw:
            _str(raw["id"], f"{path}.id")
    else:
        _object(raw, path, frozenset({"type", "thinking", "signature"}))
        _str(raw.get("thinking"), f"{path}.thinking")
        if "signature" in raw:
            _str(raw["signature"], f"{path}.signature")
    return raw


def _cost(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        raise ScenarioError(f"{path} must be a non-negative number")
    return float(value)


def _reply(raw: Any, path: str, openrouter: bool) -> Reply:
    raw = _object(raw, path, REPLY_KEYS)
    content = raw.get("content")
    if not isinstance(content, list) or not content:
        raise ScenarioError(f"{path}.content must be a non-empty list")
    blocks = tuple(_block(b, f"{path}.content[{i}]") for i, b in enumerate(content))
    stop = raw.get("stop_reason")
    if stop is not None and stop not in STOP_REASONS:
        raise ScenarioError(f"{path}.stop_reason must be one of {STOP_REASONS}")
    usage = raw.get("usage")
    if usage is not None and (
        not isinstance(usage, dict)
        or not all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in usage.values())
    ):
        raise ScenarioError(f"{path}.usage must map names to non-negative integers")
    chunk = _count(raw.get("chunk_size", DEFAULT_CHUNK_SIZE), f"{path}.chunk_size", 1)
    model = _str(raw["model"], f"{path}.model") if "model" in raw else None
    if not openrouter and ("cost" in raw or "provider" in raw):
        raise ScenarioError(f"{path}: cost and provider need the scenario's openrouter extras")
    cost = _cost(raw["cost"], f"{path}.cost") if "cost" in raw else None
    provider = _str(raw["provider"], f"{path}.provider") if "provider" in raw else None
    return Reply(blocks, stop, usage, model, chunk, cost, provider)


def _error(raw: Any, path: str) -> ErrorReply:
    raw = _object(raw, path, ERROR_KEYS)
    status = raw.get("status")
    if isinstance(status, bool) or not isinstance(status, int) or not 400 <= status <= 599:
        raise ScenarioError(f"{path}.status must be an HTTP error status")
    headers = _string_map(raw.get("headers", {}), f"{path}.headers")
    return ErrorReply(
        status,
        _str(raw.get("type"), f"{path}.type"),
        _str(raw.get("message", raw.get("type")), f"{path}.message"),
        tuple(headers.items()),
        _bool(raw.get("in_stream", False), f"{path}.in_stream"),
    )


def _step(raw: Any, path: str, index: int, *, openrouter: bool, side: bool) -> Step:
    raw = _object(raw, path, STEP_KEYS)
    if ("reply" in raw) == ("error" in raw):
        raise ScenarioError(f"{path} needs exactly one of reply and error")
    if side and "repeat" in raw:
        raise ScenarioError(f"{path}: side steps are never consumed and cannot repeat")
    return Step(
        id=_str(raw.get("id", f"step{index}"), f"{path}.id"),
        expect=_matcher(raw.get("expect", {}), f"{path}.expect"),
        captures=_captures(raw.get("capture", {}), f"{path}.capture"),
        reply=_reply(raw["reply"], f"{path}.reply", openrouter) if "reply" in raw else None,
        error=_error(raw["error"], f"{path}.error") if "error" in raw else None,
        delay_ms=_count(raw.get("delay_ms", 0), f"{path}.delay_ms", 0),
        repeat=_count(raw.get("repeat", 1), f"{path}.repeat", 1),
    )


def _variables(raw: Any, path: str) -> dict[str, str]:
    values = _string_map(raw, path)
    for name in values:
        if not _NAME.fullmatch(name):
            raise ScenarioError(f"{path}: invalid variable name {name!r}")
    return dict(values)


def _openrouter(raw: Any) -> OpenRouter | None:
    if raw is None:
        return None
    spec = _object(raw, "scenario.openrouter", frozenset({"provider"}))
    return OpenRouter(_str(spec.get("provider", DEFAULT_PROVIDER), "scenario.openrouter.provider"))


def parse_scenario(raw: Any) -> Scenario:
    """A Scenario from the decoded JSON; raises ScenarioError naming the first problem."""
    raw = _object(raw, "scenario", SCENARIO_KEYS)
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ScenarioError("scenario.steps must be a non-empty list")
    side = raw.get("side", [])
    if not isinstance(side, list):
        raise ScenarioError("scenario.side must be a list")
    openrouter = _openrouter(raw.get("openrouter"))
    extras = openrouter is not None
    parsed = tuple(_step(s, f"scenario.steps[{i}]", i, openrouter=extras, side=False) for i, s in enumerate(steps))
    ids = [s.id for s in parsed]
    if len(set(ids)) != len(ids):
        raise ScenarioError("scenario.steps ids must be unique")
    return Scenario(
        name=_str(raw.get("name"), "scenario.name"),
        description=_str(raw.get("description", ""), "scenario.description"),
        captures=_captures(raw.get("capture", DEFAULT_CAPTURE), "scenario.capture"),
        steps=parsed,
        side=tuple(_step(s, f"scenario.side[{i}]", i, openrouter=extras, side=True) for i, s in enumerate(side)),
        variables=_variables(raw.get("variables", {}), "scenario.variables"),
        openrouter=openrouter,
    )


def load_scenario(path: str | Path) -> Scenario:
    """Read and parse a scenario file."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as exc:
        raise ScenarioError(f"{path}: {exc}") from exc
    return parse_scenario(raw)


def packaged_names() -> list[str]:
    """The names of the scenarios shipped with the double (``scenarios/<name>.json``), sorted."""
    folder = resources.files("agent_doubles.model").joinpath("scenarios")
    return sorted(item.name.removesuffix(".json") for item in folder.iterdir() if item.name.endswith(".json"))


def packaged_scenario(name: str) -> Scenario:
    """The shipped scenario `name` (for example ``atk-01`` or ``e2e-tools``)."""
    if not _PACKAGED.fullmatch(name) or name not in packaged_names():
        raise ScenarioError(f"no packaged scenario {name!r}")
    text = resources.files("agent_doubles.model").joinpath("scenarios", f"{name}.json").read_text(encoding="utf-8")
    return parse_scenario(json.loads(text))
