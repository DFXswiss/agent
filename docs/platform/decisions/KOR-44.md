# KOR-44 – decisions

Browser as a platform operation (ADR-085, 13-work-vm.md §13). Small decisions of the package; the architecture is in
the ADR.

1. **The browser session is the operation, the click is an entry.** §11 asks for a record per request of a session;
   a request is "a browser" (`browser.open`). A record and a hub round trip per click would make the browser slow
   enough that a model would fall back to scripts in the work VM, which nobody sees. Every click is an encrypted
   `browser.action` entry instead (idea 28 "traceable").
2. **One browser session per session.** Tabs cover several pages; a second `browser.open` returns the open one
   (`created: false`) and opens its `url`. A subagent has its own (its own session).
3. **Implicit open** (design review M9, E-05). A browser tool without an open browser opens one first, like
   `workvm_exec` obtains a work VM: the platform takes the missing step instead of explaining it. Such a browser session
   has no operation record of its own (`operation_id` null, `implicit: true`); the tool call is recorded as
   `session.tool` and the browser session by its `browser.state` and `browser.action` events. `browser.open` stays the
   explicit request (with its own idle time).
4. **Tools after the common agent browser tools.** Every answer is the page after the action (URL, title, tabs,
   accessibility snapshot with `ref`s), so the model needs no separate read; a failed action is `ok:false`, not a
   tool error, so the model corrects itself. After the design review (M10) also hover, JavaScript evaluation,
   upload, dialogs, clicks at a point, waiting for a URL and `snapshot: false`; left out with reasons in ADR-085 §3
   (drag, PDF, interception, inline pictures).
5. **Own snapshot instead of Playwright's internal AI snapshot.** The helper builds the snapshot from the DOM (roles,
   accessible names, values, `ref` attributes) instead of an internal Playwright interface that may change between
   versions; the image pins Playwright, the helper's test pins the format.
6. **Pictures in the record as events.** The picture after an action is a JPEG of at most 100 KiB and 960 px inside the
   `browser.action` event (below the 256 KiB event limit): stored, ordered, encrypted and signed like every event,
   readable after the teardown (idea 44) without a second store. Because pictures are heavy, `browser.action` is a
   watched kind like `session.terminal`: it never reaches streams, device pulls or default reads that did not ask for
   it. The list of watched kinds lives in one place (E-19).
7. **Passwords masked in the record.** Text typed into a password field is recorded as `••••`; the picture shows what
   the page shows (masked).
8. **Live frames only for viewers, latest frame only.** No screencast without a valid watch lease; the hub keeps the
   latest frame per session in an unlogged table (bus messages carry at most 8000 bytes, so frames cannot travel on
   the bus) and drops it with the last lease.
9. **No signature per live frame** (E-25). The hub cannot make a frame without the workgroup key, only the session's
   device may post one, and the viewer drops non-increasing `seq`. A signature would cost an Ed25519 operation per
   frame on both sides for no attacker the design does not already stop.
10. **Bandwidth choice in the viewer, cap at the device.** Three steps (250/1000/3000 kbit/s) stored in the browser;
    the device caps with `BROWSER_LIVE_MAX_KBPS`. The device sends the largest wish of the viewers; the hub holds
    each viewer's stream to its own wish (one slot per stream, a newer frame replaces an unsent one).
11. **Web search.** Claude Code's own `WebSearch` for Anthropic access (API key, subscription); refused for the
    platform access whose provider offers none. No paid search interface and no automated search engine query
    before the client's answer to F9; the switch `BROWSER_SEARCH_URL` is empty by default.
12. **Idle default 600 s.** A browser holds a standard VM (4 CPU, 8 GiB); ten minutes without a call frees it. A
    session may ask for 60–3600 s at open.
13. **No quota of the record** (design review M7). The pictures are smaller (960 px, ≤ 100 KiB) and the record is kept
    like every event of the task; a quota or keep limit of its own would be an unordered layer (E-25) and can follow
    the measured volume.
14. **Runner restart keeps the browser session** (design review M12): the VM goes, the next call gets a fresh one with
    the notice, like a VM failure (idea 44).
15. **A slow call is no failure** (design review H3): a time-out answers `ok:false` and keeps the VM; only a failed
    probe replaces it. After a replacement only `browser_navigate {url}` runs at once, other actions answer with the
    notice (no double submit).
16. **Watched kinds drive everything** (design review L15): the one list decides reads, pulls, device hints, the bus
    payload (kind and session, so no process reads a heavy event nobody watches) and the stream replay.
