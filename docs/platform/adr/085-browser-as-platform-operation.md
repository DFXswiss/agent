# ADR-085: Browser as a platform operation – a temporary browser VM, controlled tools, a kept action record and a live picture encrypted end to end

Status: Accepted (2026-11-25)

## Context
- Concept idea 28 ([KONZEPT](../KONZEPT.md#k-idee-28)): browser use is requested through the platform; it provides an
  isolated Ubuntu Lima VM with a browser that the session controls through controlled interfaces; activities are
  traceable and **live** visible; the VM is removed after use. §10 point 3 ([KONZEPT](../KONZEPT.md#k-10-b3)):
  subagents, tests and browsers are requested only as controlled platform operations. Idea 44
  ([KONZEPT](../KONZEPT.md#k-idee-44)): temporary browser VMs need no resume, they are started again after a failure,
  their logs and results stay stored. §6 point 3 ([KONZEPT](../KONZEPT.md#k-6-b3)): state and events also on a phone.
  §3 point 7 ([KONZEPT](../KONZEPT.md#k-3-b7)): a countdown instead of a spinner while something starts.
- Rules and interpretations: [A6](../KONZEPT.md#a-6) (browser tools with a live picture as platform operations),
  [E-08](../KONZEPT.md#e-08)/[E-09](../KONZEPT.md#e-09) (the work VM and its free browser stay unwatched),
  [E-04](../KONZEPT.md#e-04)/[E-26](../KONZEPT.md#e-26) (agents reach everything, efficiently), [E-05](../KONZEPT.md#e-05),
  [E-11](../KONZEPT.md#e-11), [E-19](../KONZEPT.md#e-19), [E-25](../KONZEPT.md#e-25) (no unordered protection layers).
- Built before: the work VM (ADR-059) with its Chromium and the stateless tool `workvm_browse` (`read`, `screenshot`,
  `click`, `fill`; a new browser per call, nothing live, nothing kept but the job output); run VMs of scope `run`
  (ADR-080) with pool, queue, boundary, billing and teardown on every path; the read-only terminal (ADR-016) whose
  output kind `session.terminal` reaches a browser only when it watches the session.

## Decision

### 1. Operations: the browser session is the §11 record
- `browser.open {url?, idle_seconds?}` → `{browser_id, created, state, workvm_id, ready_ms, idle_seconds, url, title}`
  and `browser.close {}` → `{closed, browser_id, actions, reason}` (`dedupe: false`, like `workvm.request`).
  `browser_id` = `br-<12 hex>`. One browser session per session at a time: `browser.open` while one is open returns it
  (`created: false`, the `url` is opened in it); a subagent gets its own. §11 asks for a record per **request**: the
  session's request is "a browser", which `browser.open` records with ID, task, session, type, parameters, times,
  state and result. The single clicks are not operations – a record, a hub round trip and a pipeline pass per click
  would make the browser unusably slow – but every one of them is an encrypted entry of the browser session (§3), so
  idea 28's "traceable" holds per action.
- The operation fails only when no browser came up (`credit_exhausted`, `limit_exceeded`, `workvm_unavailable`,
  `timeout` of the capacity queue, `upstream_error` when the VM or the browser did not start; retryable). A `url` that
  does not load still opens the browser (the result says so in `title`/`url`, the action entry carries the error).

### 2. The browser VM
- A row of `runner.workvm` with scope `browser` (runner migration), `session_id` = the requesting session: warm pool,
  FIFO queue, slot, RAM, disk, the outside boundary of the work VMs (no credential, host filter, egress through the
  device's Cloudflare client, fail-closed; the known limit 13-work-vm.md §6.6 applies as accepted on 2026-11-24),
  monitoring (device → VM → session) and billing (`billing_vms.py`, `vm_kind` `work`, size `standard`) exactly as a
  run VM. It counts against the organization and owner VM limits, not against the per-task limit. The work image is
  enough: it carries Playwright with Chromium; the image gains one helper (§5).
- **Always torn down**: `browser.close` (`browser_closed`), idle (`browser_idle`: no browser call for
  `idle_seconds`, default `BROWSER_IDLE_SECONDS` = 600, 60–3600 per open), the session's end (`session_ended`), the
  task's close (`task_closed`), credit used up (`credit_exhausted`), a runner restart (`restart`, no resume, idea 44),
  a failed boundary check (`failed`). The teardown deletes the VM in a `finally` shielded from cancellation; a failed
  Lima deletion stays `deleting` and the sweep of ADR-080 deletes it again.
- **Failure** (idea 44): when the browser or its VM stops answering, the browser session stays open and the next
  call starts a fresh VM (`browser.state` `restarted`, reason `vm_lost`); the call's answer carries
  `notice: "The browser VM failed and was started again; open pages, cookies and sign-ins are gone."` and the record
  continues with the next `seq`. Nothing of the old VM is resumed.
- Countdown: `browser.state` `starting` is written before the VM is obtained, so the chat card counts down to the
  10 s target like the work VM panel and shows "delayed" with the real step after it.

### 3. Controlled tools (MCP, forwarded like the work VM tools)
- Eleven tools of the `platform` MCP server, journaled as `workvm.call` like the work VM tools (one forwarding path,
  no read audit; each call is visible as `session.tool` in the chat and terminal): `browser_navigate` (url, or
  `history` back/forward/reload), `browser_snapshot`, `browser_click`, `browser_type` (with `submit`),
  `browser_select`, `browser_press` (a key or chord), `browser_scroll`, `browser_wait` (text appears, text goes, or
  milliseconds), `browser_tabs` (list/new/select/close), `browser_download` (a link or URL into the workspace),
  `browser_screenshot` (a PNG into the workspace for `Read`).
- **Built for a model, after the common agent browser tools**: every answer is the page after the action – URL,
  title, tabs and a compact accessibility snapshot (`role "name" [ref=eN]` lines with values, links and headings,
  at most 24 000 characters) – so the model reads and acts in one step. `target` is a `ref` from the last snapshot or
  a Playwright selector (`text=Sign in`, `#user`). A failed action (no such element, navigation error, timeout) is
  **not** a tool error: `ok: false` with the reason and the snapshot, so the model can correct itself. Tool errors
  are reserved for "no browser open" (`not_found`, naming `browser.open`) and platform states (`unavailable`).
  Dialogs (`alert`, `confirm`, `beforeunload`) are accepted and reported in `notice`.
- Not built, with the reason: JavaScript evaluation and file upload (scripted work belongs in the work VM, which runs
  any Playwright script, E-08; an upload needs a workspace-to-browser transfer that no use case has asked for yet,
  E-25), hover and drag (rare; a later addition is one tool each), PDF printing, network interception.
- `workvm_browse` stays for the work VM and stays unwatched (E-09); its description and the harness say that only
  the platform browser is live and recorded.

### 4. The action record (idea 28 "traceable", idea 44 "stays stored")
- Event `browser.action` (scope `wg`, author the device, header `session`/`task`) per tool call: `{browser_id, seq,
  tool, action, target, text, url, title, tab, ok, error, notice, at, duration_ms, image}`. `text` is what was typed,
  replaced by `"••••"` for password fields (a sign-in must not put the password into the record, the picture shows
  the field masked as well). `image` is the viewport after the action: JPEG `{type, width, height, data}` (at most
  160 KiB, quality lowered and size halved until it fits, else null with `image_omitted`), so an event stays below
  the 256 KiB limit (E8).
- `browser.action` is a **watched kind** like `session.terminal`: it is stored like every event (encrypted, signed,
  in order, readable by `task:read` after the teardown) but it is not in the general live fan-out, not in device
  pulls and not in a default event read; a browser receives it only while it watches the session's browser, and
  reads history with `kinds=browser.action`. One place lists the watched kinds and their frame types (E-19: the next
  heavy kind cannot leak into every stream by forgetting a filter).
- Event `browser.state` (same scope and author): `{browser_id, operation_id, state: starting|open|restarted|closed,
  workvm_id, reason, url, actions, ready_ms, idle_seconds, opened_at, closed_at}` – in the general fan-out, the chat
  card of the browser session.

### 5. Inside the VM: one helper, one browser
- `/usr/local/bin/workvm-browser` (Node, the image's Playwright): `serve` keeps one persistent Chromium context
  (profile and downloads under `/var/lib/workvm-browser`) behind a root-only Unix socket, started on first use as the
  transient unit `workvm-browser`; `call` sends one JSON request and prints one JSON answer; `stream` prints the
  screencast of the active tab as length-prefixed JPEG frames. The runner reaches it over Lima's SSH like
  `workvm-job` and treats every answer as untrusted (bounded header and body, closed schema).

### 6. Live picture, end to end encrypted, only while someone watches
- **Watching**: a browser adds `browser=<session_id>` (at most 3) and `browser_kbps` to its stream (`GET /v1/stream`,
  rights as `terminal=`, audit `read.stream.browser`). Each hub process renews a lease row per watched session
  (`hub.browser_watch`, 30 s, renewed every 10 s while a stream watches) and sends the session's device the hint
  `browser {session_id}` when a watch starts.
- **Frames**: on the hint the runner starts the screencast of the session's open browser (Chromium's screencast
  sends a frame only when the page changed) and posts frames to `POST /v1/device/sessions/{session}/browser/live`
  `{frame?}`; the answer `{until, kbps}` keeps it streaming, `409 not_watched` stops it (also within 10 s of the last
  viewer leaving: the runner checks with an empty post when no frame came). A frame is
  `{v, browser_id, session, wg, epoch, seq, ts, salt, ct}` with `ct = nonce‖AES-256-GCM(k_frame, jpeg, AAD)`,
  `k_frame = HKDF(WK_e, salt, P‖"browser-frame\0"‖browser_id)`, AAD binding browser, session, workgroup, epoch, seq
  and time (04-crypto.md §4.13). The hub stores only the **latest** frame per session (unlogged table, overwritten,
  dropped with the watch) to hand it to every process's streams (`NOTIFY` cannot carry it) and as the first picture of
  a new viewer; it never sees a picture or a URL in clear.
- **Bandwidth as a setting**: the viewer chooses "economical" (250 kbit/s), "standard" (1000) or "sharp" (3000) in
  the browser view (stored in the browser); the hub hands the largest wish of the current viewers to the device,
  which caps it at `BROWSER_LIVE_MAX_KBPS` (default 3000) and paces frames by a byte budget (at most 5 per second,
  JPEG quality 60, at most 1280 px wide). **Without a viewer no frame leaves the device** and no screencast runs.
- No signature per frame (E-25): only the device that runs the session can post to the route, and the hub cannot
  make a frame without `WK_e`; the viewer drops a frame whose `seq` is not newer than the last.
- Watch only: the live view has no input path (like the terminal, §6 point 7).

### 7. Agent access (E-04) and the web
- Web: the chat card of `browser.state` ("Browser open – watch live", countdown while starting); a tab **Browser** in
  the session detail with the live picture (or the newest recorded one), the windowed action record (newest first,
  each entry with time, action, target, URL, result and the picture after it) and the bandwidth choice; phones see
  the same, picture scaled to the width.
- `agent-platform browser list|show|frames` (sessions of a task or session from `browser.state`, the record of one
  browser session from `browser.action`, the pictures as JPEG files); the agent watches live with the same stream
  parameter (`browser frames --follow`).

### 8. Web search
- Claude Code's own web search runs at the model provider and needs nothing on the device: it is allowed for sessions
  whose model access is the Anthropic API or a Claude subscription (the provider offers it there) and stays refused
  for the platform access, whose provider has none. The paid search interfaces and an automated query of a search
  engine (question F9) are not built; the session opens a search page in the platform browser like a person. The
  runner setting `BROWSER_SEARCH_URL` (empty = off) is the prepared switch: when the client decides F9, a URL
  template lets `browser_navigate {query}` open it; while it is empty `query` answers that the switch is off.

## Consequences
- The hub learns no picture, URL, typed text or page content: actions and frames are encrypted under the
  workgroup key. It sees that a browser session exists (operation record), when frames flow (sizes, rate) and who
  watches.
- Stored volume: one event of up to about 200 KiB per action; a browser session of 500 actions is about 60 MB at the
  hub. The record view loads pages on demand and never holds the whole record.
- A browser VM costs a VM start (warm pool: about 0.1 s; cold: about 8 s) and is billed like a work VM.
- Out of scope: tests (ADR-080), billing logic beyond the VM extension point, a resume on another device (KOR-58),
  input into the live picture, JavaScript evaluation, uploads.
