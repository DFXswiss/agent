# HOT-05 – Chat freezes with thousands of file events

## Cause

A checkout through the platform reports one `session.file` event per file (patch = whole file). The chat rendered
every event as its own row with the full patch in a closed `<details>` (closed, but in the DOM), every event caused
two store updates (pending, settled), each store update rebuilt the timeline and re-rendered every row, and every
row's time called `Intl.DateTimeFormat().resolvedOptions()` and built three `Intl.DateTimeFormat` objects. Work per
event grew with the length of the chat (quadratic in total); the tab hung and finally crashed.

## Decisions

1. **File groups are a client-side item `file-group`** (`core/chat/files.ts`, `ui/chat-items/file-group.tsx`): two or
   more consecutive `session.file` items of one session. The kind has no dot, like `countdown`, so it can never clash
   with an event kind a newer guest introduces (a summarising event of HOT-06, whatever its name, is refused by the
   reader's kind matrix as `kind_not_allowed` and shown as the existing neutral row "Unverifiable item
   (kind_not_allowed)" – nothing breaks; a verified kind without a renderer is skipped and does not split a run).
   Teaching the web a new kind is a follow-up once HOT-06 fixes its name and body. A single file change stays a `session.file` row. The group's
   key is its first file's key, so a growing run keeps its row.
2. **Bounded rendering instead of a virtualizer dependency.** The timeline renders a window at the end (150 rows,
   "Show earlier items" adds 100), keeps the window's first row while items arrive and moves it to the end only when
   100 rows more than the budget are rendered; rows are `React.memo` on their content (`sameItem`), and rows off
   screen use `content-visibility: auto`. Measuring virtualizers need fixed or measured row heights, break the
   browser's find-in-page and the accessibility tree for rows that are not mounted, and would add a dependency to the
   pinned set; the window plus file groups bounds the DOM just as well for the chat's sizes, keeps list semantics
   (`<ol>`), keyboard order and find-in-page for everything rendered. "Jump to the latest item" (shown with the
   "Show earlier items" button) resets the window and scrolls to and focuses the last row (`tabIndex=-1`).
3. **Patches only when open, at most 20 000 characters at a time** (`ui/chat-items/file/FilePatch.tsx`): a button with
   `aria-expanded` replaces `<details>` (a closed `<details>` keeps its content in the DOM). The Playwright chat spec
   clicks `chat.file.toggle` instead of the `<summary>`.
4. **Batched store writes in the reader** (`core/store/reader.ts`): settled entries are buffered and written at most
   once per 16 ms (one frame); `ingest` and `retry` write their last batch before they return (callers that read the
   store right after keep working); the live channel passes `defer: true` and leaves the last batch to the frame
   timer, so a burst of single-event frames costs one store update per frame. A sign-out drops the buffer; an entry
   evicted meanwhile stays evicted (unchanged rule).
5. **Event time formats are cached** per locale and time zone and the time zone is resolved once per page load
   (`core/i18n/format.ts` `localTimeZone`). A time-zone change of the device while the page is open shows up after a
   reload – accepted.
6. **Memory of patches.** The decrypted bodies stay in the `events` slice (bounded by the 5 000-entry LRU per scope,
   06-web.md section 2.3); the DOM no longer holds a copy of any closed patch. Dropping and re-decrypting bodies on
   demand would need a second read path through the reader for little gain once the DOM copies are gone – not done.
7. **Other lists of the task page** (checked): the Sessions tab renders the session tree (depth ≤ 2, one row per
   session – no event list); the Operations tab loads 50 operations per page and renders operation rows, not events
   (its per-row time-zone lookup is now cheap through the cached formats; not changed: KOR area); the terminal history
   is an xterm canvas with its own scrollback (not DOM rows). None has the event-per-row pattern.
8. **Browser proof** (`e2e/devices/chat-volume.spec.ts`): runs in the `devices` project on desktop and, inside the
   same file, with the Pixel 7 profile (`test.use`), so the central project lists of `playwright.config.ts` stay
   untouched. Bounds: fewer than 4 000 elements on the whole page (window of 150 rows × ~20 elements plus the app
   shell; the production tab had over 18 000 after 15 s), at most 300 ms of long tasks in a quiet 2 s window after
   everything is shown (the page is idle, not kept busy), the longest frame gap during the live burst below 1 s (the
   page keeps painting while 5 000 events are verified, decrypted and stored), and the composer takes input within
   2 s; the burst's long-task share stays below 50 %. The spec uses an ordinary item after every 10 files (1 000
   rows), so the window is exercised too. Measured before the tightening (100 files per run): live burst max frame
   gap 33 ms (desktop) / 17 ms (Pixel 7), at most 764 elements, input 9–16 ms, 0 ms long tasks in the quiet window,
   history shown 2.3–4.2 s after sign-in.
9. **Review findings not changed (low):** focus after a "show more" button disappears falls back to the page; a
   single file row that becomes a group, or a group whose first file the store evicted, remounts and closes an open
   patch; buttons "Show files"/"Show changes" carry no path in their accessible name (the path is the text right
   before them). The phone run is the Pixel 7 profile in the `devices` project (above), not iPhone 15.
