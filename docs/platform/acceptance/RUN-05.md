# RUN-05 acceptance evidence (prompt verification, delivery, follow-ups, resume and restart)

## Unit and integration tests (`make platform-ci`)
- `tests/platform/runner/prompts/`: `test_inbound.py`, `test_delivery.py`, `test_resume.py`, `test_first.py`,
  `test_wiring.py` (kit `kit.py`).
- Real PostgreSQL store, real sync runtime and verified logs and keys of the RUN-03 rig; prompt events are sealed
  with the reference builders, served by the rig's hub (contract-checked) and verified by the real inbox; journal
  records go through the real session handlers and the prompt observer; the guest is the RUN-04 fake guest extended
  by `POST …/prompts`.
- Covered:
  - refused and never delivered: forged signature, author without right in the workgroup, author not yet a member at
    `lh`, prompt addressing a session, routings to another task / scope / `scope_seq` / a subagent session, routed
    events the inbox refused (`prompt_unverified`), stale prompts (older than one already handed over), unreadable
    prompts (empty text, missing scope key, wrong author key, foreign header) - each audited
    `runner.inbound_rejected` once per (event, session);
  - strict event order per session (routed out of order, delivered 1-2-3, each after the previous acceptance);
  - waiting while `working` and during the start, the `idle` record waking the delivery;
  - network errors: write-ahead `registered`, retry with the same `prompt_id` and capped backoff, the next prompt
    never overtaking; a `typed` prompt lost by the guest sent again after 150 s; guest refusals
    (`state_conflict`, `not_found`, `id_conflict`); VM not running or not reachable;
  - store wipe: `delivered_prompt_event_ids` adopted as `accepted` (also for rows the hub still lists as pending),
    only the rest delivered;
  - journal results: `prompt_accepted`, `prompt_refused delivery_failed` (status event; a session that never accepted
    its first prompt fails `delivery_failed` and its guest session is stopped), `agent_starting` for the initial
    prompt;
  - first prompt through the real session start (argv channel, pulled on demand, refused heads skipped, delivered list
    respected), start without a routed prompt;
  - idle → suspend after the idle time, not with open prompts, retry after refusal or unreachable guest;
  - resume of a suspended session (`resuming`, `resume_claude_session_id`, credential, then the prompt), resume
    retries and every failure reason, a failed resume leaving the prompt for the next session;
  - restart hint (`resume_from`, a first prompt that is not the task's first) through the request amender;
  - hints `prompt` and `session.control` (`interrupt`, `stop`, invalid and foreign cases), reconcile step 60, daemon
    task `prompts`, registrations and the new extension point.
- Coverage of `agent_runner.prompts` (and `agent_runner` as a whole): 100 % lines and branches.

## Real session on the runner device (`-m vm`)
Run: `AGENT_RUNNER_TEST_GOLDEN=<golden> AGENT_RUNNER_VM_PREFIX=<prefix> pytest -c tests/platform/pytest.ini
--confcutdir=tests/platform -m vm tests/platform/runner/prompts` (`test_vm_resume.py`): real `limactl`, the guest of
the shared golden cache (Claude Code 2.1.330), a pool VM with the model double inside (the follow-up step requires at
least three messages, i.e. the earlier turn, which only a resumed conversation sends), the runner's `SessionService`,
journal loop and `Deliverer` (idle time 3 s), two signed prompt events verified by the real inbox.

| Run | Transitions (pushed, contract-checked) | Claude session ID after resume | Follow-up routed → answered |
|---|---|---|---|
| 1 | … `working`, `idle`, `suspended`, `resuming`, `working`, `idle` | unchanged | 779 ms |
| 2 | same | unchanged | 730 ms |
| 3 | same | unchanged | 737 ms |

Every run: the first prompt went with the start (argv), the session became `idle` (Stop hook), was suspended by the
idle rule, the follow-up resumed it with `resume_claude_session_id` = the session's Claude session ID (`--resume`),
was typed into the resumed TUI, accepted by the hook and answered by the double's follow-up step ("3+3 is 6, after
2+2 was 4."); both delivery rows `accepted`. All VMs of the run prefix were deleted afterwards.

## Not available yet
E2E-13, E2E-14 and E2E-35 do not exist yet in `agent-core` (`e2e/platform/specs/`).
