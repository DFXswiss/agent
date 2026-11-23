# Decisions of ACC-M1 (acceptance of milestone 1)

1. **`agent-hub audit verify` as an operator command.** `07-testing.md` §2.1 requires every E2E run to end with "hub
   audit chain verifies"; the orchestrator calls `agent-hub audit verify`, but no package owned that command (HUB-16
   added only `GET /v1/orgs/{org}/audit/verify`, which needs an admin's passkey session). Without it every run only
   noted "not available", so the M1 E2E check was incomplete. The command lives in the audit area
   (`audit/cli.py:register`, discovered), reuses `agent_hub.audit.query.verify` unchanged and prints the same JSON as
   the API. Exit codes: 0 ok, 1 chain or checkpoint broken, 2 configuration, 3 database unreachable (so an outage is
   never reported as a broken chain). Like the API it passes while no checkpoint exists yet (first hour of a hub).
2. **`session.vm_id` derived at the hub.** SCN-01/SCN-02 reported that the hub's `session.vm_id` stays empty: the
   contract carries no VM in `session_transition`, and no handler filled the column. A device runs one VM per
   workgroup (`05-runner.md` §3), so `vm_state` `assigning`/`running` with a workgroup sets the VM of the device's open
   sessions of that workgroup, and a `session_transition` of an open session without a VM takes the active VM of its
   device and workgroup (either arrival order). No contract change; `03-api.md` §6.1 names the effect. The update is
   done in the outbox handler (the area that already writes session rows through `sessions.transitions`) because it is
   one statement bound to the `vm_state` item and its rights check; a separate sessions function would add no rule.
3. **Early heartbeat after a VM change.** The hub's start estimate (`03-api.md` §4.6.2: 3000 ms with `pool_ready > 0`,
   8000 ms without) read a capacity up to 15 s old, so E2E-07 (a) showed 3000 ms on the clone path. The runner now
   sends one extra heartbeat 250 ms (the outbox push spacing) after a VM state change; changes in between coalesce
   into one frame, so a pool refill cannot flood the socket. `03-api.md` §6.2 names the rule.
4. **Traceability gate held at milestone 1.** After this acceptance `make platform-ci` runs `test_traceability.py`
   for milestone 1 by default (was 0), so a later change cannot silently drop an M1 requirement's test; ADR-027 §4
   reserves such changes to acceptance packages.
5. **Golden caches before E2E runs.** The orchestrator builds a missing golden image inside the hermetic run, where
   the build's package installation is refused by the egress rules (first T1 attempt: `lima_failed` at step
   `install`). Acceptance runs therefore call `tools/wp/golden-cache.sh` for the current agent state first.
6. **Attack evidence knows the guest's own records.** ATS-01 (M0) wrote the foreign-write detector before GST-05
   added the root-owned asciicast recording and capture index to every session directory; against the M1 golden 64
   of 69 checks failed on them. Root-owned files directly in a session directory (`terminal.cast`,
   `capture-index.json` and its temporary file), also in the idle victim session, and the root-owned lock files of
   `useradd` (a session start of the self-test meanwhile) count as platform writes: a session user cannot create a
   root-owned file, so the criterion keeps its strength. A test pins the names to the guest constants. Claude Code's
   transient `history.jsonl.lock` (another attack session appending its history while the snapshot is taken) is
   bookkeeping like `history.jsonl` itself.
7. **Onboarding spacing in the shared E2E fixture.** Every onboarding makes about eight anonymous requests and the hub
   allows 30 per minute and source address; in T2 all browsers reach the hub from one address, and running E2E-01,
   -06, -08 and -12 back to back (first time in one run) exceeded the limit (`429` on `login/finish`, E2E-12 timed out
   waiting for the recovery code). The hub limit is the specified product behaviour, so the suite adapts: the 15 s
   spacing that SCN-01 used for PERF now applies to every onboarding (`fixtures/platform.ts`), not a longer timeout.
8. **ADR numbers unique again.** Parallel M1/M2 packages took the same numbers in both repositories (030, 031, 035,
   037). agent-core keeps its numbers (ADR-030, -035 and -037 are quoted in migration files, whose checksums must not
   change); the agent ADRs become ADR-039 (inbox consumers), ADR-040 (founder pinning), ADR-041 (operation pipeline)
   and ADR-042 (metric naming), with every reference updated, and both repositories carry the same ADR set 001-042
   with one index (as ACC-M0 did for 028/029). Next free number: 043.
