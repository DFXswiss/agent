# KOR-15 decisions (agent-core repository: clone step of the VM start)

The correction itself is in the runner (agent: `docs/platform/decisions/KOR-15.md`): `Lima.get` looks up one instance
by name instead of listing every Lima instance of the device, which the clone path did twice.

1. **Way A, specification unchanged.** The exclusive PERF-02 re-run on T1 (2026-11-23, 10 runs) measured the clone
   step at 5.85–5.96 s device time (`vm_assigning` → `vm_ready`), inside the ≤ 6 s of `05-runner.md` section 6.1;
   time to working 6.44–6.57 s (estimate 8000 ms shown, every run below 10 s). The budget column, the totals of
   section 6.1, `03-api.md` section 4.6.1 and the scheduler's 8000 ms clone estimate stay as they are; no row in
   `spec-deviations.md`.
2. **`performance/results.md`.** Only the PERF-02 rows of section 3.1, the sentence on the clone step below them and
   open point 4 of section 6 are updated with the re-run; the PERF-01 rows and the other sections keep the PFM-01
   values (KOR-13 owns the rest of the document).
3. **Many clones at once stay open as a number with its cause.** 10.7–12.8 s for 28 parallel clones (PERF-05): CPU
   of the device (28 × 4 vCPU booting on 36 cores); not optimised in this package.
