"""Registry of the `_total` counters of 08-monitoring.md section 3 (one place that names every counter, its help text
and where its value comes from).

Device counters are read from the daemon at each sample; the areas that count them keep the numbers themselves:

| Metric | Source |
|---|---|
| `agent_runner_device_verify_failures_total` | `orgs.audit.counters(context)["verify_failures"]` (RUN-03) |
| `agent_runner_device_metrics_dropped_total` | samples older than 48 h dropped before the push (`metrics.outbox`) |
| `agent_runner_vm_start_failures_total` | `VmEnv.start_failures` of the shared VM manager (VM-02), else 0 |
| `agent_runner_vm_placements_total{reason}` | `VmEnv.placements` of the shared VM manager (HOT-32), else 0 |
| `agent_runner_vm_room_checks_total{result}` | `VmEnv.room_checks` of the shared VM manager (HOT-32), else 0 |
| `agent_runner_operator_github_connects_total` | `github.operator.state_for(context).connects` (KOR-27) |
| `agent_runner_operator_github_connect_failures_total` | `github.operator.state_for(context).failures` (KOR-27) |

Session counters come from the guest's `GET /v1/metrics` (cgroup and journal counts of the guest, contracts/changes/
RUN-15.md); a counter the guest does not send is left out for that session.

Counters live in the daemon's memory: a restart starts them at 0 again, which Prometheus' `rate`/`increase` treat as
a counter reset.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_runner.github import operator as operator_github
from agent_runner.orgs import audit
from agent_runner.vm import tasks as vm_tasks


@dataclass(frozen=True)
class DeviceCounter:
    name: str
    help: str
    read: Callable[[Any, int], int]
    labels: tuple[tuple[str, str], ...] = ()

    @property
    def key(self) -> str:
        """The key of the value in `device_values`: the name, with the labels for a labelled series."""
        if not self.labels:
            return self.name
        return self.name + "{" + ",".join(f'{label}="{value}"' for label, value in self.labels) + "}"


@dataclass(frozen=True)
class SessionCounter:
    name: str
    help: str
    field: str
    labels: tuple[tuple[str, str], ...] = ()


def _verify_failures(context: Any, _dropped: int) -> int:
    return int(audit.counters(context)[audit.VERIFY_FAILURES])


def _dropped(_context: Any, dropped: int) -> int:
    return dropped


def _vm_start_failures(context: Any, _dropped: int) -> int:
    manager = vars(context).get(vm_tasks.MANAGER_KEY)
    return int(manager.env.start_failures) if manager is not None else 0


def _vm_count(field: str, key: str) -> Callable[[Any, int], int]:
    """A count the shared VM manager keeps by key (`VmEnv.placements`, `VmEnv.room_checks`), else 0."""

    def read(context: Any, _dropped: int) -> int:
        manager = vars(context).get(vm_tasks.MANAGER_KEY)
        return int(getattr(manager.env, field).get(key, 0)) if manager is not None else 0

    return read


PLACEMENT_REASONS = ("preferred", "room", "restart", "replaced", "new_vm", "refused")
"""Reasons of a session placement (`vm.lifecycle`, HOT-32): a preferred VM, a running VM with measured room, a
restarted or replaced stopped VM, a new VM (pool or clone), or refused (`capacity_exhausted` and other refusals)."""
ROOM_RESULTS = ("room", "full", "unmeasured", "size")
"""Results of the room check of a VM before a placement: room, full (measured), not measured (its guest did not
answer, no room), an older size (takes no new session)."""


def _operator_connects(context: Any, _dropped: int) -> int:
    return operator_github.state_for(context).connects


def _operator_failures(context: Any, _dropped: int) -> int:
    return operator_github.state_for(context).failures


DEVICE_COUNTERS = (
    DeviceCounter(
        "agent_runner_device_verify_failures_total",
        "Inbound data the runner refused after verification (events, logs, wraps, requests).",
        _verify_failures,
    ),
    DeviceCounter(
        "agent_runner_device_metrics_dropped_total",
        "Metric samples dropped because they were older than 48 h before they could be pushed to the hub.",
        _dropped,
    ),
    DeviceCounter(
        "agent_runner_vm_start_failures_total",
        "Failed attempts to clone or start a Lima VM.",
        _vm_start_failures,
    ),
    *(
        DeviceCounter(
            "agent_runner_vm_placements_total",
            "Session placements on workgroup VMs by reason (room measured on a running VM, new VM, refused, ...).",
            _vm_count("placements", reason),
            (("reason", reason),),
        )
        for reason in PLACEMENT_REASONS
    ),
    *(
        DeviceCounter(
            "agent_runner_vm_room_checks_total",
            "Room checks of running or stopped workgroup VMs before a placement by result (room, full, ...).",
            _vm_count("room_checks", result),
            (("result", result),),
        )
        for result in ROOM_RESULTS
    ),
    DeviceCounter(
        "agent_runner_operator_github_connects_total",
        "GitHub connections made with the operator's GitHub token (approved users).",
        _operator_connects,
    ),
    DeviceCounter(
        "agent_runner_operator_github_connect_failures_total",
        "GitHub connections with the operator's GitHub token that failed.",
        _operator_failures,
    ),
)

TOKENS_HELP = "Model tokens of the session (input: input and cache tokens, output: output and thinking tokens)."
SESSION_COUNTERS = (
    SessionCounter("agent_runner_session_io_read_bytes_total", "Bytes read by the session unit.", "io_read_bytes"),
    SessionCounter("agent_runner_session_io_write_bytes_total", "Bytes written by the session unit.", "io_write_bytes"),
    SessionCounter("agent_runner_session_oom_kills_total", "Processes of the session killed for memory.", "oom_kills"),
    SessionCounter("agent_runner_session_hook_denies_total", "Hook denies and blocks of the session.", "hook_denies"),
    SessionCounter("agent_runner_session_hook_errors_total", "Hook handler failures of the session.", "hook_errors"),
    SessionCounter(
        "agent_runner_session_sandbox_denials_total",
        "Failed tool calls of the session whose result names a kernel refusal (EACCES/EPERM).",
        "sandbox_denials",
    ),
    SessionCounter(
        "agent_runner_session_egress_denied_total", "Refused egress packets of the session.", "egress_denied"
    ),
    SessionCounter(
        "agent_runner_session_model_tokens_total", TOKENS_HELP, "model_input_tokens", (("direction", "input"),)
    ),
    SessionCounter(
        "agent_runner_session_model_tokens_total", TOKENS_HELP, "model_output_tokens", (("direction", "output"),)
    ),
)


def device_values(context: Any, dropped: int) -> dict[str, int]:
    return {counter.key: counter.read(context, dropped) for counter in DEVICE_COUNTERS}
