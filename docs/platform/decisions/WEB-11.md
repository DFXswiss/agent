# WEB-11 – decisions

Whiteboards on three levels, history, notification center (06-web.md sections 1, 2.4, 6, 12.4, 13; 03-api.md sections
4.4, 4.9, 5; 04-crypto.md sections 4.3, 5.12; ADR-020; KONZEPT section 12).

1. **Flows in the core.** Board locations and rights (`core/whiteboards/location.ts`), the client projection
   (`project.ts`), the line diff (`diff.ts`) and the board actions (`actions.ts`) are framework-free and covered by
   Vitest alone; so are the notification texts and actions. The notification core lives in the new directory
   `core/notifications/` (the package owns the notification area; `core/whiteboards/` would have mixed the two).
2. **The text comes only from events.** `GET /v1/whiteboards` supplies metadata (entry IDs, deleted flag, newest event
   position, paging); the board's whiteboard events are pulled once per store generation
   (`GET /v1/scopes/{scope}/events?kinds=whiteboard.*`, the task board with `task=`) and read by the event reader like
   every streamed event. New entries of other browsers arrive as `event` frames and enter the projection at once
   (live update without a whiteboard frame, which the API does not have).
3. **Client projection = hub projection.** Applied in `scope_seq` order: the first `whiteboard.entry` of an ID creates
   the entry (later ones with the same ID are ignored; one that reuses an ID claimed on another board of the scope, or
   that the hub does not list although it existed when the rows were listed – an ID reused in another scope – is a
   neutral `event_conflict` row, because the hub did not project it), edits and deletes apply only to an existing entry that is not
   deleted; `base_event_id` is carried (the newest applied event) but, like the hub, not used to refuse an edit. Every
   applied event is a version of the history.
4. **Neutral rows.** Unverifiable whiteboard events (e.g. `author_not_writer`: the reader checks the author's right at
   `lh` with the kind matrix, org level admin, below wg write) are shown with their code, attributed to the board by
   their unchecked header; locked events (no key for their epoch, e.g. history before a grant without history) as
   "cannot be decrypted"; an entry body whose `level` disagrees with the header or that is not a valid body as
   `header_invalid` (bodies are checked against the kind's schema: exact keys, text length, UUIDv7 `base_event_id`);
   rows the hub lists without any event in the store as "Decrypting…" while the pull runs and
   "not available" afterwards. The list shows the updated time (`header.ts` of the newest applied event) with the
   "edited" marker; the history shows every version's time. Pending events appear once settled (as in the chat, decision WEB-05 3).
5. **Order and paging.** Newest first by the position of the creating event (one board = one scope, so `scope_seq` is the hub's
   order); placeholders by the newest event position the hub names; ties by key. All events of a board are pulled
   anyway (the texts and histories live in them), so the rows are not paged in the UI: `loadBoard` pulls the events
   first and then lists every page of rows (200 per request); "Load more" would only have moved placeholders. The history is ascending (it is a log,
   06-web.md section 12.4) and shows absolute `HH:MM:SS.mmm` times of the header `ts`.
6. **Deleted entries stay visible** as a muted row "This entry was deleted" with the History button (KONZEPT section 12:
   full history), without Edit/Delete.
7. **Writes.** The body is sealed with the scope's current key (org key on the org level, workgroup key below; one log
   refresh when the key is missing, then `epoch_stale`), `lh` = verified head, and sent with `POST /v1/events`
   (`notify:true` only on a new entry, only when the box is checked). The rights check runs before sealing
   (`forbidden`). After `unavailable`/`rate_limited` the sealed event is kept per board, kind and entry and an identical
   request re-sends it (same IDs, 06-web.md section 12.5); any other outcome forgets it. Text: 1–65 536 code points.
8. **Rights in the UI.** Create/Edit/Delete are rendered only with the board's write right (org: admin; workgroup and
   task: `wg:write`, an archived workgroup is not writable); readers see "You have read access" and the History button.
   A board the user cannot read is "not found"; an org whose log this browser never verified stays "loading" (as the
   member screens do).
9. **Navigation.** One nav entry "Whiteboard" (`/o/:org/whiteboard`); the screen has a level bar with the organization
   and every readable workgroup (default first, then by ID). The task level is the task tab "Whiteboard" (order 40).
   The workgroup task list (WEB-04) is not changed; its board is reached through the level bar.
10. **No nav entry for `/notifications`.** The bell (header item, rendered only while unlocked) is the entry point on
    every screen size. The discovered navigation (WEB-03) shows entries without `:org` also on the sign-in page, where a
    "Notifications" link would lead anonymous visitors to a sign-in redirect.
11. **Unread count.** The bell starts from `unread_notifications` of `GET /v1/me`; a live row that is new and unread adds
    one, marking read subtracts, "Mark all read" sets 0. The adjusted count remembers the profile count it started from
    and wins while the profile reports the same number (a profile copy, e.g. after saving the language), a different
    number from the hub replaces it.
12. **Live notifications of other orgs.** The hub sends a `notification` frame only on streams of its org; rows of other
    orgs appear in the center (list) and in the badge after the next `GET /v1/me`, not as toasts.
13. **Texts.** Built client-side from the kind and names decrypted from verified events (`org.profile`, `wg.profile`,
    `member.profile`, `device.profile`, the task's `task.meta`); the names are pulled once per org/task (profiles
    shared with the task screens). Names not read yet are shown as "…" (subject: "Someone"), never as IDs. A whiteboard
    notification's level follows from the row (task → task, workgroup → workgroup, else org). Targets: the board, the
    task, the org's members (invitation accepted, rotation required).
14. **Clicks.** A click on an unread row or toast marks it read (`POST /v1/notifications/read {ids}`) and navigates; a
    failed mark does not block the navigation. Toasts: at most three, polite live region, 6 s, close button.
15. **Diff.** Own Myers implementation on lines (`\n`; common prefix/suffix cut first), no dependency; deletions before
    additions inside a change; every line carries its sign (not colour alone). The trace keeps only the band
    k ∈ [-d, d] per step (O(D²) memory); beyond `MAX_EDITS` = 2000 changed lines the middle is shown as replaced, so a
    writer cannot make the history drawer of every reader exhaust memory. Diffs are memoized per version pair.
16. **Spec locations.** `web/e2e/{whiteboard,notifications}.spec.ts` (desktop project, Pixel 7 via `test.use` like the
    shell spec); the task board needs a task and therefore the device double: `web/e2e/devices/whiteboard-task.spec.ts`
    (new file, `devices` project, decision WEB-05 16). The shared team setup is the new helper
    `web/e2e/helpers/whiteboard.ts`.
17. **Task notifications in Playwright.** `task.idle`/`task.failed` rows come from hub signals in the process that
    applies the session transition; the device double (`scripts/hub_test_director.py`) applies them in its own process
    without the notification context, so they are not produced in the web suite. Their texts and targets are covered by
    Vitest; the full chain belongs to E2E-26/SCN-06.
18. **Merged coverage.** The UI files are covered by jsdom tests (`tests/ui/{whiteboard,notifications}.test.tsx`) as
    well as by the specs: browser coverage of pages in a second context did not show up reliably in the merged report.
19. **E2E-25/26** do not exist yet (SCN-06); the browser parts are covered by the web specs above.
20. **Review follow-ups (accepted as they are).** The adjusted unread count keeps winning while `GET /v1/me` repeats
    the number it started from (decision 11); a read on another device that is exactly offset by a new row elsewhere
    can leave the badge stale until the next change – accepted, the center's list is always the hub's. Answers that
    arrive after a sign-out (store generation changed) are dropped by every board and notification action; a write
    whose answer arrives then fails with `unauthorized` and keeps nothing. Toasts pause while hovered or focused
    (WCAG 2.2.1). Reader-without-controls and notify on the task level are covered in Vitest/jsdom (the device-double
    spec has one user); the neutral `author_not_writer` row is covered with the real reader in Vitest (a forged event
    cannot be injected into the response-signed pulls in the browser). The UI reuses `screens/members/Dialog`,
    `screens/tasks/hooks` and `task-tabs/chat/Markdown` read-only (no change to those files).
