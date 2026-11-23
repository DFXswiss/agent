# ADR-042: The VM slot total is exported as `agent_runner_device_vm_slots`

Status: Accepted (2026-11-22)

## Context
The metric table of `08-monitoring.md` §3 names the total VM slots of a device `agent_runner_device_vm_slots_total`
(type gauge). The same document (§7) requires `promtool check metrics` on the runner's textfile. Prometheus reserves the
suffix `_total` for counters: `promtool check metrics` (Prometheus 3.11.0, the version of the existing stack) reports
"non-counter metrics should not have `_total` suffix" and exits 3, so the binding name and the binding check cannot both
hold. Queries such as `rate()` over a `_total` series also suggest a counter where there is none.

## Decision
- The gauge is exported as `agent_runner_device_vm_slots` (HELP "VM slots of the device memory budget (total)");
  `agent_runner_device_vm_slots_free` keeps its name. Every other name of the table is unchanged; every `_total`
  metric of the runner is a counter.
- Dashboards, alert rules (MON-01) and any other consumer use `agent_runner_device_vm_slots`. The table of 08 §3 is to
  be read with this name.
- A unit test pins the rendered textfile to a sample on which `promtool check metrics` passed
  (`tests/platform/runner/metrics/fixtures/agent_runner.prom`).

## Consequences
The textfile passes `promtool check metrics` without disabling lint rules; MON-01 must not use the old name.

## Alternatives considered
`promtool check metrics --lint=none` (hides real naming errors of later metrics); exporting both names (duplicate
series, the old one still fails the lint).

## Deviation from DESIGN.md
None.
