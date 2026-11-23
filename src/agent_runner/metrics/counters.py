"""Registry of the `_total` counters of 08-monitoring.md section 3 (one place that names every counter, its help text
and where its value comes from).

Device counters are read from the daemon at each sample; the areas that count them keep the numbers themselves:

| Metric | Source |
|---|---|
| `agent_runner_device_verify_failures_total` | `orgs.audit.counters(context)["verify_failures"]` (RUN-03) |
| `agent_runner_device_metrics_dropped_total` | samples older than 48 h dropped before the push (`metrics.outbox`) |
| `agent_runner_vm_start_failures_total` | `VmEnv.start_failures` of the shared VM manager (VM-02), else 0 |

Session counters come from the guest's `GET /v1/metrics` (cgroup and journal counts of the guest, contracts/changes/
RUN-15.md); a counter the guest does not send is left out for that session.

Counters live in the daemon's memory: a restart starts them at 0 again, which Prometheus' `rate`/`increase` treat as
a counter reset.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_runner.orgs import audit
from agent_runner.vm import tasks as vm_tasks


@dataclass(frozen=True)
class DeviceCounter:
    name: str
    help: str
    read: Callable[[Any, int], int]


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
)

TOKENS_HELP = "Model tokens of the session (input: input and cache tokens, output: output and thinking tokens)."
SESSION_COUNTERS = (
    SessionCounter("agent_runner_session_io_read_bytes_total", "Bytes read by the session unit.", "io_read_bytes"),
    SessionCounter("agent_runner_session_io_write_bytes_total", "Bytes written by the session unit.", "io_write_bytes"),
    SessionCounter("agent_runner_session_oom_kills_total", "Processes of the session killed for memory.", "oom_kills"),
    SessionCounter("agent_runner_session_hook_denies_total", "Hook denies and blocks of the session.", "hook_denies"),
    SessionCounter("agent_runner_session_hook_errors_total", "Hook handler failures of the session.", "hook_errors"),
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
    return {counter.name: counter.read(context, dropped) for counter in DEVICE_COUNTERS}
