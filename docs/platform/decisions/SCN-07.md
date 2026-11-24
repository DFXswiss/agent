# SCN-07 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent` (INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent` repository, then the note of the former `agent-core` repository.

## SCN-07 decisions (agent repository: M2e scenarios E2E-15/E2E-17)

The scenarios themselves live in agent-core (`e2e/platform/specs/e2e-15-audit.spec.ts`,
`e2e-17-monitoring.spec.ts`, decisions in agent-core `docs/platform/decisions/SCN-07.md`). This repository gets one
component fix that E2E-17 needs.

1. **Offline mode of the monitoring test stack (`deploy/monitoring/test-stack/`).** E2E runs are hermetic
   (07-testing.md §5.2): the orchestrator's egress sampler watches every Lima host agent of a run, and all guest TCP
   traffic of a vz VM leaves through it. The MON-01 stack downloaded its three archives from inside the VM, and a
   plain Ubuntu 24.04 cloud image contacts Canonical at boot (measured on the runner device: an HTTPS connection to
   the pollinate entropy server within the first five seconds, besides NTP). Both would be egress violations in
   E2E-17. Fix: `fetch.sh DIR` downloads the pinned archives once on the host, before the run (like the shared
   golden cache that `golden-cache.sh ensure` builds before every E2E run); `up.sh --archives DIR` mounts DIR
   read-only, `provision.sh` copies the archives from there through the new `archive.sh` (one implementation of the
   cache check for host and VM, never a download in offline mode) and a Lima boot script, enabled by the parameter
   `AGENT_MONITORING_OFFLINE=1`, drops every packet the guest opens itself except loopback, name lookups and NTP
   from the first boot on (cloud-init `bootcmd`, before `network-online.target`); answers on connections the host
   opened (Lima's SSH, port forwards) pass. No address literal is needed (the repository allows none but loopback and
   documentation networks). `check.sh` fails when the drop is missing (fail closed). Measured on the runner device:
   create, boot, provision and three further minutes with only NTP and DNS leaving the host agent.
   Without `--archives` the stack behaves exactly as before (the manual MON-01 flow).
2. **Firewall rather than masking services.** Masking pollinate, snapd, motd-news, apt and fwupd timers would have
   to track every image update; one output policy is independent of the image's services and also covers the
   Prometheus/Grafana binaries (Grafana's update checks are already disabled by `provision.sh`).
3. **Base image.** `limactl create` uses Lima's cached `template:ubuntu-24.04` image; if the cache is empty Lima
   downloads it during the run. That download runs under the run's egress sampler (a descendant of the route's
   command) and fails the run visibly; the runner device has the image cached since MON-01.

## SCN-07 – decisions (M2e scenarios: E2E-15 audit and read log, E2E-17 monitoring)

### Monitoring test stack in E2E-17
1. **Hermetic stack.** The run's egress sampler watches every Lima host agent whose instance carries the run prefix,
   and a vz guest's TCP traffic leaves through it. The MON-01 stack downloaded its archives inside the VM and a plain
   Ubuntu image contacts Canonical at boot (pollinate). The agent repository gets an offline mode
   (`up.sh --archives DIR`, `fetch.sh`, a boot-time egress drop; agent `docs/platform/decisions/SCN-07.md`). The
   route `POST /monitoring/stack/up` uses `monitoring-archives/` next to the agent checkout on the runner device
   (`~/wp/<tag>/monitoring-archives` in the work-package layout) and answers `409` with the `fetch.sh` command when it
   is missing: the archives are fetched once before the run, like the shared golden cache, never during it.
2. **The VM carries the run prefix** (`<prefix>e2e-monitoring`): the cleanup of the run deletes it with the other
   instances when the spec cannot (`/monitoring/stack/down` runs in a `finally`).
3. **No host port.** Prometheus and Grafana stay on the VM loopback; the routes query them inside the VM through
   `limactl shell … curl` (`/monitoring/stack/prometheus`) and `check.sh`. No port of the work-package range is used,
   so `PORT_OFFSETS` (a list other packages extend) stays untouched.
4. **Textfile of the runner.** E2E runners have no `AGENT_RUNNER_TEXTFILE_DIR`; the route sets
   `<AGENT_RUNNER_HOME>/textfile` and restarts the runner once (same home, like `/runners/idle`) before the session
   starts. The stack's Prometheus adds `server="runner-device"`, the label the host Alloy adds in the fleet.
5. **Lowering the capacity** (`POST /runners/capacity`): `AGENT_RUNNER_MEMORY_RESERVE_MIB = physical RAM − n ×
   VM_MEMORY_MIB`, so the device offers exactly the `n` slots its VMs occupy and `capacity_low` turns 1 through the
   runner's own computation (no fake values); the runner restarts once, `{"slots": null}` restores the default.
6. **"`promtool test rules` fires" with the live value.** Besides `check.sh` (promtool on the checkout's synthetic
   rule tests and Grafana evaluating the rule as pending/firing), `/monitoring/stack/live-rule-test` takes the live
   `agent_runner_device_capacity_low` value from Prometheus, holds it for the rule's `for` window and lets promtool
   check that `agent-runner-capacity-low` is silent one minute before and firing at the end (the convention of
   `alerting/agent-platform.test.yaml`).
7. **Web = hub API.** Values move every 15 s, so the spec polls until the screen text equals the text formatted from
   the hub API's value read at the same moment (the web's own formatters from `web/src/core/monitoring/units.ts`):
   memory, disk, uptime, state and free capacity of the device, memory, disk and uptime of the VM, CPU, memory and
   workspace of the session. A session's CPU is a counter delta; its first sample has none, so the spec waits for one.
8. **Child session.** The parent session requests a child through `subagent.request` (the pattern of E2E-24) and waits
   for it with `get_subagent_result`; E2E-17 asserts the child on every level with its own samples (hub API, its own
   item under the VM on the screen, Prometheus series with its `session` label) and carries R-13.4 (01-requirements.md
   lists E2E-17 for it; SCN-04's E2E-24 leaves this assertion to SCN-07). It needs SCN-04's agent fixes (item 14).

15. **Runner restarts only with an idle pool.** The textfile setting and the capacity change restart the runner. A
    restart while a pool VM is being started (`runner.vm.start_failed … code=lima_failed` in the runner log) once left
    that VM booting outside the runner's control, and it reached Ubuntu and npm mirrors (egress check red in a
    development run). The routes restart the runner only after the run's Lima instances kept their names and states
    for 30 s (`capacity.restart_quietly`; the pool's `ready` count once passed while a refill clone was booting), and
    the spec additionally waits for a full pool before each restart and after restoring the capacity.
    The runner side (an interrupted start must not leave a running, unhardened VM) is reported for a runner package
    (report SCN-07); it is not changed here. Superseded by KOR-11 (decision 14): the routes restart the runner at
    once.

### Audit and read log in E2E-15
9. **Read-log placement (hub fix, `agent_hub/http/readlog.py`).** Read-log rows took org, workgroup and task only from
   path or query parameters of those names; most reads name a task, a session, an operation or an event scope only
   (`/v1/tasks/{task}/operations`, `/v1/operations/{id}`, `/v1/scopes/{scope}/events`), so they had no `org_id` and
   never appeared in the organization's audit view (WEB-12 report). The writer now places a *successful* read: the
   missing organization, workgroup, task and session come from the hub's row of the most specific ID (session, task,
   workgroup, else the operation or event scope of the target). Refused reads stay unplaced (no foreign actor IDs or
   probe rows in another organization's audit), and a row whose named IDs contradict the hub's row (an audit filter of
   one organization naming another organization's session) stays as named, so no read row ever carries IDs of another
   organization that the reader did not name. This keeps 03-api.md §1/§4.11 as they are (reads of the org visible to
   its admins and to members by workgroup), hence no ADR.
10. **Tamper check on a copy** (`POST /hub-db/audit-tamper`): `pg_dump` of the hub database into a fresh database of
    the same test cluster, the copy's append-only triggers disabled (they fire in every replication role), one row's
    `result` flipped, `agent-hub audit verify` against the copy (exit 1, `ok:false`) and against the original
    (exit 0); the copy is dropped. The live database is never written.
11. **Session in the audit screen.** 06-web.md §9.2 lists the row contents (actor, action, result, target names,
    event link) and a session *filter*, no session column. The spec checks the session of every row through the API
    and compares the rows the screen's session filter shows with the API's rows for that filter (the screen's own
    `read.org.audit` rows, written while it loads, aside); the other fields are checked on the screen.
12. **Change rows.** Every audit row with an `event_id` is an `event.append` row whose target is that event; the spec
    decrypts every one of the org's change events as the admin (org and workgroup scope, all pages, terminal events
    asked for explicitly) - the scope comes from the row's `detail.scope` (`wg.profile` lives in the org scope although
    its row names the workgroup).
13. **Operations without GitHub.** The turn uses `task.set_title` (succeeded, device-authored `task.meta`) and an
    unknown type (`rejected`, `unknown_type`); GitHub operations are E2E-21's. The whiteboard read is the org board the
    admin writes through the web.

### Dependencies
14. E2E-15 and E2E-17 need three agent fixes of SCN-04 (MCP tool schemas in the golden image, the `platform.read` ref the hub
    requires, the model double's answer to the runner's `/v1/key` spend poll). They are SCN-04's, not duplicated here;
    the acceptance runs use the integration branch after SCN-04's merge.
