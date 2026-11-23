# KOR-17 - No false `runner.inbound_rejected` for a device request ahead of the verified log

## Cause

A new user's first device request (for example "connect GitHub" right after joining an org) can reach the platform
device before the device's copy of the verified membership log shows the user. The request service checked the user
against that copy at once (`user_principal`), refused the request as `key_unknown` (or `device_not_assigned` while the
copy did not yet show the user's workgroup), audited it (`runner.inbound_rejected`, source `request`) and asked for a
reconcile run. That run brought the log up to date, the request was offered again and handled. Result: a correct
request was reported as rejected. On the two-device topology this happened regularly; one T2 run showed the refusal,
the catch-up of the new org and the answer to the same request within about half a second, the user created about
half a second before the request. In that run the user's org was the one created at onboarding: the device did not
have its log at all yet. E2E-33 found the audit of an earlier spec on the shared device.

This is the same fault as the key wraps had (decisions/HOT-04.md): a refusal that depends only on the state of the
device's log copy was judged against a copy the device knew might be behind.

## Correction (`src/agent_runner/requests/service.py`)

1. The user check raises `Refused(code, log_dependent=True)` (`key_unknown`, `device_not_assigned` of the user). Such
   a refusal is not judged against the copy as it is: the service first runs reconcile steps 1 and 2 - it asks for
   the desired state again and verifies the platform log and the log of every org offered there up to the offered
   heads (`state.fetch_state`, `logs.sync_logs`: under the book lock, all L1-L9 checks, rollback and fork alerts) -
   and then verifies the request again from the start. Unlike the wrap refresh of HOT-04, which only refreshes the
   orgs the device acts in already, this includes an org the device has never verified: a new user's first request
   usually comes from the private org created at onboarding. (The HOT-04 refresh moved unchanged to
   `logs.refresh_acting`; the wrap step still uses it.)
2. Entitled by the refreshed logs: handled, no audit. Still refused (by the user check or by any later check, e.g. the
   signature): rejected as before - a final refusal remembered as `rejected`, a log-dependent one audited once per
   request and code and not remembered, so it can still pass once the logs entitle it later (an audited request is
   not refreshed for again; the reconcile runs move the copy on).
3. Only a refresh that started after the request was first fetched counts (fetches are numbered; a refresh covers
   every fetch before its start). Refreshes are spaced by at least `REFRESH_INTERVAL_S` (2 s, host clock): a request
   that needs one before it is due is neither handled nor audited but offered again when it is due (one timer
   releases every deferred request at its own due time; one refresh then covers all of them). A flood of forged requests of unknown users therefore
   causes at most one log refresh per interval, never one per request.
4. A refresh that failed (transport error, refusal, unusable answer, or the hub answering a state that halts the
   run, e.g. `pending_certification`) and a request still refused: neither
   handled nor audited, offered again after the interval. After `REFRESH_TRIES` (3) failed refreshes the request is
   judged against the copy alone (and audited if refused), so a hub that serves requests but withholds the logs cannot
   keep a wrong request out of the audit for good.
5. No request is handled before its user is in the verified log and this device serves the user: the second check is
   the same `verify()` on the refreshed copy.

The waiting memory is per daemon (bounded, `MAX_WAITING`, the oldest entry is dropped): after a restart a waiting
request is fetched, refreshed for and judged again. Shutdown stops the release timer. A refresh runs in the request
loop and waits for the book lock: requests of unknown users can delay the next request by one refresh at most once
per interval. A dropped or restarted entry starts its `REFRESH_TRIES` again; a hub that withholds the logs could keep
a refused request from being audited that way, but such a request is never handled either, and the hub can withhold
any request anyway.

## The other transient refusals of the module

- `time_skew` for a time more than 120 s in the future: depends on the clocks, not on the log; unchanged (audited
  once per request and code, verified again when offered again).
- `forbidden` for a follow-up whose root this daemon has not processed yet: does not depend on the log (the root's
  flow is local); unchanged.
- `device_not_assigned` because the header names another device: the hub routed a request that is not addressed to
  this device; final, unchanged.

## Other sources of `runner.inbound_rejected`

| Source | Depends on the log copy | Can a rightful item be ahead of the log | Refreshed before judging |
|---|---|---|---|
| `request` (this package) | yes (user check) | yes (new user's first request) | yes, now |
| `wrap` | yes (W2/W3) | yes (assignment and wrap in one request) | yes (HOT-04) |
| `event` (inbox) | yes (E3-E5 at `lh`) | yes (`lh` beyond the verified head) | yes (L10 refresh) |
| `event` (prompt consumers: misrouted prompt, refused delivery) | no (judged on the verified event) | no | n/a |
| `log`, `platform_log` | is the log itself | no | n/a |
| `assignment` (session owner without workgroup access) | yes | no: the desired state that names the session offers the log heads, which step 2 of the same run verifies before step 5 | n/a |
| `credential` (author not eligible: `wrap_unauthorized`, `key_unknown`) | yes | yes, for a record authored by a user or another device that joined just before; not for the records this device writes itself | no: open, see the report |
| operations consumers (`event`) | no (judged on the verified event) | no | n/a |

## Review

Independent review: one high finding (the first version refreshed only the orgs the device already acted in, so the
onboarding case of the evidence stayed unfixed) - fixed as above with a test of a user in an org new to the device,
red against the first version. Medium: the waiting memory was cleared when full (now the oldest entry is dropped); a
later deferral could be released with an earlier one and fetched again (now each is released at its own due time,
tested); the refresh blocks the request loop (accepted, bounded by the interval, see above).

## Not changed

No contract change. `docs/platform/spec/05-runner.md` section 2.4 names the refresh before judging (identical in both
repositories). The E2E specs E2E-20, E2E-21 and E2E-32 now assert that the device audits no inbound refusal during
the scenario; the comment in E2E-33 that called the audit "by design" is corrected.
