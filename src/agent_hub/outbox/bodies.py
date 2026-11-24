"""The request and item bodies of `POST /v1/device/outbox` (03-api.md section 6.1, `contracts/outbox/objects.json`).

The envelope (`items` with `origin_seq`, `item_kind` and an object `body`) is checked by FastAPI: a broken envelope is
`400 request_invalid` for the whole push, because without a valid `origin_seq` nothing can be sequenced. Each item body
is checked here when the item is processed (`parse`): a body that breaks its contract is a content refusal of that item
(`rejected` with `request_invalid`), and the sequence continues (a poison item must not block the stream). The models
are strict, refuse unknown fields and use the patterns of `contracts/common.json#/$defs`. Integers that land in
`integer` columns (`vm.memory_mib`, `vm.disk_gib`, `metric_sample.free_vm_slots`) are capped at 2^31 - 1, below the
contract's 2^53 - 1, so that a too large value is a refusal instead of a database error.

`event` and `wrap` bodies keep their inner object as it is: the event rules (`agent_hub.events.store`) and the wrap
rules (`agent_hub.wraps.rules`) check them, as they do for `POST /v1/events` and `POST /v1/device/wraps`.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, model_validator

from agent_hub.auth.fields import ModelId, Strict, Uuid7
from agent_hub.crypto.encoding import MAX_SAFE_INTEGER
from agent_hub.errors import HubError

MAX_ITEMS = 500
MAX_INT4 = 2**31 - 1
ITEM_KINDS = (
    "event",
    "session_transition",
    "vm_state",
    "operation_transition",
    "usage",
    "quota",
    "metrics",
    "audit",
    "wrap",
    "runtime",
)
"""A copy of `contracts/enums/outbox-item-kinds.json` (a test compares them)."""
OUTBOX_AUDIT_ACTIONS = (
    "hook.denied",
    "read.session.get_context",
    "read.session.list_workgroups",
    "read.session.list_members",
    "read.session.list_tasks",
    "read.session.get_task",
    "read.session.get_task_messages",
    "read.session.get_whiteboard",
    "read.session.get_notifications",
    "read.session.get_operation",
    "read.session.list_operations",
    "read.session.get_subagent_result",
    "ping.delivered",
    "session.prompt_refused",
    "model.provider_outside_allowlist",
    "runner.inbound_rejected",
    "runner.log_rollback",
    "runner.log_fork",
    "runner.alert",
)
"""A copy of `contracts/enums/outbox-audit-actions.json` (a test compares them)."""
OPERATION_ERROR_CODES = (
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
)
"""A copy of `contracts/enums/operation-error-codes.json` (a test compares them)."""

ItemKind = Literal[
    "event",
    "session_transition",
    "vm_state",
    "operation_transition",
    "usage",
    "quota",
    "metrics",
    "audit",
    "wrap",
    "runtime",
]
Time = Annotated[
    str,
    StringConstraints(
        pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])T([01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$"
    ),
]
ErrorCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
VersionText = Annotated[str, StringConstraints(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+_-]{0,63}$")]
VmId = Annotated[str, StringConstraints(pattern=r"^vm-[0-9a-f]{8}$")]
LabelText = Annotated[str, StringConstraints(pattern=r"^[ -~]{1,64}$")]
StateText = Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,32}$")]
Count = Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)]
Positive = Annotated[int, Field(ge=1, le=MAX_SAFE_INTEGER)]
Int4Positive = Annotated[int, Field(ge=1, le=MAX_INT4)]
Int4Count = Annotated[int, Field(ge=0, le=MAX_INT4)]
ModelAccess = Literal["subscription", "api_key", "platform"]
OperationErrorCode = Literal[
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
]


# --- envelope ---------------------------------------------------------------------------------------------------------


class RawItem(BaseModel):
    """One item of the push as sent: its body is checked later, per item."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    origin_seq: Positive
    item_kind: ItemKind
    body: dict[str, Any]


class OutboxRequest(BaseModel):
    """Body of `POST /v1/device/outbox`: 1 to 500 items."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    items: list[RawItem] = Field(min_length=1, max_length=MAX_ITEMS)


# --- item bodies ------------------------------------------------------------------------------------------------------


class EventBody(Strict):
    """`event`: one device-authored event, checked by the event rules."""

    event: dict[str, Any]


def _absent_not_null(model: BaseModel, names: tuple[str, ...]) -> None:
    """Optional fields may be left out, never sent as `null` (the contract has no `null` for them)."""
    if any(name in model.model_fields_set and getattr(model, name) is None for name in names):
        raise ValueError("an optional field is null")


class SessionTransitionBody(Strict):
    """`session_transition`; the reason per state is checked by `agent_hub.sessions.transitions.apply`."""

    session_id: Uuid7
    state: Literal[
        "requested",
        "vm_assigning",
        "vm_ready",
        "sandbox_ready",
        "agent_starting",
        "prompt_delivering",
        "working",
        "idle",
        "suspended",
        "resuming",
        "stopping",
        "stopped",
        "failed",
    ]
    reason: ErrorCode | None
    at: Time
    agent_version: VersionText | None = None
    model_access: ModelAccess | None = None
    model_id: ModelId | None = None
    claude_session_id_present: bool | None = None

    @model_validator(mode="after")
    def _optional_fields(self) -> SessionTransitionBody:
        _absent_not_null(self, ("agent_version", "model_access", "model_id", "claude_session_id_present"))
        return self


class VmStateBody(Strict):
    """`vm_state`: the VM row as the runner reports it."""

    vm_id: VmId
    wg_id: Uuid7 | None
    state: Literal["pool", "assigning", "running", "stopping", "stopped", "deleted"]
    golden_version: VersionText
    cpus: Annotated[int, Field(ge=1, le=1024)]
    memory_mib: Int4Positive
    disk_gib: Int4Positive
    at: Time


class OperationTransitionBody(Strict):
    """`operation_transition` with the fields each state requires (contract `oneOf`)."""

    operation_id: Uuid7
    state: Literal["executing", "succeeded", "failed", "expired"]
    error_code: OperationErrorCode | None = None
    retryable: bool | None = None
    result_event_id: Uuid7 | None = None
    at: Time

    @model_validator(mode="after")
    def _fields_of_the_state(self) -> OperationTransitionBody:
        _absent_not_null(self, ("error_code", "retryable", "result_event_id"))
        given = {name for name in ("error_code", "retryable", "result_event_id") if getattr(self, name) is not None}
        required, allowed = {
            "executing": (set(), set()),
            "succeeded": ({"result_event_id"}, {"result_event_id"}),
            "failed": ({"error_code", "retryable", "result_event_id"}, {"error_code", "retryable", "result_event_id"}),
            "expired": ({"error_code"}, {"error_code", "result_event_id"}),
        }[self.state]
        if not required <= given <= allowed:
            raise ValueError("fields do not fit the state")
        if self.state == "expired" and self.error_code not in ("hub_unreachable", "timeout"):
            raise ValueError("an expiry is hub_unreachable or timeout")
        return self


class UsageDelta(Strict):
    """The counters since the previous `usage` item of the session."""

    requests: Count
    input_tokens: Count
    output_tokens: Count
    cache_read_tokens: Count
    cache_write_tokens: Count
    thinking_tokens: Count
    cost_micro_usd: Count


class UsageBody(Strict):
    """`usage`: counters to add, the latest context fill and the provider."""

    session_id: Uuid7
    delta: UsageDelta
    cost_basis: Literal["provider", "list_price", "subscription_equivalent"]
    context_used_tokens: Count
    context_window_tokens: Count
    last_provider: LabelText | None
    model: ModelId | None = None
    """The model of the request (KOR-31; an older runner omits it: the session's model counts)."""
    access_origin: Literal["own", "operator", "platform"] | None = None
    """Whose model access the request used (KOR-31, ADR-062, AUD-04 W10): `own` (the user's own subscription or API
    key), `operator` (an operator subscription, ADR-052) or `platform` (the platform key); an older runner omits it."""
    at: Time | None = None
    """When the request was made (selects the price list version; an older runner omits it)."""


class RuntimeBody(Strict):
    """`runtime` (KOR-31, ADR-062): VM runtime of a platform device attributed to one session."""

    session_id: Uuid7
    vm_id: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{1,62}$")]
    vm_kind: Literal["session", "work"]
    size: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
    from_: Time = Field(alias="from")
    to: Time
    runtime_ms: Annotated[int, Field(ge=1, le=3_600_000)]


class QuotaWindow(Strict):
    """One quota window of a subscription owner."""

    window: Annotated[str, StringConstraints(pattern=r"^(five_hour|seven_day|weekly:[A-Za-z0-9._-]{1,64})$")]
    used_percent: Annotated[float, Field(ge=0, le=1000, allow_inf_nan=False)]
    resets_at: Time | None
    severity: Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,32}$")] | None


class QuotaBody(Strict):
    """`quota`: the windows of one owner as fetched by the device (each window at most once), or `unavailable: true`
    without windows when the usage endpoint refused the owner's token (KOR-05)."""

    owner_id: Uuid7
    windows: list[QuotaWindow] = Field(default_factory=list, max_length=16)
    unavailable: Literal[True] | None = None
    fetched_at: Time

    @model_validator(mode="after")
    def _windows_once(self) -> QuotaBody:
        _absent_not_null(self, ("unavailable",))
        # exactly one of the two forms of the contract: windows (at least one), or the marker without `windows`
        if ("unavailable" in self.model_fields_set) == ("windows" in self.model_fields_set) or (
            self.unavailable is None and not self.windows
        ):
            raise ValueError("a quota has windows or is unavailable")
        names = [window.window for window in self.windows]
        if len(set(names)) != len(names):
            raise ValueError("a window is listed twice")
        return self


class MetricSample(Strict):
    """One raw metric sample (02-data-model.md section 2.8)."""

    entity_kind: Literal["device", "vm", "session", "workvm"]
    entity_id: Annotated[
        str,
        StringConstraints(
            pattern=r"^(w?vm-[0-9a-f]{8}|[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12})$"
        ),
    ]
    session_id: Uuid7 | None = None
    """Work VMs only (KOR-29): the session the work VM belongs to."""
    ts: Time
    cpu_ratio: Annotated[float, Field(ge=0, allow_inf_nan=False)] | None
    mem_used_bytes: Count | None
    mem_limit_bytes: Count | None
    disk_used_bytes: Count | None
    disk_limit_bytes: Count | None
    net_rx_bytes: Count | None
    net_tx_bytes: Count | None
    uptime_seconds: Count | None
    reachable: bool
    state: StateText
    free_vm_slots: Int4Count | None
    free_mem_bytes: Count | None

    @model_validator(mode="after")
    def _id_of_the_kind(self) -> MetricSample:
        if (self.entity_kind == "vm") != self.entity_id.startswith("vm-"):
            raise ValueError("the entity ID does not fit the entity kind")
        if (self.entity_kind == "workvm") != self.entity_id.startswith("wvm-"):
            raise ValueError("the entity ID does not fit the entity kind")
        if (self.entity_kind == "workvm") != (self.session_id is not None):
            raise ValueError("a work VM sample names its session, no other sample does")
        return self


class MetricsBody(Strict):
    """`metrics`: 1 to 1000 samples."""

    samples: list[MetricSample] = Field(min_length=1, max_length=1000)


class WrapBody(Strict):
    """`wrap`: one key wrap, checked by the wrap rules (W3)."""

    wrap: dict[str, Any]


# --- audit details (contracts/audit/<action>.json for the actions of enums/outbox-audit-actions.json) ----------------


class HookDenied(Strict):
    tool: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]+$", min_length=1, max_length=128)]
    rule: Annotated[int, Field(ge=2, le=6)]


class ReadCount(Strict):
    result_count: Count


class PingDelivered(Strict):
    ping_id: Uuid7
    kind: Literal["whiteboard.entry", "subagent.finished", "operation.finished"]
    via: Literal["hook", "tool"]


class PromptRefused(Strict):
    reason: Literal["unregistered", "delivery_failed"]


class ProviderOutsideAllowlist(Strict):
    provider: Annotated[str, StringConstraints(pattern=r"^[ -~]+$", min_length=1, max_length=64)]
    model: ModelId


class InboundRejected(Strict):
    source: Literal["event", "log", "platform_log", "wrap", "request", "assignment", "setting", "credential"]
    code: ErrorCode


class LogMismatch(Strict):
    verified_seq: Count
    offered_seq: Count


class RunnerAlert(Strict):
    alert: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*$", min_length=1, max_length=64)]
    value: Count | None


AUDIT_DETAILS: dict[str, type[Strict]] = {
    "hook.denied": HookDenied,
    **{action: ReadCount for action in OUTBOX_AUDIT_ACTIONS if action.startswith("read.session.")},
    "ping.delivered": PingDelivered,
    "session.prompt_refused": PromptRefused,
    "model.provider_outside_allowlist": ProviderOutsideAllowlist,
    "runner.inbound_rejected": InboundRejected,
    "runner.log_rollback": LogMismatch,
    "runner.log_fork": LogMismatch,
    "runner.alert": RunnerAlert,
}


class AuditBody(Strict):
    """`audit`: one runner-side audit row of the closed list; the detail is checked against its action's schema."""

    action: Literal[
        "hook.denied",
        "read.session.get_context",
        "read.session.list_workgroups",
        "read.session.list_members",
        "read.session.list_tasks",
        "read.session.get_task",
        "read.session.get_task_messages",
        "read.session.get_whiteboard",
        "read.session.get_notifications",
        "read.session.get_operation",
        "read.session.list_operations",
        "read.session.get_subagent_result",
        "ping.delivered",
        "session.prompt_refused",
        "model.provider_outside_allowlist",
        "runner.inbound_rejected",
        "runner.log_rollback",
        "runner.log_fork",
        "runner.alert",
    ]
    result: Literal["ok", "denied", "rejected", "duplicate", "failed"]
    task_id: Uuid7 | None = None
    session_id: Uuid7 | None = None
    target_kind: Annotated[str, StringConstraints(pattern=r"^[a-z_]{1,32}$")]
    target_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]
    detail: dict[str, Any]

    @model_validator(mode="after")
    def _detail_of_the_action(self) -> AuditBody:
        _absent_not_null(self, ("task_id", "session_id"))
        AUDIT_DETAILS[self.action].model_validate(self.detail)
        return self


BODIES: dict[str, type[Strict]] = {
    "event": EventBody,
    "session_transition": SessionTransitionBody,
    "vm_state": VmStateBody,
    "operation_transition": OperationTransitionBody,
    "usage": UsageBody,
    "quota": QuotaBody,
    "metrics": MetricsBody,
    "audit": AuditBody,
    "wrap": WrapBody,
    "runtime": RuntimeBody,
}


def parse(item_kind: str, body: dict[str, Any]) -> Strict:
    """The checked body of an item (`HubError("request_invalid")` when it breaks the contract of its kind)."""
    try:
        return BODIES[item_kind].model_validate(body)
    except ValidationError:
        raise HubError("request_invalid") from None
