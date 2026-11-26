"""Reconcile step `vm_switch` (order 85, after the sessions step; ADR-093 section 5): the desired state names the VMs of
this device a member asked to switch to the newest golden now (`vm_switch: [vm_id]`, malformed entries ignored). They
are handed to the switch (`GoldenSwitch.request`), which wakes task `vm-pool`; the switch applies its own rules
(05-runner.md section 3.6) - a request never moves a VM with a running session or without complete checkpoints.

A desired state that names `vm_switch` at all comes from a hub that knows the switch's status fields (`upgrade`,
`replaced_by` of `vm_state`): only then the runner sends them (`hub_reads_status`). One whose `vm_upgrade_version` is 2
or later also knows the hold reason `checkpoint_impossible` and its `checkpoint_at` (`hub_reads_impossible`, HOT-24);
`vm_switch_at` gives the time of each request (a request covers the holds the device reported before it).
"""

from __future__ import annotations

from agent_runner.reconcile.engine import Run
from agent_runner.registry import ReconcileStep
from agent_runner.vm.golden_switch import request_times, vm_ids
from agent_runner.vm.tasks import manager_for

FIELD = "vm_switch"
TIMES = "vm_switch_at"
VERSION = "vm_upgrade_version"
IMPOSSIBLE_VERSION = 2


async def step(run: Run) -> None:
    desired = run.desired
    if desired is None:
        return
    switch = manager_for(run.context).golden_switch
    switch.hub_reads_status = FIELD in desired
    version = desired.get(VERSION)
    switch.hub_reads_impossible = isinstance(version, int) and version >= IMPOSSIBLE_VERSION
    await switch.request(vm_ids(desired.get(FIELD)), request_times(desired.get(TIMES)))


STEPS = [ReconcileStep(85, "vm_switch", step)]
