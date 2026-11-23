# HOT-03 - First prompt of a task right behind a membership change

## Cause

The prompt waits for nothing the delivery could miss: once its event is stored, every routing (start, `prompt` hint,
reconcile step 60 every 30 s) records the row and the delivery sends it to a session in `prompt_delivering`. It stayed
undelivered because the device **could not read or keep acting on the event**, for two reasons that both come from
the same order - the admin's browser stores a member's `accept` (log entry n), then the task's first prompt
(`lh = n`) and, in one request, the `device_assign` of the device to the task's workgroup (n+1) together with the
device's key wrap:

1. **Wrap refused for good (fixed before this package, SCN-08 decision 3).** A reconcile run that fetched the state
   before entry n+1 and the wraps after it refused the device's wrap as `wrap_unauthorized` against entry n and
   remembered that for the daemon's life. The inbox then held the workgroup's scope forever (`key_waiting`), the
   prompt event was never stored, every routing found it `missing`, the start went without initial prompt and the
   session stayed `prompt_delivering`. This is the failure of the SCN-06 run behind decision 16 (its runner predated
   the SCN-08 correction). The wrap is now checked again once the verified logs advance.
2. **False `log_rollback` (fixed here).** A reconcile run requests `GET /v1/device/state` (step 1) and verifies the
   logs afterwards (step 2). In between, the inbox may verify newer entries of the same org: an event whose `lh` is
   beyond the verified head (for instance the `member.profile` event the new member writes right after `accept`)
   triggers the L10 refresh, which fetches whatever the hub holds by then. Step 2 then compared the state's older offered head with the advanced copy, found it
   below and treated it as a rollback (L9): the org was **stopped** (persisted), its sessions ended `not_assigned`,
   its log was no longer refreshed - the verified head the SCN-06 note saw staying behind the hub's - and no prompt
   of that org was read again. The hub never served less than the device had verified when it asked.

## Correction

- Step 1 takes the verified org heads just before it requests the state (`Run.asked_heads`, under the book lock,
  after loading the book).
- Step 2 keeps judging an offered head on its own, except when it is below the copy **only** because of entries
  verified after the request (offered `seq` at or above the head verified when asking) **and** it is a head of the
  verified history (same hash at that `seq`). Such an offer is just older than the copy; the hub's current log
  decides, exactly as in the L10 refresh (`refresh_org(..., None)`: an answer below the verified head is still a
  rollback, a continuation with another predecessor still a fork).
- Unchanged: an offer below what was verified when asking is a rollback; an offer at the head's `seq` with another
  hash is a fork; an offer that is not part of the verified history is refused; a run without step 1 judges the
  offer alone. A prompt whose `lh` is beyond the verified head is still accepted only after a successful refresh and
  the E3-E5 checks; a prompt of a non-member is still refused; a missing scope key still holds the scope.

## Tests

- `tests/platform/runner/reconcile/test_stale_offer.py`: the race itself (red without the correction: the org is
  stopped as `log_rollback`), and that a real rollback (answer below the head, offer below the head verified when
  asking) and an offer outside the verified history are still refused.
- `tests/platform/runner/prompts/test_first_prompt_orders.py`: the E2E-26 order through the real reconcile steps,
  inbox, session start and delivery against the hub double - wrap fetched before its log entry (red with the
  pre-SCN-08 wrap step), prompt after the start, prompt before the assignment, prompt hinted during the start, runner
  restart with a waiting session (new deliverer, inbox, book and engine), two prompts right after each other (order,
  exactly once), the inbox refresh racing the state (red without this correction), an unreachable pull and a prompt
  whose `lh` is beyond the verified head while the log cannot be fetched (not accepted, not lost; the session waits
  in `prompt_delivering` and gets the prompt once the log is verified).

Cause 1 is proven by its test only against the wrap step before SCN-08 (the base of this package already contains
that correction); cause 2 is red against the base. A stale offer that is not part of the verified history is still
reported as `log_rollback` (unchanged classification).

## Not changed

No contract change. The hub's state answer is not made "fresher": any snapshot can be overtaken by a concurrent
refresh of the device, so the device judges the offer against what it knew when it asked.
