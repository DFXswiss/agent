# Decisions of ACC-M2 (acceptance of milestone 2)

The same file exists in `agent-core` and `agent`.

1. **Track M2f is accepted in M3.** The smartphone track (SCN-08: E2E-29, the mobile Playwright project of every web
   scenario, PERF-03/PERF-04) was not merged when this acceptance started; by the planning decision of the
   orchestrator it is accepted together with the full matrix in ACC-M3. This acceptance covers M2a to M2e. The
   requirements that only SCN-08 proves (R-6.6; the PERF-03/PERF-04 parts of R-6.2, R-6.9, R-12.6) stay open with
   that reference; `docs/platform/acceptance/M2.md` has an "M2f" section pointing to the M3 acceptance.
2. **Traceability gate stays at milestone 1.** `test_traceability.py --milestone 2` reports 146 of 147 due
   requirements green; the one red requirement is R-6.6 (E2E-29, SCN-08). Raising the default of `make platform-ci`
   to milestone 2 would make every merge red until SCN-08 lands, so the default stays at 1 and ACC-M3 raises it.
3. **E2E-00 expects the GitHub double link.** Since SCN-04 every stack starts the GitHub double on the runner device
   (link `github`), so the smoke test's fixed link list `browser-hub, model, runner-hub` made E2E-00 red on the
   integration branch (AUD-01 C-03). The list now includes `github`; nothing else changed.
4. **The model double checks the platform key on its key document** (AUD-01 C-10). `GET /v1/key` accepted any bearer
   token. The double takes the key as an option (`--key-token-env`, `ModelDouble(key_token=...)`); when it is set, any
   other token is refused with `401 authentication_error` and recorded as a failure, so a wrong key in the runner's
   spend poll fails the scenario. The E2E orchestrator generates the run's platform key before the doubles and passes
   it to the model double through the environment (never as an argument, never journaled). Without the option the
   double keeps the former rule (any bearer token), which unit tests of other areas rely on.
5. **E2E-14 "372 of 373" is a test race, not a lost event** (AUD-01 B-02). The spec waited for `pending = 0` in the
   runner outbox, then read the outbox head and the hub's `device_stream.last_origin_seq` once. The turn can append an
   item between the two reads. The hub stores outbox items only gapless (`origin_seq_gap` refuses anything but
   position + 1, `agent_hub.outbox.process`), `pushed_at` is set only for items the hub acknowledged, and the final
   dump of the failed SCN-08 run shows the device's stream at 1121, i.e. item 373 was stored later. The spec now polls
   until the hub has stored at least the outbox head it read; the other assertions (count = max, no duplicate
   `origin_seq`, order per scope) are unchanged.
6. **`list_operations` lists the operations of the own task that this device registered** (AUD-01 B-29). `03-api.md`
   §8.1 defines the result as "operations of the own task" and ADR-024 limits session reads to the own workgroup;
   nothing in §8 or §10 asks for an org- or workgroup-wide list, so the scope is right. The only difference is a task
   that moved to another device: operations registered by the earlier device are missing from `list_operations`
   (they are listed by `get_task`, and `get_operation` answers them). This is kept as a low residual risk and named in
   the acceptance document; closing it needs a creation time in the hub's device task endpoint (RUN-08 decision 16).
7. **LIVE-01 is taken over from SCN-05.** The smoke ran green once in SCN-05 (OpenRouter/DeepSeek through the
   gateway, cost recorded). When this acceptance checked (19:50), no commit since that merge had changed the live
   path (`live_bridge.py`, `routes/model.py`, the gateway, `agent_runner.model`, `agent_runner.quota`, the hub's usage
   area), so it was not repeated (package text, item 3). Later the same evening LVT-01 (M3, final owner of LIVE-01 to
   LIVE-04) changed the live bridge and runs the live tests on its state; their acceptance belongs to ACC-M3.
8. **Attack evidence knows the temporary file of the account lock.** shadow's `commonio_lock` (used by
   `useradd`/`groupadd` when the guest adds a session user) writes `/etc/<file>.<pid>`, links it to `<file>.lock` and
   removes it. A snapshot during a concurrent session start saw `/etc/group.5699` and failed ATK-30 (hooks) once. A
   root-owned regular file `/etc/{passwd,group,shadow,gshadow,subuid,subgid}.<digits>` that appears or disappears
   between the snapshots is now the guest's, like the `.lock` files since ACC-M1; a changed existing file of that
   name, a set-ID bit, another owner, type or name stays a finding. A session user cannot create root-owned
   files in `/etc`.
9. **A red T2 run explained by a defect fixed meanwhile.** The first T2 run (E2E-26) stopped the working session after
   a false `log_rollback` of the runner (offered head 7 below the verified 8 while the inbox had verified a newer
   entry). HOT-03 fixed exactly this race and was merged during the run; the T2 runs counted here use the state with
   that fix. No change by this package.
10. **The whole T1 suite in one run is the acceptance run.** The scenario packages ran their specs alone or in small
    selections; the first run of the whole suite (all specs in file order on one stack) failed 22 of its 39 tests. The
    causes were four defects of the suite itself, each fixed in its own commit, none in the product:
    - the model double kept the credential check of the last `loadModelScenario` (E2E-16 ends with the subscription
      kind), so every later session with the platform key was refused; a generic scenario load of the model double
      now switches the check off (`fixtures/orchestrator.ts`);
    - `devicesOnline` (after a runner or hub restart) waited for every `active` device row, but an org device unpaired
      by E2E-04 keeps its row `active` at the hub (it is refused through its membership, 03 §4.5) and has no runner any
      more; the helper now waits for exactly the devices that were connected before the restart;
    - E2E-14 counted `runner.*` audit rows of the shared platform device from earlier specs; it now counts the rows of
      its own org (or none) since its start;
    - E2E-00 expected every runner to wait for its enrollment, but in file order PERF-01/-02 enroll the runner first;
      a running runner is accepted.
11. **PERF-02 inside the full suite is a load measurement, not the PERF gate.** In the first full run the clone path
    took 8.4–16.2 s (`vm_ready` 7.8–14.0 s) while the runner device carried the VMs of up to five other packages (load
    average around 20 on 36 cores). `07-testing.md` §5.3 requires an otherwise idle device; PERF-01/-02 were accepted in
    M1 and are measured again by PFM-01 in M3. This acceptance reports the values and does not change the scenario.
    The accepted whole-suite run showed the same (10.4–13.9 s at load 22–27); PERF-02 alone right afterwards passed
    5/5 (7.7–9.0 s at load 17), so the clone path itself still meets the bound. The final whole-suite run showed
    8.3–11.1 s (two of five above 10 s) at a lower device load (about 15): inside the suite the run's own VMs of the
    earlier specs (up to about 40) compete with the clones as well.
12. **ADR-045 in both repositories.** ATS-03 added ADR-045 (final attack gate hardening) only to `agent`; DOC-01's
    test `test_shared_documentation_equals_the_other_repository` then failed in both repositories. The ADR and the
    index row are copied unchanged into `agent-core`; the same change also landed from another package, so the
    integration branch carries it once.
13. **The suite fixes are tested by the suite.** The changes to E2E fixtures and specs (decisions 3, 5, 10) have no
    unit test of their own: the E2E helpers are not part of a coverage gate (`07-testing.md` §3) and their behaviour
    is only observable against a running stack. Their test is the whole-suite T1 run on the final state, which runs
    every changed helper in the order that exposed the defect. `devicesOnline` also counts, after a hub restart, only
    connections opened after the restart, so a presence row of the stopped hub instance cannot end the wait early;
    E2E-00 accepts a running runner only once the hub has an enrolled device.
14. **Two more order effects on the final state.** The whole-suite run on the rebased state (with DR-01's E2E-33)
    failed two specs, both of the suite: E2E-07 read one ready pool VM right after the pool drain, because the
    restarted runner marks the deleted instances only in its start-up reconciliation after its `enrolled=` line; the
    drain route now waits until `pool status` lists none of them and at most the requested size (unit-tested,
    `routes/pool.py`). E2E-33 expected exactly its own two orgs in the export; after the earlier specs the hub holds
    about forty, and the route exports every org (as `02-data-model.md` §3 requires), so the spec now expects every org
    of the hub, its own two among them. E2E-33 itself belongs to M3; the fix only keeps the whole suite green.
