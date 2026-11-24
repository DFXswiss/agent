# 06 Web client

> As built: where the implementation refines or deviates from this document, see the register
> [`spec-deviations.md`](../spec-deviations.md#06-web) (DOC-01).

Status: binding. Implements the user-facing parts of KONZEPT §1–§13 in `web/` (former repository agent-core, ADR-061). Uses the contracts of
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
e2e/                      Playwright specs for web-only flows against a hub test server (full E2E in e2e/platform)
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
| `/invite` (code in fragment `#…`) | invitation (§3.1), with the inviter's personal message of the package; with agent pairing parameters after the code: new account through the user's agent (`11-agent-access.md` §5.2), with the agent's request message (`t`) in the agent block | anon |
| `/connect` (pairing in fragment `#…`) | confirm the pairing of the user's AI agent with one passkey gesture (`11-agent-access.md` §5.1); shows the agent's request message (`t`) as a quote | anon → user |
| `/login` | login and unlock (§3.4) | anon |
| `/recover` | recovery (§3.5) | anon |
| `/o/:org` | home: first-task composer (§4) and recent tasks of the default workgroup | org:member |
| `/o/:org/tasks` | task overview: all tasks of the readable workgroups (§5.1a, KOR-32) | org:member |
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
| `/settings` | account: passkeys, recovery code, language, model access, credentials, GitHub, quotas, connected agents (§3.6, §8, §8.3) | user |
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
| `intro` | what the invitation grants: org name and workgroup names (from the package fields `org_name`/`wg_names`), the inviter's personal message when the package has one (`message`, rule E-28: a quote marked as the inviter's words, plain text; also on `existing_account`), role, expiry, "You have not joined <org> yet"; org invitations: "I already have an account – sign in and join" first (the login ceremony, unlock and `accept` in one action), then "New here – create an account with a passkey" with the display name, and a sentence against a second account by mistake; platform invitations in a browser with a stored session: "This invitation would create a second, separate account" with "Go to my account" and "Create a second account anyway"; the step waits until the stored session was restored (a session still starting never lands in the new-account path); with a valid session the client goes to `existing_account` directly (KOR-36) | – |
| `existing_account` (org invites) | "Join <org> with your existing account", "You have not joined <org> yet"; one main button: "Join <org> as <name>" when unlocked (the name of the account's own `member.profile` in its orgs), else "Join <org> with my account" (one passkey gesture signs in, unlocks and joins); the name is the account's, "Use another name in this organization" opens the field (asked for only when no org of the account has a name); side paths "Use another account", "Create a new account instead" (KOR-36) | login ceremony §3.4 if locked, in the same action; then `accept` with the registered identity (`04-crypto.md` §5.2a); `invite_registered` → error state |
| `create_passkey` | gesture 1 explained ("Your device will ask you to create a passkey") | `POST /v1/passkeys/register/begin` (ES256 only), `navigator.credentials.create()`, `POST /v1/passkeys/register/finish` (incl. the self-wrap of the user's new `CK_1`) |
| `unlock_passkey` | gesture 2 explained ("Confirm once more to sign in") | login ceremony §3.4; `PUT /v1/me/bundles/{slot}` with the PRF output |
| `prf_required` | §3.3 – the new passkey gave no PRF output: the registration is withdrawn, "Try again with another passkey" returns to `intro` (KOR-22); with PRF the flow goes from `unlock_passkey` straight to `accept` | `DELETE /v1/me/registration` |
| `accept` | progress "Joining …"; a failure (hub unavailable, new release) says "You have not joined yet – try again" with "Try again" in the same tab (the code stays in the screen's memory); a retry after an accept whose answer was lost finds the membership (`GET /v1/me`) and continues with `done` | `POST /v1/orgs/{org}/log` (`accept`) or `POST /v1/orgs` (genesis for platform invites, §3.1.1) |
| `done` | navigate to `/o/:org` with the first-task composer (§4); after an org invitation the header shows "You are now a member of <org>" until closed (KOR-36) | – |
| `missing` | no code in this tab (reloaded after the code left the address): "The invitation link is no longer in this tab … Nothing was joined. Open the invitation link once more." (KOR-36) | – |

Cancel handling (`AbortError`, `NotAllowedError`): return to the previous step with "Passkey creation was
cancelled – try again"; never create partial users twice (register/begin is repeated with a new ceremony; the hub
ignores the abandoned one).

#### 3.1.1 Platform invitation

No package. After gesture 2 (with PRF) the client creates the private organization: name "Private" (`de`: "Privat"),
default workgroup "Default" (`de`: "Standard"), `OK_1`, `WK_1`, self-wraps (the user's `CK_1` exists since registration), `genesis{private:true}`, profile
events, `member.profile` with the display name entered on the intro screen (required, 1–80 chars), all in
`POST /v1/orgs` with `platform_invite_id` (`04-crypto.md` §5.3). Org invites also ask for the display name and send
`member.profile` right after `accept`.

### 3.2 Recovery code (KOR-22, ADR-055)

No step of the registration asks for the recovery code, and nothing re-types it.

- **Passkey with PRF (normal case):** after gesture 2 the platform opens. No code exists (the unset recovery slot of
  `04-crypto.md` §4.1) and none is shown; no reminder, no banner, no dialog.
- **Code display** (wherever a code is shown): one screen with the code in groups `XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX` in a
  monospace block, "Copy", "Download as text file" (file generated client-side via `Blob`, name
  `agent-recovery-code.txt`), one line saying what it is for here, and **one** button. No re-typing, no "Skip".
- **Passkey without PRF:** not accepted for a new account (§3.3 "PRF required").
- **Settings** ("Passkeys and recovery", §3.6): "Recovery code" with one line of purpose and, while no code exists (the
  slot is the unset one), one quiet line "You have no recovery code yet." The button reads "Create recovery code", or
  "Create a new recovery code" once one exists. The browser draws the code, seals the identity in memory with it and
  shows it with "Done"; "Done" runs `POST /v1/me/recovery/begin`, the passkey gesture and `PUT
  /v1/me/bundles/recovery` with `assertion` (KOR-01); an old code stays valid until the hub accepted the new one. A
  cancelled gesture keeps the shown code for another try; "Cancel" leaves without sending anything.
- **After a recovery** (§3.5) the new code replaces the used one and is shown with "Continue".

### 3.3 PRF required; `prf_missing` guidance

**PRF required (KOR-22, ADR-055).** A passkey whose sign-in gives no PRF output is not accepted – at registration
(gesture 2 of §3.1), when adding a passkey (§3.6) and for the new passkey of a recovery (§3.5). One message for all
three: "This passkey cannot open your data. You need a passkey that can open your data.", one line about what happened
(registration: no account was created, the invitation is still valid; adding: the passkey was not added; recovery:
the passkey was not added, the recovery code still works), what works (the passkeys built into the device: iCloud
Keychain on Apple devices from iOS 18 / macOS 15, Google Password Manager on Android and in Chrome), what does not
(Bitwarden currently does not support PRF for the passkeys it stores), the hint to delete the unusable passkey in the
password manager, and "Try again with another passkey". The provider list lives in one place, the `passkeys`
catalog (`passkeys.prf-required.*`). Clean-up before the message: registration – `DELETE /v1/me/registration`
(`03-api.md` §4.1) deletes the unfinished account and frees the invitation at once, then the browser signs out; if the
hub cannot (older hub), the message adds that the invitation is usable again after 24 h at the latest (orphan job);
adding – the previous session removes the new passkey (`DELETE /v1/passkeys/{id}`, which also ends its new session);
recovery – the new passkey removes itself (its session ends), the browser signs out, and "Try again" begins the
recovery again with the same code (nothing was replaced).

**`prf_missing` guidance** – for accounts that exist already (made before KOR-22, or a PRF passkey that signs in from a
browser or password manager that gives no PRF output, e.g. a synced passkey in a browser without PRF support). Shown
when the login ceremony returns no PRF result (detected **per login**, `04-crypto.md` §5.4):

1. Explain: "This passkey cannot open your data on this device."
2. Options: (a) enter the recovery code (unlock only: bundle from the signed `GET /v1/me/bundles/recovery`, no rotation and
   no new passkey; the §3.5 recovery flow is only for users without any working passkey); (b) "Remember this browser":
   after unlocking, create a non-extractable AES-GCM key in IndexedDB that encrypts the identity bundle locally
   (`persist/device-key`); next login on this browser unlocks with it; (c) add a PRF-capable passkey from another
   signed-in device (§3.6).
The device key is per browser profile, listed in `/settings` ("This browser is remembered and opens your data even
without a suitable passkey") with
"Forget" (deletes the IndexedDB entry).

### 3.4 Login and unlock (`/login`)

One button "Sign in with passkey" → `POST /v1/passkeys/login/begin` → new session key (P-256, non-extractable) →
statement → `get()` with PRF → `POST /v1/passkeys/login/finish` → decrypt bundle with `KEK_i` (or device key / recovery
code per §3.3) → load `GET /v1/me`, verify logs of all orgs (§11.3), fetch wraps (`GET /v1/wraps`) and open scope keys
→ route. Unlock failure after a valid login (`decrypt_failed`) shows §3.3 options.

### 3.5 Recovery (`/recover`)

Enter code → `POST /v1/recovery/begin` → decrypt recovery bundle → sign proof → `POST /v1/recovery/finish` → create a
new passkey (gesture 1) → login (gesture 2) → upload passkey slot (a new passkey without PRF is refused, §3.3) → **new
recovery code** (§3.2: stored at once, shown with one "Continue", no re-typing) → home. Rate-limit responses (`429`) show the retry time.

### 3.6 Passkeys in `/settings`

List (`GET /v1/me`: created, last used, `prf_seen`), "Add passkey" (`04-crypto.md` §5.6; option "on another device"
uses the platform's hybrid/QR UI of `create()`; a passkey without PRF output is removed again, §3.3), "Remove" (`DELETE /v1/passkeys/{id}`; disabled for the last one,
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

Newest activity first (`GET /v1/workgroups/{wg}/tasks?order=activity`, KOR-32), with decrypted title, state badge,
owner display name, last activity; filter by state; infinite scroll (`next_before`). Composer at the top (same as §4.1) for users with
`wg:write`. Task creation is possible in any workgroup with write access.

**Being unlocked (ADR-058 §2).** When the user may read a workgroup but holds no key of its current epoch (typically an
admin reading it by inheritance before a key holder has delivered the key by catch-up), the task list keeps its
metadata and shows one calm line above it, and the task page and the workgroup whiteboard show that line instead of
their content: "Being unlocked – the content appears as soon as a member or device of this workgroup has been online."
No error, no code, nothing to do; it disappears with the key. If the verified log shows that no active principal other
than admins reading by inheritance can hold the key (no explicit member, no assigned device, the author of the current
epoch gone), the line says instead: "Older content of this workgroup can no longer be opened: nobody who could still share it is
left. New content becomes readable for you shortly." – the rotation job (§11, job 4) of an admin then rotates the
workgroup silently, so new content is readable for every reader again (`core/jobs/rotation.ts`, ADR-058 §2). The
line appears only once the org's job queue has run and is idle (`core/org/unlock.ts`).

### 5.1a Task overview (`/o/:org/tasks`, KOR-32)

Navigation entry "Tasks" directly below Home (sidebar, navigation row, phone bottom bar). All tasks of the workgroups
the user can read (from the verified log; others are never requested), newest activity first: the lists of §5.1 per
workgroup merged with per-workgroup cursors (a row is shown only when no unloaded row can precede it). Columns as
§5.1 plus the workgroup. Filters: state, workgroup, "only mine" (signed owner), title search (in the browser). Empty
state explains how to create a task. Details: `decisions/KOR-32.md`.

### 5.1b Resume notice (task page, KOR-33, ADR-072 §7)

Below the task header, `ContextNotice` reads `context_restore`/`context_reason` of the task's current main session
(`core/sessions/context.ts`; an older hub sends neither field and nothing is shown):

| `context_restore` / `context_reason` | Shown (en / de) |
|---|---|
| `restored` / null | muted line "Continued with the full context of the original session" / "Fortgesetzt mit vollem Kontext der ursprünglichen Sitzung" |
| `restored` / `workspace_partial` | amber line "Continued with the full conversation; the workspace was only partly restored." / "Fortgesetzt mit dem vollständigen Gesprächsverlauf; der Arbeitsstand wurde nur teilweise wiederhergestellt." |
| `restored` / `workspace_omitted` | amber line "Continued with the full conversation; the workspace was too large and was not restored." / "Fortgesetzt mit dem vollständigen Gesprächsverlauf; der Arbeitsstand war zu groß und wurde nicht wiederhergestellt." |
| `fallback` / reason | warning box (`role="status"`) "This task was continued by a new AI because {reason}." / "Diese Aufgabe wurde mit einer neuen KI fortgesetzt, weil {reason}." with the reason texts of `catalog/resume.*.json` (`context_missing`, `context_invalid`, `context_unavailable`, `version_incompatible`, `restore_failed`; an unknown code gets the generic text) |

A restored row with a reason the client does not know counts as a full restore (the conversation is complete). The
error catalog has texts for `quota_exceeded` (a checkpoint over its quota; shown only where a device error reaches a
user).

### 5.2 Chat (`/o/:org/t/:task`, tab Chat)

- Timeline from events of the wg scope filtered by `task` (`GET /v1/scopes/{wg}/events?task=…` + SSE), ordered by
  `(ts, scope_seq)`:
  - `prompt` – right-aligned bubble with author name.
  - `session.output` – Markdown (§2.1 rules), per session; subagent outputs are indented with the subagent label.
  - `session.tool` – collapsed row "Read src/app.py", "Edit …" (expand: input/result summaries, error flag).
  - `operation.params` / `operation.result` + `operation` rows – inline card with type, state badge (live), and
    result links (PR URL, commit SHA, comment URL, issue number and URL, connected login); duplicates show
    "duplicate of …" linking the original. Listed issues and pull requests (`github.list_issues`,
    `github.list_pull_requests`) show number (linked), title, state, draft and labels, with "more on page n" and
    the size-limit notice; a read issue (`github.get_issue`) shows title, author, state reason, its text and its page of
    comments; `github.update_issue` shows the new state and reason (KOR-25). Texts from GitHub are untrusted: they are
    rendered only as text (never as Markdown or HTML), and only `https:` URLs become links.
  - `session.file` – "Changed `path`" with an expandable patch (monospace, no syntax highlighting dependency). The
    patch is rendered only while expanded and at most 20 000 characters at a time ("Show more"). Two or more
    consecutive `session.file` items of one session are one row "N files changed" with the time span (HOT-05); it
    expands to the list of files, 50 at a time ("Show more files"), each with its own expandable patch.
  - `session.hook` with `deny` – muted row "Blocked: <tool> (<reason>)".
  - `session.status` errors and task/session state changes – system rows.
  - `session.status` `waiting` (KOR-25, `03-api.md` §10) – a card for the task owner only: "waiting for the GitHub
    connection" with Connect (device flow or existing token, §8.1) right in the card, or "Allow repository
    `<owner/name>` for this workgroup" with the allow action for users who may write the workgroup settings. It shows
    only platform text and the checked repository name, never text of the session, and disappears once the condition
    is met (GitHub connected, repository allowed), the operation is registered or `until` has passed.
  - Countdown summaries (§4.4).
- Long prompts, messages and tool inputs/outputs show 20 000 characters at a time ("Show more"); a tool row renders its
  details only while expanded.
- Long chats (HOT-05): only a window at the end of the timeline is rendered (100 rows; "Show earlier items" adds 50;
  the window keeps its first row while new items arrive; "Jump to the latest item" scrolls to and focuses the last
  row); rows are memoized on their content, so a new event renders only the new row; rows off screen skip layout and
  paint (`content-visibility: auto`; rendered rows stay in the accessibility tree and in the browser's find-in-page,
  which does not reach rows outside the window, closed details or text beyond the shown characters). Kinds
  with a verified body but without a renderer are skipped and do not break a run of file changes; an event kind the
  client's kind matrix does not know yet stays the neutral row "Unverifiable item (kind_not_allowed)". The
  reader writes settled events to the store at most once per frame (16 ms), so a burst of thousands of events costs
  a few store updates.
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

`GET /v1/tasks/{task}/operations`, newest first; detail drawer shows transitions, decrypted params and result, with
the same readable views of issue and pull request lists and of a read issue as the chat card (§5.2).

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
  and, for rows of the retired kind `rotation.required` that the hub no longer creates (ADR-058 §3), the neutral
  "Access in X is being updated"); click marks read and navigates; "Mark all read".
- Toasts for live notifications (auto-dismiss 6 s, accessible live region).

## 7. Organization management

### 7.1 Members and rights (`/o/:org/members`)

- List: members (display name from `member.profile`, role, workgroups with access), devices are on §7.3. An access a
  member has only because it is an admin (ADR-058) is shown as "reads as admin" and has no ungrant control.
- Admin actions:
  - **Invite user:** role (member default, admin), workgroups with read/write (default workgroup always write),
    expiry (72 h default, 1 h–30 d), an optional personal message for the invitee (one line, up to 500 characters,
    sealed in the package, rule E-28). Result screen shows link `<origin>/invite#<code>` and the code, with copy buttons
    and a warning to transmit confidentially; the code is not shown again. The invitations list shows each user
    invitation's state – "Not opened yet", "Link opened – not joined yet", "Account created – not joined yet" (with
    revoke), and "Accepted – <name> is a member" until it expires; acceptance comes from the verified log, opened and
    registered from the hub's list (`03-api.md` §4.2, KOR-36).
  - **Add an existing account (ADR-060):** the account code the person sees in its settings (or one of the accounts
    registered through the admin's own platform invitations), role, workgroups, one button. The identity is checked
    against the platform log before the button is enabled (`core/org/admit.ts`); the person does nothing and finds the
    org at its next sign-in, with the notification `org.admitted`. Settings → Passkeys shows the user's own account
    code (computed from the unlocked identity). An org whose log holds an entry type the bundle does not know is shown
    as "a newer version is available – reload" instead of the blocking error.
  - **Change role** (`role` entry); last-admin protection: the control is hidden for the last admin, the hub
    returns `last_admin` otherwise. Demoting an admin goes through the same batch as a removal (the rotation of every
    workgroup it read only as admin is added automatically); the dialog says in one sentence that already read
    content stays with the person. An admin that demotes itself owes nothing in its batch: the rotation becomes
    pending (job 4).
  - **Remove member** and **ungrant** (of an explicit access only; an admin keeps its inherited read): confirmation
    dialog explaining "already read content stays with the person", then the client builds the atomic batch (entry +
    `rotate` for every affected scope + wraps for all remaining recipients) while the dialog shows only "Saving …"
    (key handling stays out of sight, ADR-058 §4) and sends one `POST /v1/orgs/{org}/log`. On `seq_conflict` the
    client re-reads the log and rebuilds the batch automatically (max 3 attempts; "attempt 2 of 3" is shown).
  - **Grant workgroup access** (read/write) on any workgroup that is not archived, with an option "also give access
    to older content". The batch carries the wraps the admin holds; it does not need to hold the key itself – the
    key holders deliver the rest by catch-up. Granting `read` to an admin makes the access explicit (it then survives
    a demotion).
- Members can **leave** the org (self `remove`; for the last admin hidden).

### 7.2 Workgroups (`/o/:org/workgroups`)

List with names (decrypted `wg.profile`), my access, devices, archived flag. Admin: create (new `WK_1`, one batch with `wg_create` (the creating admin gets write), `grant` entries for chosen
members, self-wrap + wraps for those members, every other admin and chosen devices (`device_assign`), `wg.profile`
event); the note says "Every admin of the organization can read it. You can write." (ADR-058), rename
(`wg.profile`), archive (not the default workgroup).

### 7.3 Devices (`/o/:org/devices`)

- List: label (decrypted `device.profile`), kind (platform/org), online state, last seen, agent version, assigned
  workgroups, capacity bar, **capacity-low badge** when `capacity_low` (`03-api.md` §4.6.2).
- **Add org device** (admin): choose workgroups → code + instructions block:
  `agent-runner enroll --hub <origin> --code <code>` (copy button), expiry 24 h default; the device appears live when
  it accepts.
  Invitations carry the keys of the chosen workgroups, so only workgroups whose current key the admin holds are
  offered ("You can add a device as soon as a workgroup is unlocked for you").
- **Assign platform device** / **assign to a further workgroup** (admin; any writer in a private org): every
  workgroup that is not archived; candidate list (`GET /v1/workgroups/{wg}/device-candidates`), platform-log
  verification, batch. The batch wraps the keys the admin holds; the transition rule of ADR-058 §2 (`compatSkip`)
  withholds a workgroup key the admin reads only by inheritance and did not create, which the key holders deliver by
  catch-up.
- **Unassign** from a workgroup / **Unpair** (remove from org): confirmation ("the device loses access to new
  content; content it already read stays on the device") + automatic rotation batch (§7.1).
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
- **Store an existing token (KOR-25):** paste field for an existing GitHub token (GitHub CLI login, personal access
  token); it leaves the browser only HPKE-encrypted to the device in the device request `github.connect` `{token}`;
  the device checks and stores it (`05-runner.md` §11.4). Afterwards "Connected as `<login>`" with the origin (device
  flow / token) and the scopes; fine-grained tokens are recommended, not enforced.
- **Composer hint:** a prompt with an obvious GitHub reference (`owner/name`, a GitHub URL, "issue", "pull request")
  while GitHub is not connected offers Connect next to the composer; the task is sent anyway.
- **Disconnect:** `DELETE /v1/me/credentials/github` + link to the GitHub settings page to revoke the app
  authorization.
- Workgroup settings (`/o/:org/w/:wg/settings`, `wg:write`): optional repository allow-list (`owner/name`, validated
  with the regex of `03-api.md` §9; empty = every repository of the GitHub connection, ADR-054), protected branch patterns (defaults prefilled), PR draft default; saved as
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
- Credentials list with version and "Delete". A `claude_subscription` record shows its origin ("long-lived token" for
  `setup_token`, "from the Claude Code login" for `login_token`, ADR-056) and, for a login token, its expiry
  (an expired one highlighted).

### 8.3 Connected agents (KOR-24, `11-agent-access.md`)

- `/settings` → "Connected agents": `GET /v1/me/agents`, each with name and computer (opened from the sealed label),
  created, last used and state; "Disconnect" (`DELETE /v1/me/agents/{id}`) after a dialog that says the agent loses
  all access at once and keeps what it has already read. The notice after a pairing (`?agent=connected|failed`) is
  only a hint; the state shown is always the hub's list.
- The audit view (§9.2) marks rows with `agent_id` "by agent <name>" ("by agent" when the name is not the user's).

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

`npm run build` (Vite) writes to `src/agent_hub/static/`: `index.html`, `assets/<name>-<hash>.js|css`,
fonts, icons, `build-manifest.json`. All asset names are content-hashed; `index.html` is the only unhashed file and is
served with `Cache-Control: no-cache`; assets with `immutable, max-age=31536000`. The hub serves `index.html` for all
SPA routes (§2.4) and `/assets/…` statically.

### 10.2 CSP and SRI

- The CSP of `03-api.md` §1 applies to every response; therefore: no inline `<script>`/`<style>`, no `style`
  attributes in `index.html`, no `eval`/`new Function`, Tailwind compiled to a file, fonts self-hosted.
- Every `<script>` and `<link rel="stylesheet">`/`modulepreload` in `index.html` carries `integrity="sha384-…"` and
  `crossorigin="anonymous"` (Vite plugin in-repo, tested).
- **Every script the hub delivers is integrity-checked, including the lazily loaded chunks of §12.2** (screens,
  terminal, charts, markdown): the integrity of every chunk is fixed in `index.html`, so the browser refuses a chunk whose bytes
  differ. Built variant (KOR-04): `<meta name="asset-integrity">` in `index.html` holds the `sha384` of every script
  and stylesheet under `/assets/`; the in-repo preload helper (`web/src/core/assets/integrity.ts`) loads every file a
  dynamic `import()` needs through `modulepreload`/`stylesheet` links with `integrity` before the import, always
  including the chunk itself, and rejects a file without a known or matching hash (fail closed); preload links precede
  the entry script (checked by the build). `npm run build` fails when a chunk can be loaded without an integrity value. This settles the former
  conflict between `04-crypto.md` §9 ("SRI for all assets") and this section (only `index.html` tags) in favour of
  §9 (AUD-01 A-08, DOC-01 decision 4); browser test with a manipulated chunk: `web/e2e/lazy-integrity.spec.ts`
  (Chromium; Safari and Firefox on the manual checklist, §12.7).
- Libraries that inject `<style>` elements at runtime are forbidden; xterm.js and uPlot styles are imported as
  static CSS files. The terminal uses `@xterm/addon-webgl` (fallback `@xterm/addon-canvas`), not the DOM renderer,
  which injects `<style>` elements. M0 verifies that no runtime `<style>` injection remains; if one does, the only
  permitted relaxation is `style-src-elem 'self' 'unsafe-inline'` through an amendment of ADR-019 (script policy
  stays strict). A Playwright test fails on any CSP violation report (`securitypolicyviolation` listener).

### 10.3 Reproducible build

Pinned `package-lock.json`, `npm ci`, Node version in `.nvmrc`, `SOURCE_DATE_EPOCH` = commit time, no timestamps or
absolute paths in output. Two builds from the same commit MUST be byte-identical (CI job compares), whatever the
operating system, the checkout directory and the time: build rules look only at paths relative to `web/`, never at
absolute module ids (HOT-18), and `make platform-ci-linux` builds on the runner device and in the Linux VM below another
directory and fails unless the build hashes are equal. The
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
   A run that fails with a transient hub error (`unavailable`, including an unreachable hub, or `rate_limited`) fires
   its trigger again after a backoff of 1 s → 30 s with jitter until a run succeeds; a refusal still blocks the org
   (KOR-05).
2. **Wrap verification:** every fetched wrap is checked (signature, W1–W2); violations are ignored and counted
   (`wrap_unauthorized`).
3. **Catch-up wrapping:** after every log change and every 10 minutes: `GET /v1/orgs/{org}/catch-up`, create the
   wraps the user may create, `POST /v1/wraps`. The browser confirms exactly the slots `catchUpEpochs` gives (the
   current epoch; every epoch of a workgroup for an admin reading it by inheritance, ADR-058 §2) and, until every
   runner verifies ADR-058, never wraps a workgroup key for a device when the user reads that workgroup only by
   inheritance and did not create the epoch (`compatSkip`).
4. **Pending rotations:** if `rotation_pending` is non-empty and the user is admin (or wg writer for wg scopes):
   build and send the rotation batch automatically and silently (no notice, ADR-058 §3).
5. **Credential re-wrap:** when a device becomes eligible for the user's `CK` (new assignment in a workgroup where the
   user has explicit access; the inherited read of an admin does not count, ADR-058 §1) wrap `CK` for it; when a
   device lost eligibility, rotate `CK` and re-encrypt all own credential records (`POST /v1/me/credentials/rotate`)
   silently; only a renewal blocked by records the browser cannot open shows one sentence asking the user to delete
   or replace them.
   Additionally every device in `cred_holders` of `GET /v1/me` that no verified log of the user's orgs entitles
   counts as lost: rotate `CK` (and delete the records it wrote) – also for orgs whose log this browser does not hold
   (KOR-04; waits while an org has no verdict yet).
6. **Self-wraps after accept:** store wraps of the package keys for the own identity (`04-crypto.md` §5.2).
7. **Session renewal reminder** (§2.6).

## 12. Smartphone, accessibility, time, connectivity, errors

### 12.1 Layout

- Mobile-first Tailwind; breakpoints `sm` 640, `md` 768, `lg` 1024 px. Below `md`: bottom navigation (Home, Tasks (the overview of §5.1a),
  Notifications, Monitoring, More), full-width screens, drawers as full-screen sheets; from `lg`: left sidebar
  (orgs, workgroups) + content.
- Touch targets ≥ 44 × 44 px; no hover-only functions; composer stays above the on-screen keyboard
  (`visualViewport`).
- Terminal on phones: horizontal scroll at fixed geometry, pinch-zoom allowed on the terminal container only,
  "Follow" toggle reachable with the thumb.

### 12.2 Performance budget

Initial JS ≤ 300 KiB gzip (route-level code splitting: every screen except sign-in, the start redirect and not-found,
the terminal, charts and Markdown load lazily); time to interactive ≤ 3 s on a mid-range phone profile (Playwright CPU throttling ×4,
"Fast 3G"); measured in the E2E suite (median of three loads, no tolerance).

The build enforces a tighter guard than the ceiling: `INITIAL_JS_BUDGET` in `web/build/budget.ts` is the measured first
load plus 10 % (HOT-16: 199.3 KiB measured, guard 219 KiB). The guard runs in every package's gates (`npm run
build:check`, the build tests and the E2E budget test), so growth of the first load is seen deterministically where it
is caused, not later as a narrow timing failure. It is raised only together with a reason in this section. The first
load is three scripts: the framework, the app modules imported statically from the entry (chunk group `$initial`) and
the bundler runtime; what only lazy chunks share stays out of it. The lazily loaded screens share one chunk
(`screens`), loaded on the first switch to any of them; Markdown, charts and the terminal keep chunks of their own with
their libraries. While a screen's chunk loads, its frame is shown at
once (`screen.loading`), also on the first switch to it. A chunk that cannot be loaded or fails its integrity check
(section 10.2) shows "This page could not be loaded" with a reload in place of the screen, inside the shell, so the
connection banner stays; other errors reach the route's error page. A direct link into a lazy screen waits for one more
chunk after the first load.

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
(registration with a security key, login, chat), Chrome Android (Google Password Manager passkey); in Safari and
Firefox also that a changed lazy chunk or stylesheet is refused (§10.2, automated in Chromium only). Results recorded in
`docs/platform/manual-checks/<version>.md`.

### 12.8 Community

The community screen (`/community`), its navigation entry with the unread badge, the settings section "Account" and
the background job are specified in `14-community.md` §6 (KOR-26, ADR-069).

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
