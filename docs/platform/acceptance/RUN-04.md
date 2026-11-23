# RUN-04 acceptance evidence (session start in the pool VM, platform model access, journal to events)

## Unit and integration tests (`make platform-ci`)
- `tests/platform/runner/sessions/`: `test_start.py`, `test_paths.py`, `test_journal.py`, `test_service.py`,
  `test_parts.py`, `test_secrets.py`, `test_provision.py`; `tests/platform/runner/model/test_selection_platform.py`.
- Real PostgreSQL store, real sync runtime (outbox, sealer, pusher) and verified logs and keys of the RUN-03 rig;
  every outbox item is pushed to the rig's hub, which validates it against `contracts/outbox/` and verifies every
  event with the device key; guest requests are validated against `contracts/guest/`. Guestlink and VMs are injected
  (`kit.py`: fake guest with a journal, fake VMs); `test_paths.py` uses the real `VmManager` of VM-02 over fake Lima
  instances that run real guest control processes (GST-01).
- Covered:
  - every start path: `running`, `restart` (stopped VM), golden replacement, `pool`, `clone`; budget marks per step
    and over-budget flags (`StartTimer`, log lines `start_step`/`started`);
  - every failure reason of the start: `vm_start_failed` (incl. unexpected VM errors), `capacity_exhausted`,
    `guest_unreachable` (health failure, wrong/unassigned workgroup, missing token), `model_access_unavailable`
    (org device, platform device without key file, explicit preference without strategy), `sandbox_failed` (refused
    or unexpected answers of `POST /v1/sessions` and `PUT …/credential`), `agent_start_failed` (from the journal and
    for unexpected step errors); guest session stopped after a late failure; a session that ends during its start;
  - journal → transitions (`prompt_delivering`, `working` from `prompt_accepted`, `idle`, `failed` + `session.status`),
    device events of every `session.*` kind with header, epoch and decryption checked at the hub, `hook.denied`
    (tool and rule only, path only inside the encrypted event), `usage` with provider cost basis and the
    provider-outside-allow-list audit, `session.prompt_refused` audit, `guest.error`; journal order kept;
    acknowledgement only after the commit (a store failure rolls the page back, no ack, cursor unchanged);
    resumption from `guest_cursor`; rewound journal; invalid, foreign and malformed records skipped; out-of-order
    pages refused; observers;
  - reconcile step 5: start of requested main sessions of assigned workgroups with owners in the workgroup (refusal
    audited once), stop of sessions the hub stops or no longer lists, `unassigned`, interrupted starts
    (`device_shutdown`, guest stopped), unreachable VMs; stop outcomes; `guestlink` task, loop supervision with
    backoff; lock order of `records.advance` against a journal page (no deadlock);
  - model access: platform kind and catalog default, owner model, auto order, no silent fallback, discovered
    strategies, catalog parsing; platform key file rules; credential body; budget (unlimited, per org, fail closed);
  - the platform key never appears in a log line, an outbox body, a hub request or item, or any row of the runner
    schema (`test_secrets.py`, marker scan).
- Coverage of `agent_runner.sessions` and `agent_runner.model`: 100 % lines and branches.

## Real session on the runner device (`-m vm`)
Run: `AGENT_RUNNER_TEST_GOLDEN=<golden> AGENT_RUNNER_VM_PREFIX=<prefix> pytest -c tests/platform/pytest.ini
--confcutdir=tests/platform -m vm tests/platform/runner/sessions` (`test_vm_session.py`): real `limactl`, the guest
of the shared golden cache (Claude Code 2.1.330), a pool VM with the model double inside on loopback (it accepts
only requests that carry the platform key as bearer token), the runner's `SessionService` with reconcile-free
scheduling, the journal loop and the outbox push to the rig's hub.

| Run | Path | VM | vm_ready | model | sandbox | agent_starting | prompt_delivering | working |
|---|---|---|---|---|---|---|---|---|
| 1 | pool | 10 ms | 17 ms | 17 ms | 57 ms | 58 ms | 298 ms | 433 ms |
| 2 | pool | 10 ms | 14 ms | 15 ms | 55 ms | 56 ms | 289 ms | 420 ms |
| 3 | pool | 10 ms | 15 ms | 15 ms | 61 ms | 61 ms | 299 ms | 433 ms |

Times are host-monotonic from the start of step 1 (the hub round trips of a real task are not included). Every run:
transitions `vm_assigning`, `vm_ready`, `sandbox_ready`, `agent_starting`, `prompt_delivering`, `working` accepted by
the hub's contract checks; one `usage` item and two or three events (`session.hook`, `session.output`, …) pushed; all
budgets of 05 §6.1 met (pool 0.1 s, sandbox 0.3 s, TUI up ≤ 2 s after `agent_starting`, prompt accepted ≤ 0.5 s
after that); warm-pool total far below the 3 s target. All VMs of the run prefix were deleted afterwards.

After the rebase onto GST-06 (prompt delivery, Stop hook → `idle`) one more run with a golden of the merged guest:
`working` at 441 ms (pool path), followed by `idle` from the Stop hook (transitions `vm_assigning` … `working`,
`idle`), so the journal mapping of `idle` is confirmed with real Claude Code as well.

## Not available yet
E2E-06, E2E-10, E2E-11 and E2E-13 do not exist yet in `agent-core` (`e2e/platform/specs/` has the smoke spec only).
