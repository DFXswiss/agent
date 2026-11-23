# HOT-02 – False `log_chain` block and hanging org creation in the browser suite

## Case A: false `log_chain` block of a healthy org

**Cause.** `GET /v1/orgs/{org}/log` read the org row (the served `head`) and then the entries in two statements of a
READ COMMITTED transaction. An append that committed between the two statements was served as an entry beyond the
served head (a recorded failure: `?after=3` answered `entries: [seq 4]`, `head: {seq: 3}` while the admin granted a
workgroup to a new member). The browser verified the entries to head 4, compared it with the served head 3 and
correctly refused the page as a broken chain, which blocks the org. `GET /v1/platform/log` had the same order (head,
then entries) and the same race. The device reads (`GET /v1/device/logs`, `GET /v1/device/platform-log`) already read
in one REPEATABLE READ transaction.

**Correction.** Both browser log reads now read head and entries in one REPEATABLE READ snapshot, as the device reads
do. The client is unchanged: every check of the membership log (L1–L10) and of the platform log stays as it is, and a
page whose entries do not end at the served head is still refused.

**Tests.** `test_a_log_read_racing_an_append_serves_entries_and_head_of_one_state` (org route and platform route)
commits an append between the head read and the entries read; red before (entries `[0, 1]` with head 0), green
after. The device test with the same race guards the existing snapshot.

The review found the same order in `GET /v1/scopes/{scope_id}/events` (events first, then the scope head): an event
committed in between raised the served head beyond the page, and the browser moved its cursor past an event it never
received (no block, a silently lost event – a plausible cause of flaky notification and whiteboard steps). The route
now reads in one REPEATABLE READ snapshot as well; `test_a_read_racing_an_append_serves_events_and_head_of_one_state`
is red before (events `[1]` with head 2) and green after. Left as they are (no browser check depends on them):
`GET /v1/platform/enrollments` (root CLI) and the `log_head` of `GET /v1/orgs/{org}` and `GET /v1/me`.

## Case B: creating an organization hangs

**Cause.** The "Create organization" dialog copied the user's display name once, when it opened, from the profiles of
the active org (`selectOwnName`). Right after sign-in under load those profiles are not read yet, so the field stayed
empty although the name arrived a moment later; the confirm button stays disabled while the display name is empty,
and a click on it waits until the test times out. The browser test fills only the organization name.

**Correction.** The display name follows the known name until the user edits the field (`typed ?? known`). An edited
field (also an emptied one) is kept.

**Tests.** The UI test "takes the own name as display name once it is known, unless the user typed one" opens the
dialog before the name is known; red before (the field stays empty), green after. A browser reproduction that delays
only the event reads of the org by 4 s kept the confirm button disabled for 15 s without the correction and created
the organization with it. The other dialogs that start from a stored value (rename a workgroup, edit a whiteboard
entry, add a device) open only from an already shown value or a verified log and need no change.

**Review.** A retry after a failed attempt must send the identical prepared org; since the field now follows the
store, the first attempt pins the display name (UI test "keeps the display name of a failed attempt for the retry").
