# WEB-05 – decisions

Chat, Sessions tab, session detail with the read-only terminal (06-web.md §2.1, §2.3, §5.2, §5.3, §5.5, §10.2, §12.1,
§12.4; 03-api.md §4.4, §4.6, §5, §7.2; 05-runner.md §7; ADR-016, ADR-019).

1. **Flows in the core.** Timeline (`core/chat/timeline.ts`), link rules (`core/chat/links.ts`), chat actions
   (`core/chat/actions.ts`), the session tree (`core/sessions/tree.ts`), terminal chunks, the watcher and the playback
   clock (`core/terminal/`) are framework-free and covered by Vitest alone; the slices `chat`, `sessions` and
   `terminal` hold state. The new directories `core/chat`, `core/sessions`, `core/terminal` belong to this package.
2. **One source of session state.** Session rows and transitions stay in the `tasks` slice (decision WEB-04 19); the
   `sessions` slice only keeps the load state and the child IDs of `GET /v1/sessions/{id}`. Extra row fields
   (`parent_session_id`, `depth`, `model_id`) are read defensively from the row as received.
3. **Timeline content.** Verified events of the task except `task.meta` (title) and `session.terminal`; `session.hook`
   only with `decision: deny`; kinds without a chat-item renderer are not shown (operation cards come with WEB-09 by
   adding files). Unverifiable events are attributed to the task by their unchecked header (`task`) and shown as a
   neutral row with the code; pending and locked events appear once the reader settled them. System rows: the session
   transitions `suspended`, `stopped`, `failed` (device time `at`) and the close of the task (`closed_at`). A
   `countdown` item per main session at its creation: the live panel for the current main session (Retry for
   writers), a one-line summary for earlier ones. Order `(ts, scope_seq)`, system rows after events of the same
   millisecond, then the key.
4. **Subagent label.** Session rows carry no label; a subagent is named by its place in the tree, "Subagent 1",
   "Subagent 1.2" (creation order under its parent), in the chat (indented) and in the Sessions tab.
5. **Chat pull.** `GET /v1/scopes/{wg}/events?task=…&kinds=…` with the chat kinds (`prompt`, `session.output|tool|file|
   hook|status`, `operation.params|result`), once per store generation; live events come over the channel.
6. **Composer and controls.** Only for `task:write` (= `wg:write` of the task's workgroup); Stop (`interrupt`) while the
   main session is `working`, "End session" (`stop`) in a `<details>` menu while it is neither `stopping`, `stopped`
   nor `failed`; "Close task" with a confirmation in the task menu while the task is not closed. A failed send keeps
   the sealed event for an unchanged retry when the code is `unavailable` or `rate_limited` (the composer of the home
   screen also retries `seq_conflict`, which a prompt cannot get). Readers see "You have read access".
7. **Usage slot.** The chat renders `ui/usage/strip.tsx` (export `UsageStrip`) when WEB-10 adds it (discovered by a
   glob, so the chat needs no change); until then an empty `chat.usage` container.
8. **Markdown.** `react-markdown` with `skipHtml`, no rehype plugins; every link and image source passes
   `safeUrl` (absolute `http:`, `https:`, `mailto:` only; control characters refused); links get
   `rel="noopener noreferrer nofollow"` and `target="_blank"`. Images are rendered as links (the CSP allows no foreign
   images and a remote image would reveal the reader). No GFM plugin (tables are plain text). Styles are Tailwind
   classes on the rendered elements (an imported CSS file would be counted as an app source by the coverage merge).
   The renderer and the terminal are loaded lazily (§12.2).
9. **Watching terminals.** The live channel gets `LiveChannel.setTerminals` (newest three; a running connection is
   reopened with the new `terminal=` list, cursors stay in memory) and `AppSession.watchTerminal` (reference-counted
   per view, the active org's sessions are passed to the channel). The hub refuses the whole stream with `404
   not_found` when a watched session is not readable; the channel then drops the newest watched session (the likely
   cause), reports it with `terminals_refused {sessions}` (handled by the `terminal` slice through the frames
   registration) and reconnects at once – one session at a time, so a refused new view does not end the others; with
   no terminal left a further `not_found` is fatal as before. Opening or closing a terminal view reopens the stream
   (the query names the watched sessions; 03-api.md §5 has no other way): the banner briefly leaves "Live", the
   cursors stay in memory and the hub replays only what is missing.
10. **History without the LRU.** Terminal history is read with `EventReader.read` (`AppSession.terminal.read`) and
    kept in the `terminal` slice only while the view is open; it never enters the `events` slice, whose 5 000-entry LRU
    would otherwise push the chat out. Live chunks pass the reader like every frame (and the `events` slice). The ID
    uniqueness of the reader (E9 on the reader side) is repeated per session in the `terminal` slice.
11. **Strict order and gap pulls.** A session's chunks are a subsequence of the workgroup scope. A live chunk at `seq`
    is applied only if every `scope_seq` between the last known position (`through`) and `seq` is a verified event
    that is not a chunk of this session; anything else (a missing seq from a lost frame or an unwatched session, a
    pending, locked or unverifiable event) may hide a chunk, so the chunk is held and the watcher pulls
    `…?session=<id>&kinds=session.terminal&after=<through>` first. Pulls run one at a time, at most one per second
    (chunks of unwatched sessions in the same workgroup would otherwise exhaust the rate limit of 600/min); a chunk
    that cannot be read yet blocks there and is pulled again after 5 s, as is a failed pull. Unverifiable chunks are
    counted in a notice, never written. Pulled events are not trusted to match the query: a verified event that is not
    a `session.terminal` of this session is recorded as bad (`header_invalid`), never shown.
12. **Terminal view.** Two xterm screens (WebGL renderer, canvas where WebGL2 is unavailable, loaded before `open()`,
    decision WEB-01 19): Follow writes new chunks to the visible one; History replays up to the slider offset into the
    hidden one and swaps (`invisible` class); replays never overlap (xterm's `reset` does not cancel queued writes), a
    request during a running replay waits and only the newest waiting one runs; playback 1×/4×/16× by `Player` (50 ms ticks); back to Follow replays
    everything the same way. Input is disabled, no `onData`/`onKey`/`attachCustomKeyEventHandler` handler, paste is
    cancelled on the container, the cursor is hidden (transparent theme, no inactive outline, `ESC[?25l`). The
    geometry is the newest chunk's asciicast size (160 × 48 without chunks) and is never changed by the container;
    `addon-fit`'s proposal scales the font (7–15 px, half steps; again on window resize and geometry change) and the
    container scrolls horizontally below that.
    `data-chunks` (section) and `data-chars` (screen hosts) expose counts for the specs, never content.
13. **Times** use `formatEventTime` of WEB-01 (`HH:MM:SS.mmm`, date prefix when not today); an item without a usable
    time shows none.
14. **Session detail** `/o/:org/s/:session`: `GET /v1/sessions/{id}`, then the task (`GET /v1/tasks/{task}`) for
    siblings and children; tabs Terminal (default), Chat (only `session.*` items of this session), Children (child
    rows from the store). Tabs of both pages are links with `?tab=` (the first tab without parameter).
15. **Device double for session content.** `scripts/hub_test_events.py` (new) seals device-authored session events
    with the workgroup key the browser wrapped for the device and stores them through `append_events` with every hub
    check, then announces them like the outbox; it also inserts subagent session rows with a `session` frame. The
    director only keeps the device identities and routes `POST /sessions/<id>/events` and `/children` to it.
16. **Spec location.** The chat, sessions and terminal specs need the device double, so they live in
    `web/e2e/devices/{chat,sessions,terminal}.spec.ts` (project `devices`, decision WEB-04 16) instead of `web/e2e/`.
    `e2e/helpers/sessions.ts` adds the director calls and `grantOwnAccess` (a `grant` entry with the verified platform
    state; the reader test keeps only read access to a second workgroup, the default workgroup is always writable).
17. **No-input proof.** The terminal spec types, presses Enter, Ctrl+C, pastes from the clipboard and dispatches a
    paste event on the viewport while recording every request of the page: none is made. A MutationObserver installed
    before any script proves that no `<style>` element is inserted; the CSP fixture fails on any violation.
18. **Foreign files touched (minimal).** `core/net/sse.ts` (watched terminals, refusal), `core/account/session.ts`
    (watch list, `terminal` context), `ui/screens/task/TaskScreen.tsx` (the frame of WEB-04, now the tab frame of this
    package as planned), `tests/ui/tasks.test.tsx` and `e2e/devices/home.spec.ts` (the prompt is a chat item now:
    `chat.prompt.text` instead of `task.prompt`), `tests/ui/registries.test.tsx` (the registries are no longer empty),
    `scripts/hub_test_director.py` (decision 15), `web/scripts/build.mjs` (the unminified build of the coverage run
    strips comments: the Markdown libraries carry `c8 ignore` markers that the repository hygiene scan of the hub's
    static directory refuses; the minified release build drops them anyway).
19. **E2E-08/12/13** (`e2e/platform/specs/`) do not exist yet (only E2E-00); the scenario packages write them on these
    screens and test IDs.
20. **Independent review – handled.** High, fixed: pulled history events were not checked against the session and
    kind (11); fast slider moves could mix replays (12). Medium, fixed: one refused session dropped every watched
    terminal (9); a sign-out during a pull left the watcher marked as pulling; unverifiable `session.terminal` events
    appeared as chat rows (now hidden like verified ones, 3). Medium, kept with reason: the stream reconnect per watch
    change (9, the API has no other way); the edits of foreign files and the spec location are minimal and named in
    18 and 16. Low, fixed: refit on geometry change, a malformed `created_at` no longer breaks the session tree, the
    no-input spec asserts that the keys reach the terminal's text area. Low, kept: frame code `m` is part of the
    contract (`contracts/guest/journal/record.json`, recorder restart marker) and never written; author names come from
    the active org's directory (the task page always runs in its org); device labels (`device.profile`) and the usage
    column of the Sessions tab come with WEB-06/WEB-10; scanning the scope's entries per store update (≤ 5 000) is
    cheap compared to rendering.
