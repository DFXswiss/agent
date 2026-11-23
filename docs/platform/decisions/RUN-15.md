# RUN-15 decisions (metric sampler device → VM → session, textfile sink, guest `/v1/metrics`)

Small decisions of the RUN-15 package (08-monitoring.md §1–§4, §7; 05-runner.md §1.3 `TEXTFILE_DIR`, §3.4, §14;
03-api.md §6.1 outbox `metrics`, §7.1 `GET /v1/metrics`; 02-data-model.md §2.8; ADR-018; ADR-027 §3, §4).

## Contract (agent-core, additive, `contracts/changes/RUN-15.md`)
1. 08 §3 exports per-session counters (`oom_kills`, hook denies/errors, egress denies, model tokens) and 08 §1 takes
   the session network value from the gateway's request/response bytes, but 03 §7.1 had none of these fields. The
   guest is the one place that sees all of them (cgroup, the hook and usage records it journals, the egress filter of
   the session user), so `session_metrics` gets optional counts `oom_kills`, `hook_denies`, `hook_errors`,
   `egress_denied`, `model_input_tokens`, `model_output_tokens`, `model_request_bytes`, `model_response_bytes`.
   Counting them in the runner was not possible without editing RUN-04's journal handlers: the journal-handler
   registry allows one handler per kind and `session.hook`/`usage`/`guest.error` are taken.
2. Definitions: hook denies = `session.hook` decision `deny` or `block`; hook errors = `guest.error`
   `hook_handler_failed` with a session (the GST-03 docstring already named this source); input tokens = input + cache
   read + cache write (the `context_used_tokens` rule of decisions/RUN-04.md item 21); output tokens = output +
   thinking. The guest counts the journal records of each session incrementally (records are pruned only 24 h after
   the ack, so nothing is missed between two samples). After a restart of the control process it counts again over
   the records the journal still holds; when records were pruned meanwhile the value is lower (a counter reset for
   Prometheus). Counts of sessions that are no longer active are forgotten.

## Guest (`agent_guest/metrics.py`, `control_routes/metrics.py`)
3. VM values come from `/proc/stat` (busy jiffies, guest time is inside user), `MemTotal - MemAvailable`,
   `statvfs("/")`, `/proc/net/dev` without `lo`, `/proc/uptime`, `/proc/loadavg`. A missing or malformed VM source
   answers `503 unavailable` (the runner reports the probe failed) instead of inventing a value.
4. The session cgroup is `system.slice/agent-session-<id>.service` (the unit name of `sandbox/unit.py`, verified on the
   kernel). Without the unit (before the agent starts, suspended) the values are 0 and the limit null.
5. IO accounting: `system.slice` delegates only `memory pids` by default and the session units set no
   `IOAccounting=`, so `io.stat` never existed (observed in the guest-ci VM: IO counters stayed 0). The collector
   turns it on for each running unit once (`systemctl set-property --runtime <unit> IOAccounting=yes`); IO before the
   first sample of a unit (≤ 15 s) is not counted. Follow-up for the owners of the unit properties (SBX-02/GST-02):
   `-p IOAccounting=yes` in `sandbox/unit.py` would count from the start; the collector then finds `io.stat` and does
   nothing.
6. Egress denies: the production table counts all session users in one rule, so the collector keeps its own
   count-only table `inet agent_metrics` (base chain priority -10 jumping for the session's UID to a chain
   `s<session id hex>` of that session, which returns for what the filter accepts and counts the rest). The chain
   belongs to the session, not to the UID: UIDs are reused (lowest free), and a new session must not inherit an
   earlier session's count (review finding). It holds neither `drop` nor a decisive `accept` (an accept in one base
   chain never overrides a drop in another), so the egress filter stays the only decision. Chains are added at
   `sandbox_ready` (`SESSION_EVENTS`, in a background thread so the session start never waits for `nft`) and on every
   sample, removed when the session is no longer active. Verified on
   the kernel: the UDP packets of a session unit to a documentation address are counted, and the filter's drop
   counter grows by at least as much (still dropped).
7. `workspace_bytes` is the GST-05 scanner walk (`WorkspaceScanner._walk`: regular files of the session user, no
   symlinks, no `.git`, ≤ 100 000 entries), cached 60 s per session (a walk every 15 s could exceed the runner's 2 s
   budget on large workspaces).
8. Only active sessions are listed (≤ 64, by ID); the runner keeps the last values of an ended session itself. The
   guest writes no `metrics` journal records (the runner pulls `GET /v1/metrics`, 08 §2), so the runner registers no
   handler for that kind.

## Runner (`agent_runner/metrics/`)
9. Probes have fixed absolute argv (`/usr/sbin/sysctl`, `/usr/bin/top`, `/usr/bin/vm_stat`, `/bin/df`,
   `/usr/sbin/netstat`, `/usr/sbin/lsof`, `/bin/ps`, `/usr/bin/nettop`) and a 5 s timeout. VM host values need three
   commands for all VMs together (`lsof -F pn` of every disk file, one `ps -p` with all PIDs, one `nettop` listing),
   so the cost does not grow with 56 VMs; `lsof` and `ps` exit 1 when one file/PID has no answer and their output is
   still used. CPU of the device: `top -l 2 -n 0 -s 1` (the first sample counts since boot).
10. Sources per VM value: CPU = guest `cpu_usage_usec` delta when two answers exist, else VZ `%cpu`/100; memory = VZ
    RSS + hostagent RSS (what the VM costs the device; the VZ footprint is a high-water mark); disk = allocated blocks
    of `<instance>/disk` (`stat`); network = `nettop` bytes of the hostagent (usernet); uptime = VZ elapsed time, else
    guest uptime. Limits: configured memory, Lima disk size.
11. Device host values go only to the hub sample: 08 §3 says the textfile does not repeat what node_exporter already
    exports. `df -k` reads the volume of `$LIMA_HOME` (default `~/.lima` of the runner user); `netstat -ib` counts
    link rows of interfaces that are up, without loopback and tunnels (tunnel traffic is counted again on the carrier).
12. `agent_runner_device_vm_slots_total` of the table is exported as **`agent_runner_device_vm_slots`** (ADR-042):
    `_total` is reserved for counters and `promtool check metrics` (Prometheus 3.11.0), which 08 §7 runs on this file,
    refuses a gauge with that suffix (exit 3). MON-01 builds its dashboards and rules on the exported name.
13. `agent_runner_vm_state` has a series per VM state (`pool`, `assigning`, `running`, `stopping`, `stopped`) with 1
    for the current one, `agent_runner_vm_sessions` one per session state (0 included), so state panels never go
    stale. `agent_runner_vm_probe_ok` exists only for VMs that should run (`pool`, `assigning`, `running`): a stopped
    VM must not raise `agent-runner-vm-probe-failed`. Pool VMs have `wg=""`.
14. Probe of a VM = its guest answered `GET /v1/metrics` within 2 s with a contract-valid body (same socket and token
    as `/v1/health`; one call instead of two). A guest answer outside the contract is refused as a whole (B4).
15. Sessions: local sessions with a VM that have not ended, plus ended ones with `ended_at` in the last 15 min
    (cardinality rule of 08 §3). A session the guest reports but the store does not know is ignored. Subagent sessions
    are sessions like any other (R-13.4). The hub gets an ended session once (its final values); the textfile keeps it
    for the 15 min. A running session the guest did not report (guest unreachable) has no values in that round:
    old values are never exported as current (review finding).
16. Hub sample fields: device `reachable` true and `state` `active` (`sync_fatal` while pushing is stopped), free VM
    slots and free VM memory budget from the capacity; VM `reachable` = probe; session disk = workspace bytes, network
    = model response/request bytes, uptime = seconds since created.
17. Sink B: the samples of every round are stored in `runner.metric_pending` (migration `0005_metric_pending.sql`,
    one row per round, plain JSON: IDs and numbers the hub receives in plaintext) and moved into the outbox while the
    device WebSocket is open and the outbox backlog is below 1000 items: oldest rows first, at most 20 000 samples per
    round, at most 1000 samples per item, rows deleted in the same transaction. Rows older than 48 h are deleted first
    and counted. The age rule must act before the outbox: its `origin_seq` is gapless (no deletes) and an unpushed item
    may already be on the way, so rewriting it could cause `origin_seq_conflict`. The table survives a daemon restart
    and is bounded by the age rule; a device that is not enrolled stores nothing. (A first version held the samples in
    memory; the review rejected it for memory growth, restart loss and one long transaction on reconnect.) The table
    is added to the schema list of `tests/platform/runner/test_store.py` like the migrations of RUN-04/05/07.
18. `agent_runner_device_hub_connected` needs to know whether the WebSocket is open; `WsClient` had no such state. A
    minimal additive attribute `WsClient.connected` (set while a socket session runs) was added to `sync/ws.py`
    (RUN-02's file, merged long ago) with assertions in its existing test – the only change outside the package's own
    paths besides the contract.
19. Counter registry (`counters.py`): device counters read where the areas already count them
    (`orgs.audit.counters(context)["verify_failures"]`, `VmEnv.start_failures`, the sink's dropped samples); session
    counters come from the guest. Counters live in memory (reset on restart).
20. `agent_runner_device_model_spend_usd{window}`: RUN-12 (spend query) is not merged. `textfile.metrics_of(snapshot,
    spend=...)` takes the value; the sampler passes none, so the metric is left out until RUN-12 wires it.
21. A failing round is logged as `runner.metrics sample_failed error=<type>`, a failing sink as
    `textfile_failed`/`outbox_failed`, `limactl list` as `lima_list_failed code=…`; never a value. The next round starts
    15 s after the previous one started.
22. The sampler fills `VmEnv.dirs` (the instance directory cache of VM-02) from its `limactl list`, so the guest
    client does not run another `limactl list` per VM; `Lima.list` returns only the runner's own instances, and Lima
    names are random and never reused, so an entry left after a concurrent delete is harmless.
23. The hub refuses a whole `metrics` item when one sample names an entity it does not know (HUB-10); the outbox
    order puts `vm_state` and session transitions before the samples of the same VM or session, and a refused item is
    `rejected`, never blocking the stream.
24. Known limit: one guest call can include two `nft` calls, a `systemctl set-property` for a new unit and a cold
    workspace walk; on a very large workspace the first call after a session start may miss the runner's 2 s limit
    and count as one failed probe (the walk is then cached for 60 s).
