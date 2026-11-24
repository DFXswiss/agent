"""Extension points of the operation pipeline (ADR-027 section 4 style; ADR-041).

Executors are registered at the runner's existing point `<area>/executors.py:EXECUTORS` (mapping operation type ->
`Executor`, `registry.executors`). Two more points are discovered the same way (`registry.discover`, areas in sorted
order), so the executor packages add files and never edit this area:

- `<area>/op_prechecks.py:PRECHECKS`: mapping operation type -> `Precheck`. A pre-check runs after the formal checks
  of this area and before the dedupe tag, for checks that need GitHub or the workspace (RUN-09: `repo_not_accessible`;
  RUN-10: the commit snapshot, parent and tree SHA, secret scan, sizes). It raises `CheckFailed` or returns a
  `PrecheckResult`: `dedupe` adds the dedupe fields only known at check time (`github.checkout` `ref` = resolved SHA,
  `github.commit` `parent_sha` and `tree_sha`), `data` is kept with the operation (`Operation.checked["data"]`) for its
  executor. At most one pre-check per type.
- `<area>/op_finished.py:NOTIFY`: `notify(context, operation) -> Awaitable[None]`, called when an operation became
  final after its MCP call had already returned `pending` (RUN-14: ping `operation.finished` to the session). At most
  one provider; without one the event is only logged.

A type is **enabled** on this device when an executor is registered for it and, for the types in `NEEDS_PRECHECK`, a
pre-check as well; a request for any other type is rejected locally with `unknown_type`.

An executor is `execute(ctx: OperationContext) -> Awaitable[dict]` returning the result object of the type
(`contracts/ops/<type>.result.json`); a failure is `OperationFailed(code, retryable=..., retry_after=...)`. Retryable
failures are retried by the pipeline inside the same operation (2 s, 8 s, 30 s, honouring `retry_after`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from agent_runner import registry
from agent_runner.errors import RunnerError
from agent_runner.operations.records import Operation
from agent_runner.operations.settings import WorkgroupSettings
from agent_runner.sessions.records import LocalSession

NEEDS_PRECHECK: Mapping[str, tuple[str, ...]] = {
    "github.checkout": ("ref",),
    "github.commit": ("parent_sha", "tree_sha"),
}
"""Types whose dedupe fields include values a pre-check determines, with those fields."""


OPERATION_ERROR_CODES = frozenset(
    {
        "unknown_type",
        "params_invalid",
        "repo_not_allowed",
        "repo_not_accessible",
        "branch_protected",
        "branch_exists",
        "non_fast_forward",
        "path_invalid",
        "secret_detected",
        "size_exceeded",
        "github_not_connected",
        "not_found",
        "limit_exceeded",
        "budget_exceeded",
        "credit_exhausted",
        "rights_denied",
        "hub_unreachable",
        "upstream_error",
        "timeout",
        "model_not_allowed",
        "tree_mismatch",
        "no_changes",
        "workvm_unavailable",
    }
)
"""`contracts/enums/operation-error-codes.json` (a test compares them)."""


def known_code(code: object, fallback: str = "upstream_error") -> str:
    """`code` when it is in the closed list, else `fallback` (nothing outside the list reaches the hub or a guest)."""
    return code if isinstance(code, str) and code in OPERATION_ERROR_CODES else fallback


def message_text(message: object) -> str | None:
    """A message the contract allows (printable ASCII, 1-1024 characters), else None."""
    if isinstance(message, str) and 1 <= len(message) <= 1024 and message.isascii() and message.isprintable():
        return message
    return None


class CheckFailed(Exception):
    """A formal check or pre-check refused the operation: `code` of `operation-error-codes.json`, JSON `pointers`
    (`params_invalid`) and an optional English sentence."""

    def __init__(self, code: str, *, pointers: list[str] | None = None, message: str | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.pointers = pointers
        self.message = message

    def error(self) -> dict[str, Any]:
        """The operation error object (`ops/objects.json#/$defs/error`)."""
        found: dict[str, Any] = {"code": known_code(self.code)}
        message = message_text(self.message)
        if message:
            found["message"] = message
        if self.pointers:
            found["pointers"] = self.pointers[:32]
        return found


class OperationFailed(Exception):
    """An executor's failure; `retryable` failures are retried within the operation (`retry_after` in seconds)."""

    def __init__(
        self, code: str, *, retryable: bool = False, retry_after: float | None = None, message: str | None = None
    ) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after
        self.message = message


@dataclass(frozen=True)
class PrecheckRequest:
    context: Any
    operation: Operation
    session: LocalSession
    settings: WorkgroupSettings
    params: Mapping[str, Any]
    """The effective parameters (refs normalised, defaults from the settings applied)."""


@dataclass(frozen=True)
class PrecheckResult:
    dedupe: Mapping[str, object] = field(default_factory=dict)
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperationContext:
    """What an executor receives: the daemon context, the operation (with the hub's `child_session_id` for
    `subagent.request`), its session, the effective parameters and the pre-check data, the attempt number (1-4) and
    the session environment (store, outbox, guest clients)."""

    context: Any
    operation: Operation
    session: LocalSession
    params: Mapping[str, Any]
    data: Mapping[str, Any]
    attempt: int
    env: Any


Executor = Callable[[OperationContext], Awaitable[dict[str, Any]]]
Precheck = Callable[[PrecheckRequest], Awaitable[PrecheckResult]]
Notify = Callable[[Any, Operation], Awaitable[None]]


def prechecks(package: str = registry.ROOT_PACKAGE) -> dict[str, Precheck]:
    found: dict[str, Precheck] = {}
    for area, value in registry.discover("op_prechecks", "PRECHECKS", package):
        if not isinstance(value, Mapping):
            raise RunnerError("registry_invalid", f"{area}.op_prechecks")
        for key, check in value.items():
            if not isinstance(key, str) or not callable(check):
                raise RunnerError("registry_invalid", f"{area}.op_prechecks")
            if key in found:
                raise RunnerError("registry_duplicate", key)
            found[key] = check
    return found


def finished_notifier(package: str = registry.ROOT_PACKAGE) -> Notify | None:
    """The registered `operation.finished` notifier, None when no package provides one."""
    found: list[Notify] = []
    for area, value in registry.discover("op_finished", "NOTIFY", package):
        if not callable(value):
            raise RunnerError("registry_invalid", f"{area}.op_finished")
        found.append(value)
    if len(found) > 1:
        raise RunnerError("registry_duplicate", "NOTIFY")
    return found[0] if found else None
