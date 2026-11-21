# 06 Web client

Status: binding. Implements the user-facing parts of KONZEPT §1–§13 in `agent-core/web/`. Uses the contracts of
`03-api.md`, the formats and flows of `04-crypto.md` and the data model of `02-data-model.md`. Stack decision:
ADR-019. Everything confidential (`00-overview.md` §3) is decrypted **only** in the browser; the UI never sends
plaintext of confidential content to the hub.

## 1. Principles

1. **Thin hub, smart client.** The client verifies the membership log, platform log, wraps and every event
   (`04-crypto.md` §6) before rendering. Unverifiable items are shown as a neutral "unverifiable item" row with the
   error code, never as content and never silently dropped.
2. **Rights in the UI mirror the hub.** Controls that the user's verified rights do not allow are not rendered
   (not just disabled); the hub enforces again (`03-api.md` §3).
3. **Push is a hint, pull is the truth.** Every live frame can be lost; the client repairs gaps by pulling with its
   cursors (`03-api.md` §5).
4. **Mobile first.** Every screen works on a 360 × 640 CSS-pixel viewport (§12).
5. **No spinner for VM/session start.** A countdown with real status is shown instead (§4).

## 2. Stack and structure

### 2.1 Dependencies

| Purpose | Package | Notes |
|---|---|---|
| Language | TypeScript (strict, `noUncheckedIndexedAccess`, `exactOptionalPropertyTypes`) | |
| Build/dev | Vite | output §10 |
| UI | React 18, React Router 6 (data router) | |
| Styling | Tailwind 3 (compiled, no runtime CSS-in-JS) | |
| Terminal | `@xterm/xterm` + `@xterm/addon-fit` + `@xterm/addon-webgl` (fallback `@xterm/addon-canvas`) | read-only, §5.5 |
| Markdown | `react-markdown` (no `rehype-raw`, `skipHtml: true`, links `rel="noopener noreferrer nofollow"`, only `http(s):`/`mailto:` URLs) | chat outputs and whiteboards |
| Charts | `uplot` | monitoring, §9 |
| Tests | Vitest, `@vitest/coverage-v8`, Playwright, `monocart-coverage-reports`, Testing Library | dev only |
| Lint/format | ESLint (`--max-warnings 0`), Prettier (`singleQuote`, `printWidth 120`) as in the DFX `app` repo | dev only |

No further **runtime** dependency without an ADR. Crypto is WebCrypto only (`04-crypto.md` §1). Node 24 LTS for
build and test; npm with committed `package-lock.json`.

### 2.2 Directory layout (`web/`)

```
src/core/                 framework-free, runs unchanged in Node (Vitest) and the browser
  encoding/               b64u, canonical JSON (+ non_ascii check), UUIDv7, time formats
  crypto/                 HKDF, AES-GCM, HPKE, Ed25519/X25519, bundle, wraps, events, credentials,
                          device requests, invitation codes, dedupe tag (verification of vectors)
  passkey/                WebAuthn option/response (de)serialisation, PRF extraction, session statement
  log/                    membership-log + platform-log verification (L1–L10, P1–P4), rights projection,
                          wrap rules (W1–W3), rotation-batch builder, catch-up planner
  net/                    signed fetch (agent-request-v1 with the session key), response-signature check,
                          SSE-over-fetch client (frames, heartbeat, reconnect, cursors, gap repair)
  store/                  single application store (event store per scope, entities, decrypted cache),
                          selectors, subscription API (`subscribe(selector, listener)`)
  persist/                IndexedDB: session key (non-extractable CryptoKey), session certificate meta,
                          verified log heads per org, verified platform-log head, optional device key,
                          last cursors; nothing else
  jobs/                   background jobs (§11)
  i18n/                   message catalogs `de.json`, `en.json`, error-code catalog
src/ui/                   React components, routes, hooks (`useStore(selector)` via useSyncExternalStore)
src/main.tsx              bootstrap: config check (§10.4), restore session, router
tests/                    Vitest unit tests (core 100 % on its own, ui via Testing Library)
e2e/                      Playwright specs for web-only flows against a hub test server (full E2E in agent-core/e2e/platform)
```

`src/core` MUST NOT import React, the DOM outside `persist/` and `passkey/`, or anything from `src/ui`.

### 2.3 State management

One store in `core/store` holds: config, me, orgs (verified log state, rights, keys in memory as non-extractable
`CryptoKey` or raw bytes in closures), workgroups, tasks, sessions, operations, notifications, devices,
decrypted event bodies (bounded LRU per scope: 5 000 events; terminal chunks kept only for watched sessions),
metrics, connection state, job state. Scope keys and the identity private keys live only in memory; a page reload
requires the passkey (or recovery code / device key) again. The store is updated only by core modules; React reads
via selectors.

### 2.4 Routing

| Route | Screen | Right |
|---|---|---|
| `/` | redirect: unauthenticated → `/login`; else last org → `/o/:org` | – |
| `/invite` (code in fragment `#…`) | invitation (§3.1) | anon |
| `/login` | login and unlock (§3.4) | anon |
| `/recover` | recovery (§3.5) | anon |
| `/o/:org` | home: first-task composer (§4) and recent tasks of the default workgroup | org:member |
| `/o/:org/w/:wg` | task list + composer of a workgroup (§5.1) | wg:read |
| `/o/:org/t/:task` | chat (§5.2), tabs: Chat, Sessions, Operations, Whiteboard, Audit | task:read |
| `/o/:org/s/:session` | session detail: terminal (§5.5), chat of that session, children | task:read |
| `/o/:org/whiteboard` and `/o/:org/w/:wg/whiteboard` | whiteboards (§6.1) | org:member / wg:read |
| `/o/:org/members` | members and rights (§7.1) | org:member (edit: admin) |
| `/o/:org/workgroups` | workgroups (§7.2) | org:member (edit: admin) |
| `/o/:org/w/:wg/settings` | workgroup settings: repositories, protected branches, PR draft default (§8.1) | wg:read (edit: wg:write) |
| `/o/:org/devices` | devices (§7.3) | org:member (edit: admin / private-org writer) |
| `/o/:org/monitoring` | monitoring (§9.1) | org:member |
| `/o/:org/audit` | audit (§9.2) | org:member (scope per `03-api.md` §4.11) |
| `/notifications` | notification center (§6.2) | user |
| `/settings` | account: passkeys, recovery code, language, model access, credentials, GitHub, quotas (§3.6, §8) | user |
| `/platform/invites` | platform invitations (§7.4) | platform:inviter |
| `/security` | security page (§10.5) | anon |
| `*` | not found | – |

The fragment of `/invite#<code>` is read once, then removed with `history.replaceState` so it does not remain in
history; it is never sent to the hub, logged or stored.

### 2.5 Internationalisation and messages

- Locales `de` and `en`. Initial locale = `user_settings.locale` if signed in, else the first supported
  `navigator.languages` entry, else `en`. Switch in `/settings` (`PUT /v1/me/settings`).
- All UI strings come from `core/i18n/<locale>.json`; a unit test asserts both catalogs have the same keys.
- **Error catalog:** every error code of `03-api.md` §11 and `04-crypto.md` §8 has an entry
  `{title, body, action}` in both languages; unknown codes render a generic message showing the code. A unit
  test enumerates the codes from `contracts/` and fails on a missing entry.
- Numbers, money (USD, 4 decimals below 1 USD) and dates via `Intl` with the active locale.

### 2.6 Signed requests and the session

`core/net` signs every non-anonymous request with the session key (`03-api.md` §1). On `401 session_expired` or
less than 5 minutes before `expires_at` the UI shows a non-blocking "Sign in again" prompt; the re-login (§3.4)
replaces the session key without losing the screen. `POST /v1/session/end` on explicit sign-out, then IndexedDB
session entries are deleted and memory is cleared.

## 3. Invitation, registration, login, recovery, passkeys

### 3.1 Invitation screen (`/invite#<code>`)

States and steps (`04-crypto.md` §5.1–§5.3):

| State | UI | Calls |
|---|---|---|
| `parsing` | – | parse code (`invite_code_length`/`invite_code_invalid` → error state with "Check the code") |
| `lookup` | "Checking invitation…" (short, no countdown) | `POST /v1/invites/lookup` |
| `invalid` | one message for unknown/used/revoked/expired (`invite_unknown`), `invite_expired` separately | – |
| `org_verify` (org invites) | – | open package, verify the log **returned by the lookup** (the invitee is not a member yet and cannot call `GET /v1/orgs/{org}/log`); failure → blocking error (`invite_not_in_log`, `log_*`) |
| `intro` | what the invitation grants: org name and workgroup names (from the package fields `org_name`/`wg_names`), role, expiry; buttons "Create passkey" and "I already have an account" (org invitations only); with a valid session the client goes to `existing_account` directly | – |
| `existing_account` (org invites) | "Join <org> with your existing account" | login ceremony §3.4 if no session; then `accept` with the registered identity (`04-crypto.md` §5.2a); `invite_registered` → error state |
| `create_passkey` | gesture 1 explained ("Your device will ask you to create a passkey") | `POST /v1/passkeys/register/begin` (ES256 only), `navigator.credentials.create()`, `POST /v1/passkeys/register/finish` (incl. the self-wrap of the user's new `CK_1`) |
| `unlock_passkey` | gesture 2 explained ("Confirm once more to sign in") | login ceremony §3.4 |
| `recovery_code` | §3.2 | `PUT /v1/me/bundles/{slot}` after PRF |
| `prf_missing` | §3.3 | – |
| `accept` | progress "Joining …" | `POST /v1/orgs/{org}/log` (`accept`) or `POST /v1/orgs` (genesis for platform invites, §3.1.1) |
| `done` | navigate to `/o/:org` with the first-task composer (§4) | – |

Cancel handling (`AbortError`, `NotAllowedError`): return to the previous step with "Passkey creation was
cancelled – try again"; never create partial users twice (register/begin is repeated with a new ceremony; the hub
ignores the abandoned one).

#### 3.1.1 Platform invitation

No package. After `recovery_code` the client creates the private organization: name "Private" (`de`: "Privat"),
default workgroup "Default" (`de`: "Standard"), `OK_1`, `WK_1`, self-wraps (the user's `CK_1` exists since registration), `genesis{private:true}`, profile
events, `member.profile` with the display name entered on the intro screen (required, 1–80 chars), all in
`POST /v1/orgs` with `platform_invite_id` (`04-crypto.md` §5.3). Org invites also ask for the display name and send
`member.profile` right after `accept`.

### 3.2 Recovery code display and confirmation

- The code is shown once in groups `XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX` in a monospace block with "Copy" and "Download as
  text file" (file generated client-side via `Blob`, name `agent-recovery-code.txt`).
- Continue requires retyping the **last group** (6 characters, case-insensitive); mismatch → inline error.
- "Skip" is offered only behind a confirmation dialog that states the consequence: for a private organization with a
  single member, losing all passkeys and the code loses all data (`04-crypto.md` §5.5 step 4). Skipping sets a
  persistent banner "Recovery code not confirmed" until confirmed in `/settings`, where a **new** code can be generated
  (replaces the recovery slot, `PUT /v1/me/bundles/recovery` with `new_recovery_id`).

### 3.3 `prf_missing` guidance

Shown when the login ceremony returns no PRF result (detected **per login**, `04-crypto.md` §5.4):

1. Explain: "This passkey cannot unlock your encryption keys on this device."
2. Options: (a) enter the recovery code (unlock only: bundle from the signed `GET /v1/me/bundles/recovery`, no rotation and
   no new passkey; the §3.5 recovery flow is only for users without any working passkey); (b) "Remember this browser":
   after unlocking, create a non-extractable AES-GCM key in IndexedDB that encrypts the identity bundle locally
   (`persist/device-key`); next login on this browser unlocks with it; (c) add a PRF-capable passkey from another
   signed-in device (§3.6).
3. During registration with a non-PRF authenticator, option (b) is offered directly after the recovery code.
The device key is per browser profile, listed in `/settings` ("This browser can unlock without passkey PRF") with
"Forget" (deletes the IndexedDB entry).

### 3.4 Login and unlock (`/login`)

One button "Sign in with passkey" → `POST /v1/passkeys/login/begin` → new session key (P-256, non-extractable) →
statement → `get()` with PRF → `POST /v1/passkeys/login/finish` → decrypt bundle with `KEK_i` (or device key / recovery
code per §3.3) → load `GET /v1/me`, verify logs of all orgs (§11.3), fetch wraps (`GET /v1/wraps`) and open scope keys
→ route. Unlock failure after a valid login (`decrypt_failed`) shows §3.3 options.

### 3.5 Recovery (`/recover`)

Enter code → `POST /v1/recovery/begin` → decrypt recovery bundle → sign proof → `POST /v1/recovery/finish` → create a
new passkey (gesture 1) → login (gesture 2) → upload passkey slot → **new recovery code** (§3.2, mandatory, no skip)
→ home. Rate-limit responses (`429`) show the retry time.

### 3.6 Passkeys in `/settings`

List (`GET /v1/me`: created, last used, `prf_seen`), "Add passkey" (`04-crypto.md` §5.6; option "on another device"
uses the platform's hybrid/QR UI of `create()`), "Remove" (`DELETE /v1/passkeys/{id}`; disabled for the last one,
`last_passkey`).

## 4. First task and countdown

### 4.1 Entry

Right after the first login (and on `/o/:org` whenever the user has no task yet) the home screen shows the composer
with:

- **Context line:** organization (the invitation's org, else the private org) and workgroup selector preselected to
  the default workgroup; changing the workgroup lists only workgroups with `wg:write`.
- **Suggestions** (static, localized, clickable chips that fill the composer, never auto-send): "Research: …"
  (e.g. "Summarize the current state of …"), "GitHub pull request: …" (if GitHub is not connected the chip first
  offers "Connect GitHub", §8.1), "Explain this repository: …", "Write a short plan for …".
- Free-text prompt field (multi-line; send with button; `Ctrl/⌘+Enter` on desktop).

Every send creates a task (KONZEPT §2), even for a short knowledge question.

### 4.2 Title derivation (deterministic)

`title = first non-empty line of the prompt`, with all runs of Unicode whitespace collapsed to one space and trimmed;
if longer than 80 characters (counted in Unicode code points) it is cut to 79 code points + `…`. Implemented in
`core/tasks/title.ts`, vector-tested. The title is the body of the `task.meta` event; a session may later refine it
with `task.set_title` (shown as "renamed by session").

### 4.3 Send sequence

1. Ensure device (`03-api.md` §4.6.1 step 1): if the workgroup has no assigned device, the user is admin (in a private
   org the single member always is) and the org has platform access (ADR-026), assign the first platform candidate automatically (verify platform log, build
   `device_assign` + wraps of `WK`, `OK`, own `CK` batch). Shown as the first countdown step "Assigning execution
   device". If not allowed or no candidate: message `no_device_available` with a link to `/o/:org/devices`.
2. Encrypt `task.meta` and `prompt` (scope wg, `lh` = verified head), `POST /v1/tasks`.
3. Navigate to `/o/:org/t/:task` immediately; the chat shows the prompt and the countdown panel.

### 4.4 Countdown panel

- Starts at `min(estimate_ms, 10000)` from the `POST /v1/tasks` response, counting down in whole seconds (tenths
  below 3 s), rendered as large digits with a progress ring. **No spinner** anywhere in this panel.
- Below: **status step list** built from `session_transition` rows (SSE `session` frames, `GET /v1/sessions/{id}`):
  `requested`, `vm_assigning`, `vm_ready`, `sandbox_ready`, `agent_starting`, `prompt_delivering`, `working`, each
  with a check mark, the device-reported time and the elapsed time since task creation (hub clock offset from
  `hello.hub_time` is applied). The current step is highlighted.
- Countdown reaches 0 before `working`: the digits are replaced by **"Delayed"**, the current step and a live elapsed
  counter ("Starting the agent – 12.4 s"). The panel stays until `working` or `failed`.
- `estimate_ms` is null (`scheduling`): instead of digits the panel shows the real reason: `device_offline`
  ("The execution device is offline – the task starts as soon as it is back"), `capacity_exhausted` ("All execution
  slots are busy …"), with elapsed time.
- `working` reached: panel collapses into a one-line summary "Ready in 4.2 s" (kept in the chat timeline).
- `failed`: error from the catalog (`state_reason`) and "Retry" (sends the same prompt as a follow-up, §5.2).

## 5. Tasks, chat, sessions, terminal

### 5.1 Task list (`/o/:org/w/:wg`)

Newest first (`GET /v1/workgroups/{wg}/tasks`), with decrypted title, state badge, owner display name, last
activity; filter by state; infinite scroll (`next_before`). Composer at the top (same as §4.1) for users with
`wg:write`. Task creation is possible in any workgroup with write access.

### 5.2 Chat (`/o/:org/t/:task`, tab Chat)

- Timeline from events of the wg scope filtered by `task` (`GET /v1/scopes/{wg}/events?task=…` + SSE), ordered by
  `(ts, scope_seq)`:
  - `prompt` – right-aligned bubble with author name.
  - `session.output` – Markdown (§2.1 rules), per session; subagent outputs are indented with the subagent label.
  - `session.tool` – collapsed row "Read src/app.py", "Edit …" (expand: input/result summaries, error flag).
  - `operation.params` / `operation.result` + `operation` rows – inline card with type, state badge (live), and
    result links (PR URL, commit SHA, comment URL); duplicates show "duplicate of …" linking the original.
  - `session.file` – "Changed `path`" with an expandable patch (monospace, no syntax highlighting dependency).
  - `session.hook` with `deny` – muted row "Blocked: <tool> (<reason>)".
  - `session.status` errors and task/session state changes – system rows.
  - Countdown summaries (§4.4).
- Input: only here, only for `task:write`. Send = `prompt` event via `POST /v1/tasks/{task}/prompts`. While the main
  session is `working`, input stays enabled (prompt is queued by the session) and a **Stop** button (`interrupt`)
  plus a menu **End session** (`stop`) are shown (`POST /v1/sessions/{id}/control`). "Close task"
  (`POST /v1/tasks/{task}/close`) in the task menu.
- Read-only users see no composer and no control buttons; a notice "You have read access" is shown.
- Usage strip (per task): tokens, cost with basis label ("provider", "list price", "subscription equivalent"),
  context-window percentage of the main session.

### 5.3 Sessions tab

Tree of sessions (main session, subagent children by `parent_session_id`, depth ≤ 2) with state, device, model,
usage; each node links to `/o/:org/s/:session`.

### 5.4 Operations tab

`GET /v1/tasks/{task}/operations`, newest first; detail drawer shows transitions, decrypted params and result.

### 5.5 Session detail and read-only terminal (`/o/:org/s/:session`)

- Tabs: **Terminal**, **Chat** (outputs of this session only), **Children**.
- Terminal: `@xterm/xterm` with `disableStdin: true`, cursor hidden, no `onData`/`onKey`/`attachCustomKeyEventHandler`
  handlers, no clipboard paste; selection + copy allowed. Size from the asciicast header (`cols`/`rows`, 160 × 48 by
  default); `addon-fit` scales the font to the container width; on narrow screens the terminal scrolls horizontally
  instead of reflowing (no resize is ever sent).
- Data: history via `GET /v1/scopes/{wg}/events?session=…&kinds=session.terminal` (paged), live via SSE
  `terminal=<session>` (max 3 watched sessions). Chunks are applied strictly in `scope_seq` order; a gap triggers a
  pull before further rendering.
- Modes: **Follow** (default, live tail) and **History** (a time slider over the session's recording range; moving it
  replays from the start into an off-screen terminal up to the chosen offset, then shows it; playback speed 1×/4×/16×).
- An explicit label "Read-only view – prompts are entered in the chat" is always visible.

## 6. Whiteboards and notifications

### 6.1 Whiteboards

- Locations: org level (`/o/:org/whiteboard`), workgroup level (`/o/:org/w/:wg/whiteboard`), task level (task tab).
- List: entries newest first (`GET /v1/whiteboards`), decrypted latest text (Markdown), author, updated time,
  "edited" marker.
- Create/edit/delete only with the right of `04-crypto.md` §4.3 (org level: admin; wg/task level: wg write). Create
  form has an optional checkbox **"Notify participants"** (sends `notify:true`, `03-api.md` §4.9).
- History drawer: all versions (`GET /v1/whiteboards/{entry}/history`) with author and time, diff to the previous
  version (line-based, client-side).

### 6.2 Notifications

- Bell with unread badge (from `GET /v1/me.unread_notifications`, updated by SSE `notification`).
- Center (`/notifications`): newest first, content-free texts built client-side from kind + decrypted target names
  ("New whiteboard entry in workgroup X", "Task Y is waiting for you", "Task Y failed", "Z accepted the invitation",
  "Key rotation required in org X"); click marks read and navigates; "Mark all read".
- Toasts for live notifications (auto-dismiss 6 s, accessible live region).

## 7. Organization management

### 7.1 Members and rights (`/o/:org/members`)

- List: members (display name from `member.profile`, role, workgroups with access), devices are on §7.3.
- Admin actions:
  - **Invite user:** role (member default, admin), workgroups with read/write (default workgroup always write),
    expiry (72 h default, 1 h–30 d). Result screen shows link `<origin>/invite#<code>` and the code, with copy buttons
    and a warning to transmit confidentially; the code is not shown again. Open invitations list with revoke
    (`03-api.md` §4.2).
  - **Change role** (`role` entry); last-admin protection: the control is hidden for the last admin, the hub
    returns `last_admin` otherwise.
  - **Remove member** and **ungrant**: confirmation dialog explaining "already read content stays with the person",
    then the client builds the atomic batch (entry + `rotate` for every affected scope + wraps for all remaining
    recipients) with a progress view ("Rotating keys 2/5", "Wrapping for 7 recipients") and sends one
    `POST /v1/orgs/{org}/log`. On `seq_conflict` the client re-reads the log and rebuilds the batch automatically
    (max 3 attempts).
  - **Grant workgroup access** (read/write) with wraps of the current `WK` and an option "also give access to
    history" (wraps of older epochs).
- Members can **leave** the org (self `remove`; for the last admin hidden).

### 7.2 Workgroups (`/o/:org/workgroups`)

List with names (decrypted `wg.profile`), my access, devices, archived flag. Admin: create (new `WK_1`, one batch with `wg_create` (the creating admin gets write), `grant` entries for chosen
members, self-wrap + wraps for those members and chosen devices (`device_assign`), `wg.profile` event); other admins
get no access without an explicit `grant`, rename (`wg.profile`), archive (not the
default workgroup).

### 7.3 Devices (`/o/:org/devices`)

- List: label (decrypted `device.profile`), kind (platform/org), online state, last seen, agent version, assigned
  workgroups, capacity bar, **capacity-low badge** when `capacity_low` (`03-api.md` §4.6.2).
- **Add org device** (admin): choose workgroups → code + instructions block:
  `agent-runner enroll --hub <origin> --code <code>` (copy button), expiry 24 h default; the device appears live when
  it accepts.
- **Assign platform device** to a workgroup (admin; any writer in a private org): candidate list
  (`GET /v1/workgroups/{wg}/device-candidates`), platform-log verification, batch.
- **Unassign** from a workgroup / **Unpair** (remove from org): confirmation + automatic rotation batch (§7.1).
- Rename (admin, `device.profile`).

### 7.4 Platform invitations (`/platform/invites`, `platform:inviter`)

Create (code generated client-side, `POST /v1/platform/invites` with `invite_id` and expiry), show link/code once,
list open/used/revoked/expired, revoke.

### 7.5 Org switcher

Header control listing all orgs from `GET /v1/me` with decrypted names; switching changes the SSE `org` parameter and
reconnects the stream. "Create organization" (non-private genesis with a new default workgroup) is available to
every user; such an org cannot use platform devices until the operator grants platform access (ADR-026); the device
screen says so. Private organizations are only created from a platform invitation.

## 8. Integrations and settings

### 8.1 GitHub

- `/settings` → GitHub: state from `GET /v1/me/credentials` (`github`, login shown after decrypting the record).
- **Connect:** if `GET /v1/me/devices` is empty (fresh private org), first run the device auto-assign batch of §4
  (`03-api.md` §4.6.1 step 1) for the default workgroup; then pick an eligible online device (preselect a device of
  the default workgroup)
  → device request `github.connect` (`POST /v1/device-requests`) → response 1 shows `user_code` large with copy
  and the verification link (opens in a new tab) and a countdown until code expiry → response 2 shows success with
  the GitHub login, or the error. Hint to select repositories when installing the GitHub App.
- **Disconnect:** `DELETE /v1/me/credentials/github` + link to the GitHub settings page to revoke the app
  authorization.
- Workgroup settings (`/o/:org/w/:wg/settings`, `wg:write`): repository allow-list (`owner/name`, validated with the
  regex of `03-api.md` §9), protected branch patterns (defaults prefilled), PR draft default; saved as
  `settings.workgroup` event (latest wins; concurrent edit warning when a newer event arrived meanwhile).

### 8.2 Model access

- Preference (`auto`, `subscription`, `api_key`, `platform`) and model choice (`PUT /v1/me/settings`). Allowed model
  IDs per access kind come only from `GET /v1/config` (`models`); there is no build-time list. `platform` explains it is available only on DFX devices.
- **Anthropic API key:** paste field (password type), stored as `anthropic_api_key` credential (encrypt with `CK`,
  `PUT /v1/me/credentials/anthropic_api_key`); optional "Check key" device request `credential.check`.
- **Claude subscription (setup-token relay):** device request `claude.login` → response shows the authorize link →
  user authorizes in a new tab and pastes the displayed code into the field → follow-up device request message with
  the code → success shows the subscription e-mail; the token never appears in the UI.
- **Mailbox automation:** form for IMAP host/port/user/password/e-mail stored as `mailbox` credential; then
  `claude.login` with "automatic" mode; progress responses are displayed; fallback to the manual relay on
  `login_automation_failed`.
- **Quotas:** `GET /v1/me/quotas` – bars for 5-hour and weekly windows with reset times; OpenRouter/API-key spend in
  monitoring (§9).
- Credentials list with version and "Delete".

## 9. Monitoring and audit

### 9.1 Monitoring (`/o/:org/monitoring`)

- Overview (`GET /v1/monitoring/overview?org=`, live via SSE with `monitoring=1`): devices (CPU, RAM, disk, network,
  uptime, reachability, state, free capacity), drill-down to VMs of own workgroups, then sessions (CPU, memory,
  tasks, workspace size, state, usage: tokens, cost, context %).
- Charts (uPlot) via `GET /v1/monitoring/series`: ranges 1 h, 6 h, 24 h (raw), 7 d, 30 d, 90 d (rollups); legend and
  tooltips; values with units; accessible data table toggle.

### 9.2 Audit

- `/o/:org/audit`: filters action, actor, workgroup, task, session, time; newest first; rows show actor display
  name, action, result, target names (decrypted where available), link to the referenced event. Admin: "Verify chain"
  (`GET /v1/orgs/{org}/audit/verify`) with result.
- Task tab Audit: same view filtered by task.

## 10. Build, reproducibility, CSP, SRI, security page

### 10.1 Build output

`npm run build` (Vite) writes to `agent-core/src/agent_hub/static/`: `index.html`, `assets/<name>-<hash>.js|css`,
fonts, icons, `build-manifest.json`. All asset names are content-hashed; `index.html` is the only unhashed file and is
served with `Cache-Control: no-cache`; assets with `immutable, max-age=31536000`. The hub serves `index.html` for all
SPA routes (§2.4) and `/assets/…` statically.

### 10.2 CSP and SRI

- The CSP of `03-api.md` §1 applies to every response; therefore: no inline `<script>`/`<style>`, no `style`
  attributes in `index.html`, no `eval`/`new Function`, Tailwind compiled to a file, fonts self-hosted.
- Every `<script>` and `<link rel="stylesheet">`/`modulepreload` in `index.html` carries `integrity="sha384-…"` and
  `crossorigin="anonymous"` (Vite plugin in-repo, tested).
- Libraries that inject `<style>` elements at runtime are forbidden; xterm.js and uPlot styles are imported as
  static CSS files. The terminal uses `@xterm/addon-webgl` (fallback `@xterm/addon-canvas`), not the DOM renderer,
  which injects `<style>` elements. M0 verifies that no runtime `<style>` injection remains; if one does, the only
  permitted relaxation is `style-src-elem 'self' 'unsafe-inline'` through an amendment of ADR-019 (script policy
  stays strict). A Playwright test fails on any CSP violation report (`securitypolicyviolation` listener).

### 10.3 Reproducible build

Pinned `package-lock.json`, `npm ci`, Node version in `.nvmrc`, `SOURCE_DATE_EPOCH` = commit time, no timestamps or
absolute paths in output. Two builds from the same commit MUST be byte-identical (CI job compares). The
`build-manifest.json` lists `{version, commit, files:[{path, sha256, size}], root_key_fingerprint}`; its sha256 is the
**build hash**, published with each release.

### 10.4 Compiled configuration

`VITE_PLATFORM_ROOT_KEY` (b64u Ed25519 public key) is compiled in. At startup the client fetches `GET /v1/config` and
compares `platform_root_key`; mismatch → blocking error `platform_root_mismatch`. The RP ID/origin from the config
must equal `location.origin`.

### 10.5 Security page (`/security`)

Shows build hash, commit, platform root key fingerprint (SHA-256, grouped hex), hub key ID, and a plain explanation:
what the hub sees (`04-crypto.md` §7) and what it cannot see; that web E2EE depends on the delivered JavaScript and
how to compare the build hash with the published one; that execution devices process plaintext of their workgroups.

## 11. Background jobs (`core/jobs`)

Run while signed in and unlocked; serialized per org; status visible in a small "Sync" indicator.

1. **Log verification** (`log/`): on login, on SSE `log` frames and on events with `lh` > verified head. Rollback
   (`log_rollback`) or fork (`log_fork`) → **blocking error screen** for that org ("The server presented an
   inconsistent membership history"), no further reads or writes in that org, with the evidence (heads) for support.
2. **Wrap verification:** every fetched wrap is checked (signature, W1–W2); violations are ignored and counted
   (`wrap_unauthorized`).
3. **Catch-up wrapping:** after every log change and every 10 minutes: `GET /v1/orgs/{org}/catch-up`, create the
   wraps the user may create, `POST /v1/wraps`.
4. **Pending rotations:** if `rotation_pending` is non-empty and the user is admin (or wg writer for wg scopes):
   build and send the rotation batch automatically, with a toast.
5. **Credential re-wrap:** when a device becomes eligible for the user's `CK` (new assignment in a workgroup where the
   user is a member) wrap `CK` for it; when a device lost eligibility, rotate `CK` and re-encrypt all own credential
   records (`POST /v1/me/credentials/rotate`).
6. **Self-wraps after accept:** store wraps of the package keys for the own identity (`04-crypto.md` §5.2).
7. **Session renewal reminder** (§2.6).

## 12. Smartphone, accessibility, time, connectivity, errors

### 12.1 Layout

- Mobile-first Tailwind; breakpoints `sm` 640, `md` 768, `lg` 1024 px. Below `md`: bottom navigation (Home, Tasks,
  Notifications, Monitoring, More), full-width screens, drawers as full-screen sheets; from `lg`: left sidebar
  (orgs, workgroups) + content.
- Touch targets ≥ 44 × 44 px; no hover-only functions; composer stays above the on-screen keyboard
  (`visualViewport`).
- Terminal on phones: horizontal scroll at fixed geometry, pinch-zoom allowed on the terminal container only,
  "Follow" toggle reachable with the thumb.

### 12.2 Performance budget

Initial JS ≤ 300 KiB gzip (route-level code splitting: terminal, charts, markdown and settings load lazily); time to
interactive ≤ 3 s on a mid-range phone profile (Playwright CPU throttling ×4, "Fast 3G"); measured in the E2E suite.

### 12.3 Accessibility

Semantic HTML, labels for all inputs, focus visible, keyboard operable dialogs with focus trap, `aria-live` for
toasts, countdown (announced every 5 s and on step change) and connection banner, contrast WCAG AA, `prefers-reduced-motion`
respected (countdown ring without animation).

### 12.4 Lists and time

Lists newest first (repository rule) with stable tie-breaker; chat and logs ascending. Event times (chat items,
tool calls, operations, status steps, terminal positions, audit rows, whiteboard history) are shown as absolute
local time `HH:MM:SS.mmm` (user time zone, `Intl.DateTimeFormat().resolvedOptions().timeZone`), prefixed with the
date when not today; a relative label ("2 min ago") is only a secondary label in lists.

### 12.5 Connectivity

Banner states: **Live**, **Reconnecting… (n s)** (SSE backoff 1 s → 30 s with jitter), **Offline** (`navigator.onLine`
false). Writes are not queued offline: send buttons show "Offline" and retries re-use the same IDs (idempotent,
`03-api.md` §1). After reconnect the client pulls all cursors before showing "Live".

### 12.6 Error pages

Not found, forbidden (without revealing existence), blocking crypto/log errors (§11), session expired, hub
unavailable (`503`), unsupported browser (missing WebAuthn, X25519/Ed25519 in WebCrypto, or `fetch` streaming) with the
list of supported browsers.

### 12.7 Browser support and manual checklist

Automated: Chromium (desktop and mobile emulation: Pixel 7 and iPhone 15 profiles) with the CDP virtual authenticator
(`hasPrf: true` and `false`). Manual checklist per release (no virtual authenticator available): Safari macOS and iOS
(iCloud Keychain passkey with PRF, registration, login, recovery, terminal view, countdown), Firefox desktop
(registration with a security key, login, chat), Chrome Android (Google Password Manager passkey). Results recorded in
`docs/platform/manual-checks/<version>.md`.

## 13. Test hooks

- `data-testid` convention: `<screen>.<element>[.<qualifier>]`, lowercase kebab parts, e.g. `invite.create-passkey`,
  `countdown.digits`, `countdown.step.vm-ready`, `chat.composer.send`, `terminal.viewport`, `members.invite.submit`.
  IDs are part of the contract with the E2E suite; renaming needs the E2E change in the same PR.
- No test-only code paths in production builds: no `import.meta.env.MODE === 'test'` branches in `src/`; test
  configuration only through the public config and the hub test server. A build check fails if the production bundle
  contains the string `__TEST__` or exposes globals on `window`.
- Coverage: `src/core` 100 % lines/branches in Vitest alone; whole `src` 100 % lines/branches merged from Vitest and
  Playwright (monocart), gate details in `07-testing.md`.
- Playwright coverage per screen (web-level specs; full-chain scenarios in `07-testing.md`): invitation (user/platform,
  PRF/no PRF, cancel variants), login/unlock, recovery, passkey add/remove, first task countdown (on time, delayed,
  offline), chat and controls, read-only rights, terminal follow/history and no-input assertion (keystrokes produce
  no request), whiteboards and notify, notifications, members/rotation batch, workgroups, devices, GitHub connect,
  model settings, monitoring, audit, platform invites, security page, CSP-violation listener on every page, mobile
  viewports.
