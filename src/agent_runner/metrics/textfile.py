"""Sink A: `<TEXTFILE_DIR>/agent_runner.prom` for the host's node_exporter textfile collector (08-monitoring.md
section 3, ADR-018).

`metrics_of(snapshot, spend=None)` builds every metric of the table of section 3 with `# HELP` and `# TYPE`;
`render(metrics)` writes the exposition text (label values escaped: backslash, double quote, newline); `write_atomic`
writes it like the infrastructure's textfile scripts: a temp file `.agent_runner.prom.XXXXXX` (not ending in `.prom`, so
the collector never reads half a file) in the same directory, the complete body in one write, mode 0644, `rename`.

Labels are IDs only (`vm`, `wg`, `session`, `state`, `direction`): no names, e-mails or other personal data. A VM
without a workgroup (pool) has an empty `wg`. Series of a session exist while it has not ended and for 15 min after
(`session.RETENTION_S`); a value that was not measured is left out rather than reported as 0.

The total of the VM slots is `agent_runner_device_vm_slots`, not the `agent_runner_device_vm_slots_total` of the table:
`_total` is reserved for counters and `promtool check metrics`, which section 7 runs on this file, refuses a gauge with
it (decisions/RUN-15.md).

`agent_runner_device_model_spend_usd{window}` (platform key, daily) comes from the spend query of RUN-12
(`quota.openrouter.spend_metrics`, passed by the sampler as `spend`); without fresh spend data the metric is left out
(decisions/RUN-15.md, decisions/RUN-12.md).

The operator's subscriptions (KOR-23, ADR-052; only while `OPERATOR_SUBSCRIPTION_DIR` is set, passed by the sampler as
`operator`): `agent_runner_operator_subscription_files{state}` (usable, blocked, exhausted - used up, KOR-50 -,
deferred, ignored), `agent_runner_operator_subscription_sessions{file}` (the label is the file name without
`.token`, chosen by the operator; never a token or an organization) and
`agent_runner_operator_subscription_newest_change_age_seconds` (left out when no file was seen).

The work VM slots (ADR-059 addendum 4; only on a device with work VMs, `Snapshot.workvm_slots`):
`agent_runner_workvm_slots{state}` (`held`, `free`), `agent_runner_workvm_requests_waiting`,
`agent_runner_workvm_stopped` and the capacity that admits new work VMs: `agent_runner_workvm_memory_free_bytes`,
`agent_runner_workvm_cpu_load_ratio`, `agent_runner_workvm_disk_used_bytes`; the device's admission (05-runner.md
section 3.8): `agent_runner_admission_active{kind}` and `agent_runner_admission_waiting{kind}` (`session`, `work`,
`ci`).
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from agent_runner.metrics.counters import DEVICE_COUNTERS, SESSION_COUNTERS
from agent_runner.metrics.snapshot import SESSION_STATES, VM_STATES, Snapshot
from agent_runner.model.operator import STATES as OPERATOR_STATES
from agent_runner.model.operator import Summary

FILE_NAME = "agent_runner.prom"
TEMP_PREFIX = ".agent_runner.prom."
FILE_MODE = 0o644
MIB = 1024 * 1024

Value = int | float
Labels = tuple[tuple[str, str], ...]


@dataclass
class Metric:
    name: str
    kind: Literal["gauge", "counter"]
    help: str
    samples: list[tuple[Labels, Value]] = field(default_factory=list)

    def add(self, labels: Labels, value: Value | None) -> None:
        if value is not None:
            self.samples.append((labels, value))


def escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def escape_help(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n")


def format_value(value: Value) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value) if isinstance(value, int) else repr(float(value))


def render(metrics: Sequence[Metric]) -> str:
    """Exposition text; metrics of the same name are merged under the first one's HELP/TYPE, empty ones skipped."""
    merged: dict[str, Metric] = {}
    for metric in metrics:
        target = merged.setdefault(metric.name, Metric(metric.name, metric.kind, metric.help))
        target.samples.extend(metric.samples)
    lines: list[str] = []
    for metric in merged.values():
        if not metric.samples:
            continue
        lines.append(f"# HELP {metric.name} {escape_help(metric.help)}")
        lines.append(f"# TYPE {metric.name} {metric.kind}")
        for labels, value in metric.samples:
            text = ",".join(f'{key}="{escape_label(item)}"' for key, item in labels)
            lines.append(
                f"{metric.name}{{{text}}} {format_value(value)}" if text else f"{metric.name} {format_value(value)}"
            )
    return "\n".join(lines) + "\n"


def write_atomic(directory: Path, text: str) -> Path:
    """Write `text` to `<directory>/agent_runner.prom` atomically (module docstring); returns the target path."""
    target = directory / FILE_NAME
    descriptor, temporary = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=directory)
    data = text.encode("utf-8")
    try:
        try:
            if os.write(descriptor, data) != len(data):
                raise OSError("short write to the textfile")
            os.fchmod(descriptor, FILE_MODE)
        finally:
            os.close(descriptor)
        os.replace(temporary, target)
    except BaseException:
        os.unlink(temporary)
        raise
    return target


def _gauge(name: str, help_text: str) -> Metric:
    return Metric(name, "gauge", help_text)


def _device(snapshot: Snapshot, spend: Mapping[str, float] | None) -> list[Metric]:
    device, capacity = snapshot.device, snapshot.device.capacity
    out: list[Metric] = []

    def single(name: str, help_text: str, value: Value) -> None:
        metric = _gauge(name, help_text)
        metric.add((), value)
        out.append(metric)

    single("agent_runner_textfile_timestamp_seconds", "Unix time this file was written.", round(snapshot.wall, 3))
    single("agent_runner_device_up", "1 while the runner daemon samples.", 1)
    single(
        "agent_runner_device_hub_connected", "1 while the device WebSocket to the hub is open.", device.hub_connected
    )
    single("agent_runner_device_sync_fatal", "1 when pushing to the hub stopped for an operator.", device.sync_fatal)
    single("agent_runner_device_outbox_backlog", "Outbox items not yet acknowledged by the hub.", device.backlog)
    single(
        "agent_runner_device_outbox_held", "Outbox items waiting in scope queues (no key or log head yet).", device.held
    )
    single("agent_runner_device_outbox_held_scopes", "Scopes whose outbox queue waits.", device.held_scopes)
    single("agent_runner_device_vm_slots", "VM slots of the device memory budget (total).", capacity.slots_total)
    single("agent_runner_device_vm_slots_free", "Free VM slots.", capacity.slots_free)
    single("agent_runner_device_vm_memory_budget_bytes", "Memory budget for VMs.", capacity.budget_mib * MIB)
    single("agent_runner_device_vm_memory_used_bytes", "VM memory in use from the budget.", capacity.used_mib * MIB)
    single("agent_runner_device_pool_ready", "Warm pool VMs ready for assignment.", capacity.pool_ready)
    single("agent_runner_device_capacity_low", "1 when less than 10 % of the VM slots are free.", capacity.capacity_low)
    for counter in DEVICE_COUNTERS:
        metric = Metric(counter.name, "counter", counter.help)
        metric.add((), device.counters[counter.name])
        out.append(metric)
    if spend is not None:
        metric = _gauge("agent_runner_device_model_spend_usd", "Model spend of the platform key in USD.")
        for window, value in sorted(spend.items()):
            metric.add((("window", window),), value)
        out.append(metric)
    return out


def _workvm_slots(snapshot: Snapshot) -> list[Metric]:
    """The work VM slots (ADR-059 addendum 4), only on a device with work VMs."""
    slots = snapshot.workvm_slots
    if slots is None:
        return []
    held = _gauge("agent_runner_workvm_slots", "Work VM slots by state (held by a VM that is not stopped, free).")
    held.add((("state", "held"),), slots.held)
    held.add((("state", "free"),), slots.free)
    waiting = _gauge("agent_runner_workvm_requests_waiting", "Requests waiting for a work VM slot.")
    waiting.add((), slots.waiting)
    stopped = _gauge("agent_runner_workvm_stopped", "Stopped work VMs (disk kept, no slot held).")
    stopped.add((), slots.stopped)
    memory = _gauge(
        "agent_runner_workvm_memory_free_bytes",
        "RAM a new work VM may use (above the reserve, less what every VM holds).",
    )
    memory.add((), slots.memory_free_mib * MIB)
    cpu = _gauge("agent_runner_workvm_cpu_load_ratio", "Host CPU load per core that admits new work VMs.")
    cpu.add((), round(slots.cpu_load, 3))
    disk = _gauge("agent_runner_workvm_disk_used_bytes", "Disk allocated to work VMs and their pool.")
    disk.add((), slots.disk_used_gib * 1024 * MIB)
    active = _gauge("agent_runner_admission_active", "Active instances per kind in the device's admission.")
    queued = _gauge("agent_runner_admission_waiting", "Requests waiting per kind in the device's admission.")
    for kind, (count, waits) in sorted(slots.kinds.items()):
        active.add((("kind", kind),), count)
        queued.add((("kind", kind),), waits)
    return [held, waiting, stopped, memory, cpu, disk, active, queued]


def _vms(snapshot: Snapshot) -> list[Metric]:
    gauges = {
        "probe_ok": _gauge("agent_runner_vm_probe_ok", "1 when the guest of a VM that should run answered."),
        "state": _gauge("agent_runner_vm_state", "1 for the current state of the VM."),
        "cpu": _gauge("agent_runner_vm_cpu_ratio", "CPU of the VM in cores."),
        "mem": _gauge("agent_runner_vm_memory_bytes", "Host memory of the VM (VZ and hostagent resident)."),
        "mem_limit": _gauge("agent_runner_vm_memory_limit_bytes", "Memory configured for the VM."),
        "disk": _gauge("agent_runner_vm_disk_used_bytes", "Allocated bytes of the VM disk file."),
        "disk_limit": _gauge("agent_runner_vm_disk_limit_bytes", "Size of the VM disk."),
        "uptime": _gauge("agent_runner_vm_uptime_seconds", "Seconds since the VM process started."),
        "rx": Metric("agent_runner_vm_network_receive_bytes_total", "counter", "Bytes received by the VM."),
        "tx": Metric("agent_runner_vm_network_transmit_bytes_total", "counter", "Bytes sent by the VM."),
        "sessions": _gauge("agent_runner_vm_sessions", "Sessions of the VM by state."),
    }
    for vm in snapshot.vms:
        labels: Labels = (("vm", vm.vm_id), ("wg", vm.wg_id or ""))
        if vm.probe_ok is not None:
            gauges["probe_ok"].add(labels, vm.probe_ok)
        for state in VM_STATES:
            gauges["state"].add((*labels, ("state", state)), state == vm.state)
        gauges["cpu"].add(labels, vm.cpu_ratio)
        gauges["mem"].add(labels, vm.memory_bytes)
        gauges["mem_limit"].add(labels, vm.memory_limit_bytes)
        gauges["disk"].add(labels, vm.disk_used_bytes)
        gauges["disk_limit"].add(labels, vm.disk_limit_bytes)
        gauges["uptime"].add(labels, vm.uptime_seconds)
        gauges["rx"].add(labels, vm.net_rx_bytes)
        gauges["tx"].add(labels, vm.net_tx_bytes)
        for state in SESSION_STATES:
            gauges["sessions"].add((*labels, ("state", state)), vm.sessions.get(state, 0))
    return list(gauges.values())


def _sessions(snapshot: Snapshot) -> list[Metric]:
    gauges = {
        "cpu": _gauge("agent_runner_session_cpu_ratio", "CPU of the session unit in cores."),
        "mem": _gauge("agent_runner_session_memory_bytes", "Memory of the session unit (cgroup memory.current)."),
        "mem_limit": _gauge("agent_runner_session_memory_limit_bytes", "Memory limit of the session unit."),
        "workspace": _gauge("agent_runner_session_workspace_bytes", "Bytes of the files in the session workspace."),
        "runtime": _gauge("agent_runner_session_runtime_seconds", "Seconds since the session was created."),
    }
    counters = [Metric(counter.name, "counter", counter.help) for counter in SESSION_COUNTERS]
    for session in snapshot.sessions:
        labels: Labels = (("vm", session.vm_id), ("session", session.session_id))
        values = session.values or {}
        gauges["cpu"].add(labels, session.cpu_ratio)
        gauges["mem"].add(labels, values.get("mem_current_bytes"))
        gauges["mem_limit"].add(labels, values.get("mem_max_bytes"))
        gauges["workspace"].add(labels, values.get("workspace_bytes"))
        gauges["runtime"].add(labels, session.runtime_seconds)
        for spec, metric in zip(SESSION_COUNTERS, counters, strict=True):
            metric.add((*labels, *spec.labels), values.get(spec.field))
    return [*gauges.values(), *counters]


def _operator(summary: Summary) -> list[Metric]:
    files = _gauge("agent_runner_operator_subscription_files", "Token files of the operator's subscriptions by state.")
    for state in OPERATOR_STATES:
        files.add((("state", state),), summary.states[state])
    sessions = _gauge(
        "agent_runner_operator_subscription_sessions", "Sessions bound to a token file of the operator's subscriptions."
    )
    for name, count in sorted(summary.sessions.items()):
        sessions.add((("file", name),), count)
    age = _gauge(
        "agent_runner_operator_subscription_newest_change_age_seconds",
        "Seconds since the newest change of a token file of the operator's subscriptions.",
    )
    age.add((), None if summary.newest_age_s is None else round(summary.newest_age_s, 3))
    return [files, sessions, age]


def metrics_of(
    snapshot: Snapshot, spend: Mapping[str, float] | None = None, operator: Summary | None = None
) -> list[Metric]:
    """Every metric of 08-monitoring.md section 3 for `snapshot` (device, work VM slots, VMs, sessions in that order),
    then the operator's subscriptions when they are configured."""
    found = [*_device(snapshot, spend), *_workvm_slots(snapshot), *_vms(snapshot), *_sessions(snapshot)]
    return found if operator is None else [*found, *_operator(operator)]
