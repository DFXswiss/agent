# KOR-15 decisions (agent repository: clone step of the VM start within its budget)

Open point 4 of the performance acceptance PFM-01 (agent-core `docs/platform/performance/results.md` section 6): the
clone step of the session start (`05-runner.md` section 6.1, step 1, "clone ≤ 6 s") took 6.17–6.36 s device time
(`vm_assigning` → `vm_ready`) in the exclusive PERF-02 run, against 5.89 s in the M0 measurement
(`docs/platform/measurements/M0.md`).

1. **Decomposition (runner device, Lima 2.3.0, golden cache of the integration branch, own prefix).** `limactl clone`
   0.05–0.08 s; `limactl start` with the control-token parameter 5.8–5.9 s when the device is quiet (VM boot: kernel
   1.45 s + userspace 2.8–3.05 s by `systemd-analyze`, the rest is Lima's host agent: VZ start, SSH, requirements,
   socket forward); the guest health answers `unassigned` within 20 ms of `limactl start` returning (polled in
   parallel every 20 ms), so the runner's 100 ms health interval costs nothing; `POST /v1/assign` 0.01 s. Between
   `limactl start` returning and the first health answer, however, the runner spent 0.24–0.34 s (M0: 0.05 s).
2. **Cause: full instance listings.** `Lima.get(name)` ran `limactl list --json` over every instance of the device and
   filtered afterwards. Lima inspects each listed instance: 0.17–0.20 s with 67 instances on the device, 0.03 s for
   `limactl list --json NAME`. The clone path ran it twice (`Lima.start` refuses a missing instance first; the socket
   path needs the instance directory), and the cost grows with the number of VMs on the device – the runner's own VMs
   in production, and other work packages' VMs on the shared test device. This explains why the step got slower than
   in M0, which ran with few instances on the device.
3. **Way A (chosen): look up one instance by name.** `Lima.get` runs `limactl list --json NAME`; Lima 2.3.0 ends the
   call for a missing name with "No instance matching NAME found." / "unmatched instances" and exit 1, which the
   existing rule maps to `LimaNotFound`, and `get` returns None as before (recorded in
   `tests/platform/runner/vm/fixtures/list-missing.txt`); an instance directory without `lima.yaml` is reported by
   name like in the full listing (`status: "Broken"`, exit 0, checked on the runner device), so the removal of an
   interrupted clone's leftover in `delete` (decisions KOR-03) keeps working. No check is removed or loosened: `start` still refuses a
   missing instance, the health and assignment checks are unchanged, the golden, VM size and Lima settings are
   unchanged. Tests: `test_get_lists_only_the_named_instance`,
   `test_get_refuses_a_foreign_name_and_keeps_other_failures` and the clone-path assertion in
   `test_clone_path_when_the_pool_is_empty` (every `limactl list` of `ensure_vm` names its instance; red without the
   change). The fake `limactl` answers `list NAME` like Lima 2.3.0. The full listing remains where all instances are
   wanted (`list()`: startup reconciliation, `vm list`, `pool list`, uninstall, golden maintenance).
4. **Effect (exclusive PERF-02 on T1, 10 runs, `exclusive.json`).** Clone step device time 5.85–5.96 s (median
   5.88 s, before 6.17–6.36 s); time to working 6.44–6.57 s at the hub (before 6.78–7.14 s). The budget of section
   6.1 holds, so the specification is unchanged (way B not needed).
5. **Not taken.** `ssh.overVsock: false` for clones (Ubuntu 24.04 has systemd 255, Lima falls back to the usernet
   SSH forwarder): measured 0.5 s slower, kept at Lima's default. Starting the health polling in parallel with
   `limactl start`: the forwarded socket only exists when `limactl start` returns, no gain. Boot-time work inside the
   golden (cloud-init per instance about 1 s) belongs to the golden image, outside this package.
6. **Many clones at once (28 in PERF-05: 10.7–12.8 s), no optimisation here.** The limit is the device: 28 VMs × 4
   vCPU boot at once on 36 cores (CPU-bound kernel and userspace boot), on one APFS volume. Serial sections in the
   runner are short: the slot lock covers the capacity check and the row insert only; `limactl` runs in the default
   thread pool (32 workers on a 36-core device, enough for 28 parallel starts); per-instance locks are independent.
   The removed full listings ran twice per clone in that burst (about 60 instances each). The user sees the actual
   state during such a burst; the estimate in the burst stays the residual risk of PFM-01 decision 5.
