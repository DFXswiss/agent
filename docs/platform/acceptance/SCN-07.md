# SCN-07 – acceptance evidence (M2e scenarios: E2E-15 audit and read log, E2E-17 monitoring)

Devices by role only: **runner device** (T1: everything; T2: runner, VMs, doubles) and **second device** (T2: hub,
PostgreSQL, browser, orchestrator). Golden image: shared TEST_MODE cache of the integration state (ADR-036), warm pool;
model double on the runner device (ADR-034). Monitoring test stack: `deploy/monitoring/test-stack` of the agent
checkout in offline mode (Prometheus 3.11.0, node_exporter 1.8.2, Grafana 12.4.2, archives fetched before the run).

## E2E (07-testing.md section 2.1)

| Run | Specs | Result |
|---|---|---|
| `e2e.sh SCN-07 --topology t1 --only E2E-15,E2E-17 --repeat 3` | E2E-15, E2E-17 | 3/3 iterations green |
| `e2e.sh SCN-07 --topology t2 --only E2E-15 --repeat 3` | E2E-15 | 3/3 iterations green |

Standard end checks of every iteration: no egress violation (the stack VM included: it carries the run prefix and is
watched like every instance of the run), model double `state.ok`, runners alive, hub audit chain verifies
(`agent-hub audit verify`), VMs of the run deleted (the monitoring VM by the spec and, on failure, by the cleanup).

| Spec | Evidence |
|---|---|
| E2E-15 | The admin writes an org whiteboard entry; one turn renames the task (`task.set_title`: `operation.accepted/executing/succeeded`), reads the org whiteboard (`read.session.get_whiteboard`), is denied a Read outside the workspace (`hook.denied`, tool and rule only, no path) and requests an unknown type (`operation.rejected`, `unknown_type`). Each of these rows has org, workgroup, task, session, device, actor, time, target and result (API); on the audit screen (filter by task) each shows time `HH:MM:SS.mmm`, actor, result, workgroup, task link, device and target; the session filter shows exactly the API's rows for that session. Every change row of the org (`event.append`, `event_id` = target) references a stored encrypted event that the admin decrypts (org and workgroup scopes, terminal events included); the title event and the whiteboard entry hold their canaries; event links lead to the task or session page. Read log: `read.org.audit`, `read.task`, `read.scope.events` (placed in the org by the hub) and the session's `read.session.get_whiteboard` are rows of the org and on the screen. "Verify chain" ok; `POST /hub-db/audit-tamper`: the copy with one flipped `result` fails `agent-hub audit verify` (exit 1, `ok:false`), the original verifies (exit 0). Plaintext scan without findings. |
| E2E-17 | Parent and child session (`subagent.request`, `get_subagent_result`) on the platform device. Hub API: device, VM, parent and child with samples. Screen: device online, reachable, state, CPU, memory, disk, network, uptime, free capacity (slots, memory, warm VMs) and history (CPU chart with data table); VM with state, reachability, CPU, memory, disk, network, uptime, history; parent session with CPU, memory, workspace, task link, history; the child as its own item with its state, task link and history. Memory, disk, uptime, state and capacity texts equal the hub API's values formatted with the web's formatters. Prometheus: series of device, VM and session (parent and child) with only the documented labels plus `server`, `job`, `instance`; VM series carry the session's workgroup, session series the VM. `check.sh`: offline drop active, `promtool check metrics` on the runner's textfile, `promtool check rules`, `promtool test rules`, Grafana provisioning without errors, three dashboards with data for every panel and variable query, seven rules evaluated. Capacity lowered to the occupied slots: `capacity_low` at the hub, the badge and the warning on the screen, `agent_runner_device_capacity_low{server}` = 1 in Prometheus, Grafana's `agent-runner-capacity-low` pending/firing, `promtool test rules` with the live value fires at the end of the 10-minute window and is silent one minute before. Capacity restored, stack deleted. |

## Fixed on the way (own commits with unit tests)

- agent `deploy/monitoring/test-stack`: offline mode (archives fetched on the host, copied from a read-only mount; the
  VM's own outbound traffic dropped from the first boot on; `check.sh` fails without the drop). Before: downloads
  from inside the VM and Canonical traffic at boot - egress violations in a hermetic run.
- agent-core hub `http/readlog.py`: successful reads that name only a task, session, operation or event scope are
  placed in their organization, workgroup and task from the hub's rows, so the org's audit view lists them (R-8.9);
  refused or contradicting reads stay as named.

## Open points

- Runner: a runner stopped while a pool VM starts once left that VM booting outside its control, and it reached
  Ubuntu and npm mirrors. The E2E routes restart runners only after the run's VMs have been quiet for 30 s; the runner
  side belongs to a runner package. Superseded by KOR-11 (decision 14): the runner deletes a VM whose start was
  interrupted, and the routes restart runners at once. Residual (KOR-11 decision 14): why the leftover clone reached
  the mirrors is not known, and a runner killed after its 10 s stop timeout leaves such a VM until its next start.
