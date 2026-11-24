# KOR-44 – decisions

Browser as a platform operation (ADR-085, 13-work-vm.md §13). Small decisions of the package; the architecture is in
the ADR.

1. **The browser session is the operation, the click is an entry.** §11 asks for a record per request of a session;
   a request is "a browser" (`browser.open`). A record and a hub round trip per click would make the browser slow
   enough that a model would fall back to scripts in the work VM, which nobody sees. Every click is an encrypted
   `browser.action` entry instead (idea 28 "traceable").
2. **One browser session per session.** Tabs cover several pages; a second `browser.open` returns the open one
   (`created: false`) and opens its `url`. A subagent has its own (its own session).
3. **Explicit open, no implicit start.** Unlike `workvm_exec`, a browser tool without an open browser answers
   `not_found` naming `browser.open`: the §11 record of the browser session must exist before the first action, and
   only the session's device registers operations. One extra call per browser session.
4. **Tools after the common agent browser tools.** Every answer is the page after the action (URL, title, tabs,
   accessibility snapshot with `ref`s), so the model needs no separate read; a failed action is `ok:false`, not a
   tool error, so the model corrects itself. Left out with reasons in ADR-085 §3 (JavaScript evaluation, upload,
   hover, drag).
5. **Own snapshot instead of Playwright's internal AI snapshot.** The helper builds the snapshot from the DOM (roles,
   accessible names, values, `ref` attributes) instead of an internal Playwright interface that may change between
   versions; the image pins Playwright, the helper's test pins the format.
6. **Pictures in the record as events.** The picture after an action is a JPEG of at most 160 KiB inside the
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
    the device caps with `BROWSER_LIVE_MAX_KBPS`. With several viewers the largest wish counts (one stream per
    session).
11. **Web search.** Claude Code's own `WebSearch` for Anthropic access (API key, subscription); refused for the
    platform access whose provider offers none. No paid search interface and no automated search engine query
    before the client's answer to F9; the switch `BROWSER_SEARCH_URL` is empty by default.
12. **Idle default 600 s.** A browser holds a standard VM (4 CPU, 8 GiB); ten minutes without a call frees it. A
    session may ask for 60–3600 s at open.
