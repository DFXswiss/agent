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
   Teaching the web a new kind is a follow-up once HOT-06 fixes its name and body. A single file change stays a
   `session.file` row. The group's key is its first file's key, so a growing run keeps its row.
2. **Bounded rendering instead of a virtualizer dependency.** The timeline renders a window at the end (100 rows,
   "Show earlier items" adds 50), keeps the window's first row while items arrive and moves it to the end only when
   50 rows more than the budget are rendered; rows are `React.memo` on their content (`sameItem`), and rows off
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
7. **Other lists of the task page** are not changed here (other packages' areas); what stays unbounded there is
   listed in item 10 and in the package report, as input for KOR-35.
8. **Browser proof** (`e2e/devices/chat-volume.spec.ts`): runs in the `devices` project on desktop and, inside the
   same file, with the Pixel 7 profile (`test.use`), so the central project lists of `playwright.config.ts` stay
   untouched (no iPhone 15 run). File scenario: 5 000 `session.file` with patches, an ordinary item after every 10
   files (1 000 rows, so the window is exercised), live burst and history. Bounds: fewer than 4 000 elements on the
   whole page (window of 100–150 rows × ~20 elements plus the app shell; the production tab had over 18 000 after
   15 s), frame gap below 500 ms during the burst, long-task share of the burst below 50 %, at most 300 ms of long
   tasks in a quiet 2 s window afterwards, the composer shows typed text within 2 s and the page scrolls. Measured
   values: package report.
9. **Review findings not changed (low):** focus after a "show more" button disappears falls back to the page; a
   single file row that becomes a group, or a group whose first file the store evicted, remounts and closes an open
   patch; buttons "Show files"/"Show changes" carry no path in their accessible name (the path is the text right
   before them). The phone run is the Pixel 7 profile in the `devices` project (above), not iPhone 15.
10. **Follow-up after the emergency release (every chat kind in large numbers).** The owner demanded that no kind of
    event in large numbers may freeze the page, not only file changes. Added: prompts and the tool summary line are
    bounded too (20 000 characters with "Show more"; 300 characters per summary part, the expanded view has the full
    input); a tool row renders its expanded part only while open (a closed `<details>` kept megabytes of input/output in
    the DOM); messages and tool inputs/outputs show 20 000 characters at a time (`ui/chat-items/file/Clipped.tsx`, test
    ID `chat.more`, which also replaces `chat.file.patch.more`); the window shrank from 150/100 to 100/50 rows because
    150 Markdown rows reached 6 300 elements and moving the window mounted 100 messages in one frame (583 ms gap). A
    second scenario of the browser proof (`mixed`: 1 500 long Markdown messages, 1 500 tool calls with 8 KB input and
    output, 1 000 subagent messages, and one tool call at the contract's maximum of 64 KiB, opened in the browser and
    clipped) runs on desktop and phone; its bounds are 9 000 elements (up to 150 Markdown rows of ~45 elements plus the
    shell) and 1 000 ms frame gap (see item 11), with the same 50 %
    burst share and 2 s input bound. Browser find-in-page reaches only what is rendered: rows in the window, open tool
    rows and patches, the first 20 000 characters of long text (a closed tool row no longer holds its input and output
    in the DOM). Not covered here (follow-up KOR-35): the Operations tab renders every loaded and every live operation
    row (50 per page, unbounded with "More" and live rows), the Sessions tab renders the whole session tree (breadth
    unbounded), the terminal replays the whole history into an xterm with 100 000 lines of scrollback (canvas, no DOM
    rows, memory grows with the recording), and the store keeps up to 5 000 decrypted bodies per scope.
11. **Frame-gap bound of the rich scenario: 1 000 ms.** A batch of 500 events (250 messages and 250 tool calls, as
    the hub delivers them in one push) moves the window, so up to 100 new Markdown and tool rows mount in one frame.
    Measured in the coverage build (instrumented JavaScript): 417 ms on desktop and 650 ms on the phone profile on an
    idle test device, 750 ms on desktop while other gate runs loaded the device, which hit the earlier 750 ms bound.
    The bound guards against a regression to whole-list renders (multiple seconds) without failing on a shared
    device; the 50 % long-task share and the 2 s input bound stay. Rendering the rows at transition priority
    (`useDeferredValue`) was tried and dropped: every store write is a synchronous update and restarts the pending
    render, so during a steady stream the new rows lagged by up to 5 s, and the chat briefly showed its empty text
    for a large history.
