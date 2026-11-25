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
decrypted event bodies, metrics, connection state, job state. Scope keys and the identity private keys live only in
memory; a page reload requires the passkey (or recovery code / device key) again. The store is updated only by core
modules; React reads via selectors.

Volume bounds (ADR-086 §1, `decisions/KOR-35.md` §4):
- **Event store.** A bounded store per scope in two classes with a bound each: the bulk kinds of sessions (`session.output`,
  `session.tool`, `session.file`, `session.hook`, `session.terminal`, `usage`) and every other kind (titles,
  whiteboards, profiles, prompts, statuses, operations); each class holds at most 5 000 events and 64 MiB
  (ciphertext plus decrypted body), so chat traffic never evicts titles or whiteboards. A class over its bound drops
  its oldest events (lowest `scope_seq`) first. Eviction never leaves a silent gap: every list's page state (`pages`
  slice: scope, query, boundary = its `before` cursor) whose boundary lies at or below an evicted event is raised
  just above it, so "Earlier" reads the evicted rows again; every mounted list (the newest page a view just opened
  as much as pages read back with "Earlier") keeps its rows at or above its boundary up to twice the bound before
  the oldest go, so bulk inflow of other tasks never empties a chat that was just opened. Task titles keep their read position in the same
  slice, so an evicted title is read again. Terminal chunks of watched sessions are kept in the `terminal` slice
  (§5.5).
- **Shown tasks.** Mounted views register the tasks and sessions they show (`core/store/focus.ts`,
  reference-counted). A live event of a bulk kind whose header names neither is not decrypted and not stored (only
  the channel's cursor moves); the view that opens such a task later reads its newest page.
- **Loading.** Chat, session chat and operations read the **newest** page of their query
  (`GET /v1/scopes/{scope}/events?before=…`, `core/net/pages.ts`) and older pages only on "Earlier"
  (`core/tasks/pages.ts`, state in the `pages` slice); stored bulk events below a newest page read again after a
  skip lie before a gap and are dropped (except what another mounted list shows at or above its boundary); a view
  renders only events at or above its list's boundary, and a re-read of
  the newest page raises the boundaries of overlapping lists of the same scope (e.g. the session chat). A forced
  refresh (after `resync`) while a read runs queues one fresh read after it instead of joining the running one. Single pulls (profiles, one task's title or prompts, an operation's events)
  read one newest page; a board at most 10; task titles of a list are read per workgroup, newest pages first, at
  most 10 pages per call. Nothing loops over all pages. An old hub that refuses `before` (`400 request_invalid`) is
  asked once more without it and served by bounded ascending windows from then on: the scope head is cached per
  client and scope (only the first page reads it); when the windows below the head find nothing, a search by halves
  finds the newest older match, so a chat whose content lies far below the head never opens empty (the view shows
  its loading state meanwhile; an emulated page is empty only when nothing older exists).
- **After `resync`** the channel pulls only the non-bulk kinds up to the head; every mounted view reads its newest
  page again. A `resync` that arrives while an earlier one still waits in the channel's queue is dropped (the waiting
  one covers both): a burst of oversized frames brings hundreds of them.
- **Timeline.** A change of the events alone is merged into the sorted timeline (new and replaced items only); a
  change of the task or its sessions rebuilds it.

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
| `/o/:org/t/:task` | chat (§5.2), tabs: Chat, Sessions, Pull requests (§5.6), Operations, Whiteboard, Audit | task:read |
| `/o/:org/s/:session` | session detail: terminal (§5.5), chat of that session, children | task:read |
| `/o/:org/whiteboard` and `/o/:org/w/:wg/whiteboard` | whiteboards (§6.1) | org:member / wg:read |
| `/o/:org/members` | members and rights (§7.1) | org:member (edit: admin) |
| `/o/:org/workgroups` | workgroups (§7.2) | org:member (edit: admin) |
| `/o/:org/pulls` and `/o/:org/w/:wg/pulls` | pull requests of the default / a workgroup, "Fix conflicts" (§5.6, KOR-48) | wg:read (link, fix: wg:write) |
| `/o/:org/w/:wg/settings` | workgroup settings: repositories, protected branches, PR draft default (§8.1) | wg:read (edit: wg:write) |
| `/o/:org/devices` | devices (§7.3) | org:member (edit: admin / private-org writer) |
| `/o/:org/monitoring` | monitoring (§9.1) | org:member |
| `/o/:org/audit` | audit (§9.2) | org:member (scope per `03-api.md` §4.11) |
| `/o/:org/github` | company GitHub accounts of the organization (§8.1, KOR-47) | org:admin |
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
  An area whose texts only lazily loaded screens use may live in `core/i18n/lazy/<area>.<locale>.json` and is added
  to the catalogs by `core/i18n/lazy/<area>.ts` when the first of those screens loads (the area `model`, KOR-50), so
  it stays out of the first load (§12.2); the same parity test covers it.
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

### 4.0 Situation view (home of an organization, `/o/:org`, KOR-39, ADR-076)

The home of an organization shows where people are needed, across all workgroups the user reads:

- **Counters** at the top: "Waiting for me" (`needs_decision` tasks the user owns), "Stuck", "Running", "Finished
  today" (`GET /v1/orgs/{org}/attention/summary`, `since` = local midnight). The navigation entry Home (sidebar,
  navigation row, phone bottom bar) carries the "waiting for me" count as a badge. Below them the organization's
  subscriptions ("Subscriptions: n available · m used up – next release 14:05", `GET /v1/orgs/{org}/quotas`, refreshed
  every minute, a link to the section of §8.2; hidden without any subscription to list or when the hub cannot answer;
  KOR-50, [ADR-081](../adr/081-model-access-fallback-and-dfx-access-on-own-hardware.md) §6).
- **Groups** in this order: "Needs your decision" (`needs_decision`, longest waiting first), "Stuck" (`blocked`),
  "Running" (`working`), "Recently finished" (`done`, last 7 days, with the link to the default workgroup's task
  list); the last three newest first. Each group is a windowed list paged by the hub (50 per page; at most 50 entries
  in the DOM, moved by 25, the window of the chat bundling, §5.2); an empty group shows one short line.
- **Entry** (the row building block `TaskItem` of the task lists, §5.1/§5.1a, extended by an attention line):
  title (opened in the browser), workgroup, responsible person (the signed owner), main agent with its state, the
  reason in plain words (ADR-076 §1 table), last activity and running time; "Running" also shows the phase and the
  number of active subagents. Expanding an entry loads its session tree (`GET /v1/tasks/{task}`): main agent,
  subagents indented below the agent they report to, each with its state (at most 100 shown, the rest counted).
- **Act without opening the task** (the next step, never only an explanation, E-05): `question` shows the agent's
  question (the sealed `operation.params` opened in the browser) with an answer field (sends a follow-up prompt);
  `approval` shows the question with "Approve" / "Decline" and an optional comment (a prompt starting with
  `Approved.` / `Declined.`, ADR-076 §8); `connection_missing` starts the GitHub connection (§8.1);
  `device_missing` leads admins to assigning a computer (§7.3), others see who can do it; `model_access_missing`
  opens the model settings (§8.2); `credit_empty` says that the operator sets the balance and offers "Continue";
  `session_failed`, `guest_error`, `device_unreachable`, `no_capacity` offer "Try again" (a follow-up prompt starts a
  new main session); `device_failed` ("Interrupted – the computer failed" / "Unterbrochen – der Rechner ist
  ausgefallen", `blocked`, ADR-087) offers "Choose a computer automatically" (`PUT /v1/tasks/{task}/placement` with
  nulls) only for a task pinned to the failed computer, otherwise nothing (the task moves by itself); `quota_choice` ("Your subscription is used up until 14:05 – continue with DFX access?") offers
  "Continue with DFX access" (`POST /v1/tasks/{task}/model-consent`) and, to the task's owner only, "Always" (the
  setting `fallback.own_to_dfx` of §8.2), with the note that DFX access is charged and that the task otherwise
  continues by itself; `quota_wait` names the time ("Waiting for model quota – continues at 14:05") and needs nobody
  (ADR-081 §5); every entry offers "Open task". Users without write access see the entry without controls.
- **New task**: the composer of §4.1 stays at the top in a compact form (context line with the workgroup selector and
  the prompt field), so a new task is always one step away; its suggestion chips are shown while the organization has
  no task yet, otherwise behind "Suggestions". Users without write access see a short note instead.
- Live: stream frames `attention` update the entry and the counters (batched, counters refetched at most once per
  second); works fully on a phone (single column, controls
  ≥ 44 px).

### 4.1 Entry

Right after the first login (and on `/o/:org` whenever the user has no task yet) the home screen shows the composer
with its suggestions open (at the top of the situation view, §4.0; with tasks the suggestions are behind
"Suggestions") with:

- **Context line:** organization (the invitation's org, else the private org) and workgroup selector preselected to
  the default workgroup; changing the workgroup lists only workgroups with `wg:write`.
- **Suggestions** (static, localized, clickable chips that fill the composer, never auto-send): "Research: …"
  (e.g. "Summarize the current state of …"), "GitHub pull request: …" (if GitHub is not connected the chip first
  offers "Connect GitHub", §8.1), "Explain this repository: …", "Write a short plan for …".
- Free-text prompt field (multi-line; send with button; `Ctrl/⌘+Enter` on desktop).
- **Device choice** (ADR-075 §6): a compact selector "Device: Automatic" listing the devices assigned to the chosen
  workgroup and, per device, "Any VM" and its live VMs with their sessions ("vm-1a2b3c4d · 3 of 16 sessions");
  preselected with the workgroup's default. The choice goes into `POST /v1/tasks` as `placement`.

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
   device". If not allowed or no candidate the task is still created and waits (step 2; §4.4 shows the follow-up).
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
- `estimate_ms` is null (`scheduling`): instead of digits the panel shows the real reason, with elapsed time, in
  two kinds (ADR-075 §7): **waiting for room** – `capacity_exhausted` ("All execution devices are full – the task
  starts by itself as soon as there is room"), `wg_vm_limit` ("The workgroup uses as many VMs as allowed – the task
  starts when one of them has room"); **waiting for a device** – `device_offline` ("The execution device is offline
  – the task starts as soon as it is back"), `no_device` ("No execution device is assigned to this workgroup" with
  the button "Assign a device" for admins, which assigns the first candidate as in §4.3 step 1, else "An admin of the
  organization can assign one"), `platform_access_required` ("This organization has no execution device yet – the
  operator has been notified"). A chosen device that is busy or offline says so with its name. **Interrupted**
  (category `interrupted`, ADR-087) – `device_failed` ("Interrupted: the device failed – the task continues once the
  device is back or another suitable device can take it over"); "Choose automatically" only while the task is
  pinned to the failed device.
- The task header and the sessions tab name device and VM of the running session ("on <device> · vm-…").
- `working` reached: panel collapses into a one-line summary "Ready in 4.2 s" (kept in the chat timeline).
- `failed`: error from the catalog (`state_reason`) and "Retry" (sends the same prompt as a follow-up, §5.2).

## 5. Tasks, chat, sessions, terminal

### 5.1 Task list (`/o/:org/w/:wg`)

Newest activity first (`GET /v1/workgroups/{wg}/tasks?order=activity`, KOR-32), with decrypted title, state badge,
owner display name, last activity; filter by state. Composer at the top (same as §4.1) for users with `wg:write`. Task
creation is possible in any workgroup with write access.

**Volume (ADR-086 §1).** The rows are a windowed list (`tasks.list`; the home screen's recent tasks use the same list
with 5 rows and no paging): newest first, 100 rows at first, 50 more per "older" move, at most 200 rows rendered;
"Load more" (`next_before`, 50 per page) appears only when the window reaches the loaded end and loads on request only
– no automatic infinite scroll. Titles and owners of the rows come from one store selector per list; rows are memoized
on their content; time formats are created once per locale.

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
§5.1 plus the workgroup; the same window and "Load more" as §5.1 (list `tasks.overview`). Filters: state, workgroup,
"only mine" (signed owner), title search (in the browser). Empty state explains how to create a task. Details:
`decisions/KOR-32.md`.

**Bounded search.** Titles are encrypted, so "only mine" and the title search filter the loaded rows. One search request
(a new search text or filter) loads at most 10 further pages, one page of the binding list at a time, and stops earlier
once 50 rows match or no list has a further page; a load already running is awaited, a replaced request stops. Then the
list says "Searched the N most recent tasks." ("Searched X of N tasks." once the hub reports a total) with a "Search
further" button that starts the next request of at most 10 pages. While a search is active, "Search further" replaces
"Load more".

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

**Device failure** (KOR-58, ADR-087 §6). `DeviceFailureNotice` reads `interrupted_at` and `failover_of` of the current
main session (`core/sessions/failover.ts`; missing or malformed: nothing is shown):

| Field | Shown (en / de) |
|---|---|
| `interrupted_at`, session not `stopped`/`failed` | warning box (`role="status"`) "Interrupted – the device has been unreachable since {time}. The task continues once the device is back or another suitable device can take it over." / "Unterbrochen – das Gerät ist seit {time} nicht erreichbar. …", with "Choose automatically" for writers while the task is pinned to the failed device |
| `failover_of` | muted line "Continued on another device after a device failure (from the last saved checkpoint)" / "Nach einem Geräteausfall auf einem anderen Gerät fortgesetzt (Stand des letzten gesicherten Checkpoints)" |

The Sessions tab marks a session `stopped` with `device_failed` "Cancelled because the device failed" / "Wegen
Geräteausfall abgebrochen"; the error catalog has `device_failed` (session end reason, scheduling reason, operation
error code).

### 5.1c Model switch line (task page, KOR-50, [ADR-081](../adr/081-model-access-fallback-and-dfx-access-on-own-hardware.md) §5)

Below the resume notice the task page shows the newest switch of the model access of one of its sessions (audit row
`session.model_switched`, read from `GET /v1/orgs/{org}/audit?action=session.model_switched&task=`): "Model switched:
Claude subscription → DFX access over OpenRouter (model) · used up until 14:05" (cause `exhausted` with or without
the time, `unusable`, `released`; an operator subscription is named "operator subscription"). It is read when the page opens, when the task or its main session changes state and
after a reconnect; without a switch, or when the audit cannot be read, nothing is shown.

### 5.2 Chat (`/o/:org/t/:task`, tab Chat)

- When the main session failed with `model_access_unavailable`, the task's owner sees a card "Connect your Claude
  subscription – the task continues by itself once it is connected" that leads to the connection (§8.2); others see
  that the owner was asked. The hub continues the task when a model credential is stored (ADR-079 §4, E-05).

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
    expands to the list of files, 50 at a time ("Show more files", at most 200 rendered; beyond that the first files
    give way behind "Show previous files"), each with its own expandable patch.
  - `session.hook` with `deny` – muted row "Blocked: <tool> (<reason>)".
  - `session.status` errors and task/session state changes – system rows.
  - `session.status` `waiting` (KOR-25, `03-api.md` §10) – a card for the task owner only: "waiting for the GitHub
    connection" with Connect (device flow or existing token, §8.1) right in the card, or "Allow repository
    `<owner/name>` for this workgroup" with the allow action for users who may write the workgroup settings. It shows
    only platform text and the checked repository name, never text of the session, and disappears once the condition
    is met (GitHub connected, repository allowed), the operation is registered or `until` has passed.
    `waiting_for` `github_access` (KOR-65, ADR-077): "GitHub access needed for the repository" with the one next step
    of `next` – Connect right in the card (`connect`), a link that opens GitHub in a new tab (install the GitHub App
    for the owner, request the organisation's approval, authorise SSO, open the repository), or the pre-filled token
    link plus the token way (`new_token`); a new connection does not end it, the registered operation does.
  - Countdown summaries (§4.4).
- Long prompts, messages, patches and tool inputs/outputs show 20 000 characters at a time ("Show more") with
  "Download the full text" (§12.4); a tool row renders its details only while expanded; the Markdown of a message is
  built only once its row comes within one screen of the viewport (before that: its first line).
- Long chats (HOT-05, ADR-086): the timeline is a windowed list (§12.4) in follow mode at the end: 100 rows at first,
  "Show earlier items" adds 50, at most 150 rows are rendered. While following, the window keeps its first row as new
  items arrive and drops the oldest rows beyond 150; once a move drops rows at the newest end, the window is anchored
  (new items stay hidden behind "N newer entries – show"). "Jump to the latest item" returns to 100 rows at the end
  and scrolls to and focuses the last row. Rows are memoized on their content, so a new event renders only the new
  row; rows off screen skip layout and paint (`content-visibility: auto`; rendered rows stay in the accessibility tree
  and in the browser's find-in-page, which does not reach rows outside the window, closed details or text beyond the
  shown characters). The chat reads the newest page of its events when it opens; when the window reaches the oldest
  loaded item and the hub has older events, "Load more" reads the next older page (never all pages). The session
  detail's chat (§5.5) is the same list. Kinds
  with a verified body but without a renderer are skipped and do not break a run of file changes; an event kind the
  client's kind matrix does not know yet stays the neutral row "Unverifiable item (kind_not_allowed)". The
  reader writes settled events to the store at most once per frame (16 ms), so a burst of thousands of events costs
  a few store updates.
- Input: only here, only for `task:write`. Send = `prompt` event via `POST /v1/tasks/{task}/prompts`. While the main
  session is `working`, input stays enabled (prompt is queued by the session) and a **Stop** button (`interrupt`)
  plus a menu **End session** (`stop`) are shown (`POST /v1/sessions/{id}/control`). "Close task"
  (`POST /v1/tasks/{task}/close`) in the task menu.
- Read-only users see no composer and no control buttons; a notice "You have read access" is shown.
- **Queue (KOR-46, ADR-083 §3)**, above the composer for every reader: `GET /v1/tasks/{task}/queue`, reloaded when
  the task's prompts, sessions or the task change. "N instructions waiting" (none: nothing shown), the waiting prompts
  in event order with the sender's name ("you", "<name> (through their agent)") and their state: waiting, "waiting
  for the device" (the session's device is offline) or "being withdrawn". Writers see **Withdraw** on their own
  waiting prompts (`POST /v1/tasks/{task}/prompts/{event}/withdraw`; `peer_outdated` reads "the device has to be
  updated first"). The prompt bubble shows the outcome of a withdrawal: "Withdrawn", "Being withdrawn" or "Could
  not be withdrawn: it had already reached the AI".
- **Triggered by**: operation cards and the operation drawer, and session rows of the Sessions tab, name the person
  whose instruction they follow from (`triggered_by`), when the hub reports one.
- Usage strip (per task): tokens, cost with basis label ("provider", "list price", "subscription equivalent"),
  context-window percentage of the main session.

### 5.3 Sessions tab

Tree of sessions (main session, subagent children by `parent_session_id`, any depth, ADR-088) with state, device, model,
usage; each node links to `/o/:org/s/:session`. The tree is flattened depth first (the depth indents a row) into a
windowed list `task.sessions` (100 rows at first, 50 per move, at most 200 rendered); one selector delivers the task's
sessions and their usage, rows are memoized. The reachability of the sessions' devices is read once for the list
(`useDevicePresence`: pages of the device list until every shown device is found, the list ends or the hub's cap of
10 000 is read), not per row.

### 5.4 Operations tab

`GET /v1/tasks/{task}/operations`, newest first; detail drawer shows transitions, decrypted params and result, with
the same readable views of issue and pull request lists and of a read issue as the chat card (§5.2).
The rows are a windowed list `task.operations` (100 rows at first, 50 per move, at most 200 rendered); older pages
(50 each) are loaded by "Load more" when the window reaches the loaded end. One selector builds the views of the task's
operations (memoized per row and per operation's events). The detail's params and result are pretty-printed JSON,
clipped to 20 000 characters with "Show more" and "Download" (`operation-params.json`, `operation-result.json`), built
only while the detail is open.

### 5.4a Test runs and logs (KOR-43, ADR-080)

A `test.run` event is a chat card: kind, command, state (running, passed, failed, error, timed out, cancelled), exit
code, duration and start time of its VM, the summary ("Tests: 3 failed, 12 passed") and the tail (collapsed); the
operation card of `test.run` shows the same summary and "Open log". `workvm.run` items whose job kept a full log offer
"Open full log". The work VM panel of a session lists its runs (latest per run, newest first). The log viewer reads
the log's manifest and its chunks on demand (`GET /v1/tasks/{task}/logs/{object}` and `.../chunks/{index}`, 256 KiB of
output each), checks that the manifest's `ref` and object are those of the event that named it, opens each chunk
with the workgroup key of the manifest's epoch (`src/core/crypto/runlog.ts`, `04-crypto.md` §4.12), shows a window of
lines (never the whole log in the page), jumps to the end and downloads the whole log as a file chunk by chunk. A
truncated log says how much was kept. A work VM that starts implicitly (`workvm.state` `starting`) counts down like an
open `workvm.request`; a stopped one (paused session) reads "stopped – starts again on the next use".
A run whose own last event still says running while its operation is `failed` with `device_failed` is shown
cancelled with "Reason: the device failed" / "Grund: Gerät ausgefallen" (the hub cannot write the encrypted run
event; the operation state wins, ADR-087 §5).

### 5.4b Platform browser (KOR-44, ADR-085)

A `browser.state` event is a chat card: state (starting with the countdown to the 10 s target and "delayed" with the
real step after it, open, restarted, closed with its reason), the current URL, the number of actions, the start time
of its VM and **"Watch live"**, which opens the session's **Browser** tab. The operation cards of `browser.open` and
`browser.close` show the browser ID and the result. The Browser tab of the session detail (§5.5) shows:
- the **live picture**: while the tab is open the stream watches the session (`browser=<session>`, `browser_kbps`);
  each `browser_live` frame is opened with the workgroup key of its epoch (`src/core/crypto/browserframe.ts`,
  `04-crypto.md` §4.13); frames of another browser session than the newest open one, older ones and foreign ones are
  dropped, a frame older than 30 s shows its age, the picture is scaled to the width (phones included)
  and labelled "Live" with its age; without a live frame the newest recorded picture is shown, labelled with its time;
  watch only – no input of any kind reaches the page;
- the **bandwidth choice** "Economical" (250 kbit/s), "Standard" (1000, default) and "Sharp" (3000), kept in the
  browser (local storage) and sent as `browser_kbps`;
- the **action record**: the session's `browser.action` events, newest first, loaded page by page
  (`GET /v1/scopes/{wg}/events?session=…&kinds=browser.action`) and shown in a window (never the whole record in the
  page): time, tool and action, target, typed text (masked for passwords), URL, title, result or error, notice, and the
  picture after the action (decoded only for rows in view); live entries arrive as `browser_action` frames (not
  replayed on reconnect: the tab pulls the newest page again);
- the note that the free browser of the work VM (`workvm_browse` and scripts) is neither recorded nor live (E-09).


- Tabs: **Terminal**, **Chat** (outputs of this session only), **Children**, **Browser** (§5.4b, KOR-44).
- Terminal: `@xterm/xterm` with `disableStdin: true`, cursor hidden, no `onData`/`onKey`/`attachCustomKeyEventHandler`
  handlers, no clipboard paste; selection + copy allowed. Size from the asciicast header (`cols`/`rows`, 160 × 48 by
  default); `addon-fit` scales the font to the container width; on narrow screens the terminal scrolls horizontally
  instead of reflowing (no resize is ever sent).
- Data: on open the newest page of `GET /v1/scopes/{wg}/events?session=…&kinds=session.terminal&before=…` (newest
  first, old-hub fallback, §2.3), live via SSE `terminal=<session>` (max 3 watched sessions); the view registers the
  session as shown. Chunks are applied strictly in `scope_seq` order; a gap triggers an ascending pull of at most 3
  pages before further rendering – a longer gap (or a `resync`) reads the newest page again. Chunk lists grow by push
  (no copy per chunk); Follow keeps the newest 8 MiB of output, older output stays on the hub and is read again on
  demand, never lost.
- Modes: **Follow** (default, live tail; xterm scrollback 10 000 lines) and **History** (a window over the loaded
  output, at most 16 MiB: "Load earlier output" reads the previous page newest first; a time slider replays up to the
  chosen offset – at most 2 MiB from a chunk boundary – into an off-screen terminal, then shows it; playback
  1×/4×/16×).
- An explicit label "Read-only view – prompts are entered in the chat" is always visible.
- Main orchestrator on DFX hardware (KOR-57, `11-agent-access.md` §11, ADR-082 §6): for the task owner of a running
  (`working`, `idle`) main session the header asks `GET /v1/sessions/{id}/orchestrator` and offers "Make this session
  my main orchestrator" / "Diese Session als Hauptorchestrator" only when the hub says `available` (the owner's private
  organization, a platform device); a session that is the orchestrator already says so. The dialog (loaded when it
  opens) asks for the session's keys, checks `device_sig` and the fingerprint under the device key of the verified
  platform log (`pairing_mismatch` otherwise), names the session, its device, "Replaces “X”" and the full access, runs
  the pairing-tagged passkey ceremony and the confirmation of §8.3, sends the signed handover and waits for the
  session's claim (connected / failed).

### 5.6 Pull requests and "Fix conflicts" (KOR-48, ADR-084)

- **Links.** A pull request belongs to a task through a link: the device registers one after
  `github.create_pull_request`, a person links one by its address. `GET /v1/workgroups/{wg}/pr-links` (`?task=` for
  one task; newest first, 100 per page, "Load more") lists `{epoch, tag, sealed, task_id, session_id, linked_by}`;
  the browser opens `sealed` with the workgroup key of the row's epoch, bound to the row's workgroup, epoch, tag,
  task and origin (`device:<session_id>` / `user:<linked_by>`), and recomputes the tag (04-crypto.md §4.11a). A
  row without a body (older runner), without the epoch's key or whose body does not open is shown as "Pull request
  (details unavailable)" with its task (`core/pulls/links.ts`).
- **State.** Repository, number and state are read by a device: device request `github.pull_status` to the task's
  device, else an (online) device of the workgroup, with every pull request of the view batched into one request
  (≤ 50, one request at a time; the task is named when the batch has one, `core/pulls/status.ts`). Rows show open /
  draft / merged / closed and, for an open one, "Conflict" (`mergeable: false` alone), "Being checked"
  (`mergeable: null` – never "No conflict"; read once more after 10 s by itself, then "Check again") or "No
  conflict"; the link to GitHub (https only) and the task. `github_not_connected` offers "Connect GitHub" in the row
  (§8.1, E-05); other failures show their reason and "Check again". A pull request whose head is in another
  repository (a fork) says that "Fix conflicts" is not available.
- **Fix conflicts** (users with `task:write`, only for a conflict in the same repository) posts the fixed platform
  instruction v1 (`core/pulls/instruction.ts`, equal to `agent_platform/pulls.py`) as an ordinary prompt to the
  linked task (`POST /v1/tasks/{task}/prompts`), so the task's own conversation resumes (ADR-072).
- **Feedback.** "Conflicts fixed, branch updated (merge commit …)" once the task's verified operation events hold a
  succeeded `github.push` to the pull request's head branch of a commit made by `github.commit` with `merge: true`
  (`core/pulls/fixed.ts`); the state is then read again until it shows the merge commit as head.
- **Where.** The workgroup view `/o/:org/pulls` (default workgroup) and `/o/:org/w/:wg/pulls` with a workgroup bar,
  navigation entry "Pull requests" (phones: under "More"); the task tab "Pull requests"; in the situation view
  (§4.0) an entry whose task has links shows a compact line per pull request with "Fix conflicts" (states read only
  for the entries on screen). "Paste a pull-request address" (`https://github.com/<owner>/<repo>/pull/<n>` or
  `<owner>/<repo>#<n>`, writers) looks the pull request up under every epoch the browser holds first: a linked one
  is shown with its task and nothing changes; else `POST /v1/workgroups/{wg}/pr-links` under the current epoch. A
  person's link can be removed ("Unlink"); a device's link stays.
- **Volume** (ADR-086 §1): the rows of the view and of the tab are windowed lists (lists `pulls` and `task.pulls`,
  50 rows added per move, at most 200 rendered; only rendered rows read their state) over the hub's cursor pages
  ("Load more"). A situation-view entry names at most 3 links of its task and counts the rest with a link to the
  task's tab; the task choice of "Paste a pull-request address" offers the first page of the workgroup's tasks (50).
- Test IDs: `pulls.screen`, `pulls.level`, `pulls.item` (`data-pull`, `data-state`, `data-conflict`), `pulls.state`,
  `pulls.conflict`, `pulls.github`, `pulls.task`, `pulls.fix`, `pulls.fix.sent`, `pulls.recheck`, `pulls.fixed`,
  `pulls.fork`, `pulls.connect`, `pulls.unlink`, `pulls.unavailable`, `pulls.more`, `pulls.link.input`,
  `pulls.link.task`, `pulls.link.submit`, `pulls.link.created`, `pulls.link.exists`, `pulls.link.invalid`,
  `pulls.list`, `task.pulls`, `attention.pull`, `attention.pulls.more`, `attention.fix-conflicts`.

## 6. Whiteboards and notifications

### 6.1 Whiteboards

- Locations: org level (`/o/:org/whiteboard`), workgroup level (`/o/:org/w/:wg/whiteboard`), task level (task tab).
- List: entries newest first (`GET /v1/whiteboards`), decrypted latest text (Markdown), author, updated time,
  "edited" marker.
- Create/edit/delete only with the right of `04-crypto.md` §4.3 (org level: admin; wg/task level: wg write). Create
  form has an optional checkbox **"Notify participants"** (sends `notify:true`, `03-api.md` §4.9).
- History drawer: the versions (`GET /v1/whiteboards/{entry}/history`) newest first with author and time, diff to the
  previous version (line-based, client-side).
- Volume (ADR-086 §1): the board selects only the `whiteboard.*` events of its scope (chat traffic in a workgroup
  scope does not re-render it). Entries are a windowed list (`whiteboard.entries`, newest first, 40 rows at first,
  20 more per move, at most 60 rendered), rows memoized on their content, the Markdown parsed only near the viewport;
  the board reads its events (newest 10 pages) and the first page of rows (`limit=200`); older pages of rows only via
  "Load more" (`before=<next_before>`, ADR-086 §2), never all pages in a loop. An entry known from its events but
  missing from the rows read so far counts as unlisted (`event_conflict`) only when every page is read or the hub
  received its creating event strictly (ms) after the `created_at` of the oldest row read (the hub orders rows by
  `created_at DESC`); otherwise it is shown from its verified events. The history reads pages newest first
  (`limit=50`, then `before=<next_before>` via "Load more"); a hub that refuses `before`/`limit`
  (`400 request_invalid`) is asked once more without them (whole history, capped) and remembered per client. The
  history is a windowed list (`whiteboard.history`, newest first, same sizes); each version's diff is collapsed
  ("Show changes") and computed only when expanded; it shows only the changed hunks with 3 lines of context under
  `@@ -a,b +c,d @@` headers and is clipped at 20 000 characters ("Show more", "Download").

### 6.2 Notifications

- Bell with unread badge (from `GET /v1/me.unread_notifications`, updated by SSE `notification`).
- Center (`/notifications`): newest first, content-free texts built client-side from kind + decrypted target names
  ("New whiteboard entry in workgroup X", "Task Y is waiting for you", "Task Y failed", "Z accepted the invitation",
  and, for rows of the retired kind `rotation.required` that the hub no longer creates (ADR-058 §3), the neutral
  "Access in X is being updated"; `task.attention`: "Task Y needs your decision" / "Task Y is stuck" with the reason
  from `ref`, KOR-39; `device.needed` to admins: "Workgroup X in Y needs a computer – assign one", to the devices
  screen; `operator.request` to operators, account level: "Organization <id> has used up its credit and requests
  more" / "… waits for access to the platform's computers", ADR-079; `model.switched` to the task's owner: "Task Y:
  model switched: Claude subscription → DFX access over OpenRouter because Claude subscription is used up until
  14:05" from `ref` = `<from>:<to>:<until or empty>` (access values `subscription`, `operator` – "operator
  subscription" –, `api_key`, `platform`; a malformed ref: "Task Y: model switched"), to the task, ADR-081 §5); click
  marks read and navigates
  (`task.attention`: to the situation view entry); "Mark all read".
  The rows are a windowed list (ADR-086 §1): 100 rows at first, 50 more per "show older" or loaded page, at most 200
  rendered; older pages come from the hub (`before` cursor) only through "Load more", offered when the window reaches the
  loaded end. The names of all rows come from one selector of the list.
- Toasts for live notifications (auto-dismiss 6 s, accessible live region).
- `prompt.waiting_for_device` (KOR-46): "Your instruction in task Y waits for the device of its session", opening the
  task.

## 7. Organization management

### 7.1 Members and rights (`/o/:org/members`)

- List: members (name: the member's own `member.profile`, else the provisional name an admin gave – marked with who
  gave it –, else "Name not known yet" with what happens next and, for admins, "Enter name"; the ID only behind "Show
  ID", never as the main line; ADR-073), role, workgroups with access; devices are on §7.3. Admins enter or change the
  provisional name of a member without an own name (`member.name`). Admitting an existing account and inviting a user
  ask for the person's name; the invitations list says for whom each invitation is ("Invitation for <name>"), and an
  invitation whose account was meanwhile admitted directly is shown as settled with "Close invitation" (the admission
  closes it by itself). Opening an org whose log has no own `member.profile` of the user writes the user's own name
  from another org, without a dialog. An access a
  member has only because it is an admin (ADR-058) is shown as "reads as admin" and has no ungrant control.
- Volume (ADR-086 §1): the members, the open invitations and every other list of §7.1–§7.4 are windowed lists
  (§12.4: 100 rows at first, "show" adds 50, at most 200 rendered) in the order stated here (members: admins first,
  then by name). The rows are built once per change of their inputs (log, names, the hub's list) and re-render only
  when their content changed. A row's workgroup chips show the first 8 and "N more – show". The members come
  complete with `GET /v1/orgs/{org}` (ADR-086 §2) and are not paged.
- Hub paging (ADR-086 §2, `core/org/pages.ts`): the open invitations (§7.1, §7.3), the devices (§7.3), the platform
  candidates (§7.3) and the platform invitations (§7.4) read the hub's list page by page (`before`/`limit`; 100 rows,
  devices 200) – the first page, the next one only on "Load more" (also offered while a page holds no row the
  verified log keeps open). A re-read (log change, creation, revocation, the device presence poll) reads the loaded
  range again: the same number of pages from the newest one. An old hub that refuses the parameters
  (`400 request_invalid`) is asked once more without them and remembered (per loaded configuration); its whole
  answer is one page, and a note says when its cap cut the list (`truncated`).
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

List (windowed, §7.1; default workgroup first, archived last) with names (decrypted `wg.profile`), my access,
devices, archived flag. Admin: create (new `WK_1`, one batch with `wg_create` (the creating admin gets write), `grant` entries for chosen
members, self-wrap + wraps for those members, every other admin and chosen devices (`device_assign`), `wg.profile`
event); the note says "Every admin of the organization can read it. You can write." (ADR-058), rename
(`wg.profile`), archive (not the default workgroup).

### 7.3 Devices (`/o/:org/devices`)

- List (windowed, §7.1; newest device first): label (decrypted `device.profile`), kind (platform/org), online state,
  last seen, agent version, assigned workgroups (chips bounded as in §7.1) or **"Whole organization"**, capacity bar,
  **capacity-low badge** when `capacity_low` (`03-api.md` §4.6.2), and the details CPUs, memory, free disk, running
  VMs, active sessions, the limits "sessions per VM" and "VMs" and, per VM, its workgroup and sessions ("vm-… ·
  Research · 3 of 16 sessions") (ADR-075). The rows come from the verified log, the presence from the hub's list (pages of
  200, newest enrolment first, §7.1): while older pages exist the list ends with the last device the loaded pages
  name, and "Load more" reads the next page; once the hub's list is complete every device of the log is shown (one
  the hub did not send with "status unknown"). The presence poll (every 15 s while visible) and a log change read
  the loaded pages again (one request per page) and re-render only rows whose presence changed.
- **Add org device** (admin): choose workgroups → code + instructions block:
  `agent-runner enroll --hub <origin> --code <code>` (copy button), expiry 24 h default; the device appears live when
  it accepts.
  Invitations carry the keys of the chosen workgroups, so only workgroups whose current key the admin holds are
  offered ("You can add a device as soon as a workgroup is unlocked for you").
- **Assign platform device** / **assign to a further workgroup** (admin; any writer in a private org): every
  workgroup that is not archived; candidate list (`GET /v1/workgroups/{wg}/device-candidates`, pages of 100 in the
  hub's order with "Load more", §7.1), platform-log verification, batch. The batch wraps the keys the admin holds; the transition rule of ADR-058 §2 (`compatSkip`)
  withholds a workgroup key the admin reads only by inheritance and did not create, which the key holders deliver by
  catch-up.
- **Assign to the whole organization** (admin; ADR-075 §5): one batch `device_assign_org` + wraps (`OK`, the `WK`s
  the admin holds, own `CK`); the dialog states plainly: "The device will run tasks of every workgroup, also of
  workgroups created later. To run them it receives the keys of every workgroup: whoever controls the device can read
  their tasks, chats and whiteboards and the access data of their members. To keep a workgroup off the device, assign
  it to single workgroups instead." **Remove from the organization** (`device_unassign_org` + rotation batch).
- **Default device of a workgroup** (`PUT /v1/workgroups/{wg}/placement`, writers): "Automatic" or a device and
  optionally a VM; shown in the workgroup list.
- **Unassign** from a workgroup / **Unpair** (remove from org): confirmation ("the device loses access to new
  content; content it already read stays on the device") + automatic rotation batch (§7.1).
- Rename (admin, `device.profile`).

### 7.4 Platform invitations (`/platform/invites`, `platform:inviter`)

Shown to users with the role `inviter` and, while the root-signed switch `open_platform_invites` is on, to every user
(both read from the verified platform log, ADR-079); a user without the role sees only its own invitations. Create (code generated client-side, `POST /v1/platform/invites` with `invite_id` and expiry), show link/code once,
list open/used/revoked/expired (windowed, newest first, paged with "Load more", §7.1), revoke.

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
  the GitHub login, or the error. Hint to allow the organisations or repositories the agents need. "Connect GitHub"
  is offered only while an eligible device announces a sign-in (`features` of `GET /v1/me/devices`: `github.device_flow`
  or `github.operator`, or no GitHub ways at all from an older runner; KOR-65), and the flow lists only such devices;
  otherwise the section says the sign-in is not set up and offers the token alone.
- **Store an existing token (KOR-25):** paste field for an existing GitHub token (GitHub CLI login, personal access
  token); it leaves the browser only HPKE-encrypted to the device in the device request `github.connect` `{token}`;
  the device checks and stores it (`05-runner.md` §11.4). Afterwards "Connected as `<login>`" with the origin (device
  flow / token) and the scopes; fine-grained tokens are recommended, not enforced. A link opens GitHub's form for a
  classic token pre-filled with `repo,workflow` (KOR-65).
- **Composer hint:** a prompt with an obvious GitHub reference (`owner/name`, a GitHub URL, "issue", "pull request")
  while GitHub is not connected offers Connect next to the composer; the task is sent anyway.
- **Disconnect:** `DELETE /v1/me/credentials/github` + link to the GitHub settings page to revoke the app
  authorization.
- Workgroup settings (`/o/:org/w/:wg/settings`, `wg:write`): optional repository allow-list (`owner/name`, validated
  with the regex of `03-api.md` §9; empty = every repository of the GitHub connection, ADR-054), protected branch patterns (defaults prefilled), PR draft default; saved as
  `settings.workgroup` event (latest wins; concurrent edit warning when a newer event arrived meanwhile).

**Several accounts, company accounts, defaults (KOR-47, ADR-078).** Below the primary connection the section lists the
user's further GitHub accounts (`GET /v1/me/github-identities`, every own record opened and checked like the primary
one; login, source, scopes, signing key yes/no – never the token): "make primary", "reconnect" (`github.connect` with
`identity_id`), "disconnect", "add another account" (device flow or existing token) and the option "also create a
signing key" (`signing_key:true`, outcome shown). The screen **GitHub** of an organization (`/o/:org/github`) lists
its company accounts by metadata only – no browser opens them (key model KM-35): "Company account of <org> ·
connected <date>". Admins connect one (`github.connect` with `org_id` to a device of the organization; the browser
seals the answered body under a fresh epoch of the organization's credential key for its devices and stores it with
`PUT /v1/github-identities/{id}`), reconnect, remove it and set or clear the organization's default. The workgroup
settings offer the workgroup's default (company accounts of its organization; admins and writers). The composer offers
"GitHub account" (Automatic, own accounts, company accounts of the task's organization; `github_identity` of
`POST /v1/tasks`); the task header shows the task's account and lets a writer change or clear it. The operations tab
shows "as <login>, triggered by <member>" (or "a company account of <org>", "another account"). `409 peer_outdated`
reads "some devices of this organization must be updated first". A job re-seals own non-primary records under the
newest `CK` after a rotation.

### 8.2 Model access

- Preference (`auto`, `subscription`, `api_key`, `platform`) and model choice (`PUT /v1/me/settings`). Allowed model
  IDs per access kind come only from `GET /v1/config` (`models`); there is no build-time list. `platform` explains it is available only on DFX devices.
- **Fallback** (KOR-50, [ADR-081](../adr/081-model-access-fallback-and-dfx-access-on-own-hardware.md) §1), saved with the same button: "Use DFX access when my own subscription is used
  up" (`fallback.own_to_dfx`, default off; charged) and "Use DFX access over OpenRouter when the operator's
  subscriptions are used up" (`fallback.operator_to_platform`, default on). Not shown for a hub whose settings carry
  no `fallback` (an older release).
- **Model access and quotas** (organization, section `model-quotas`, ADR-081 §6): `GET /v1/orgs/{org}/quotas` of the
  active organization – the summary ("Subscriptions: n available · m used up – next release 14:05"), the user's own
  subscription (its windows, or "Not readable" honestly, with the last observed limit time), the operator's
  subscriptions when the organization may use them (label, state, used share or "quota not readable", release or
  "used up until", last observed limit) and whether DFX access over OpenRouter is available.
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
- An agent that runs in a session on DFX hardware (`session_id`) is shown "On DFX hardware, in a session of one of your
  tasks"; it is revoked with the reason `session_ended` when the session ends (`11-agent-access.md` §11).
- The audit view (§9.2) marks rows with `agent_id` "by agent <name>" ("by agent" when the name is not the user's).
- "Security activity" (`GET /v1/me/audit`) is a windowed list like §9.2 (100 rows at first, 50 per move or page, at
  most 200 rendered, "Load more" with the hub's cursor).

## 9. Monitoring and audit

### 9.1 Monitoring (`/o/:org/monitoring`)

- Overview (`GET /v1/monitoring/overview?org=`, live via SSE with `monitoring=1`): devices (CPU, RAM, disk, network,
  uptime, reachability, state, free capacity), drill-down to VMs of own workgroups, then sessions (CPU, memory,
  tasks, workspace size, state, usage: tokens, cost, context %), below a session its work VMs (KOR-29: CPU, memory,
  disk, network, runtime, charts).
- Volume (ADR-086 §1): the tree is flattened into rows (device, its VMs and their sessions while opened, each
  session's work VM rows right after it, then the entries not linked to a listed device or VM) and shown as the windowed list `monitoring.tree`: 100 rows at first,
  50 per move, at most 200 rendered; rows are memoized on their content. The ages ("Updated 5 s ago") count in their
  own cells on one shared one-second clock; the tree re-renders only when its data changes or an entity turns stale.
  Task titles come from one selector for the whole list. When the hub cut the overview (at most 2 000 sessions,
  `truncated`, ADR-086 §2) the screen says "Showing the 2,000 most recently active of N sessions"; when it cut the
  devices at 10 000 (`devices_truncated`) it says "Showing the first N devices; the hub has more".
- Charts (uPlot) via `GET /v1/monitoring/series`: ranges 1 h, 6 h, 24 h (raw), 7 d, 30 d, 90 d (rollups); legend and
  tooltips; values with units; accessible data table toggle. The data table shows at most the bucket count of the
  longest range (360 rows, newest first) and says how many points it shows when a series has more.

### 9.2 Audit

- `/o/:org/audit`: filters action, actor, workgroup, task, session, time; newest first; rows show actor display
  name, action, result, target names (decrypted where available), link to the referenced event. Admin: "Verify chain"
  (`GET /v1/orgs/{org}/audit/verify`) with result.
- Task tab Audit: same view filtered by task.
- Volume (ADR-086 §1): the rows are a windowed list (lists `audit` and `task.audit`): 100 rows at first, 50 more per
  "show older" or loaded page, at most 200 rendered; "Load more" (hub cursor) appears when the window reaches the
  loaded end, or alone while a time filter hides every loaded row. Task titles are pulled once per distinct task of
  the loaded rows and read, like agent names, with one selector per list; rows are memoized and never subscribe to
  the store themselves.

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
operating system, the checkout directory and the time: the rules that assign modules to chunks look only at paths
relative to `web/`, never at absolute module ids (HOT-18), and `make platform-ci-linux` builds on the runner device and in the Linux VM below another
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

Run while signed in and unlocked; serialized per org; status visible in a small "Sync" indicator. The long-task monitor
(§12.9) is not one of these jobs: it runs from the start of the page, signed in or not.

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
load plus 10 % (HOT-16: 199.3 KiB measured, guard 219 KiB), raised to 220 KiB by KOR-57: every client verifies the
agent of a user as a sub-principal in the membership logs at sign-in (ADR-082), which needs about 1 KiB gzip in the first
load; the confirmation of a pairing, the revocation, the job `agents` and the community check of agent authors load
lazily; raised to 221 KiB by KOR-48: the error texts of six new operation codes, the navigation entry and the request
kind of the pull-request view (its other texts load with its code, area `pulls`; decisions/KOR-48.md); the orchestrator
dialog of KOR-57 stage b adds its texts in both languages (the catalogs stay in the first load, HOT-16 point 8) while its
code loads lazily. The guard runs in every package's gates (`npm run
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


The pull-request view (KOR-48) keeps its texts in the lazily added area `pulls` (`core/i18n/lazy/pulls.ts`); only its
navigation label and tab title are in the first load, as the area `pullnav`.
### 12.3 Accessibility

Semantic HTML, labels for all inputs, focus visible, keyboard operable dialogs with focus trap, `aria-live` for
toasts, countdown (announced every 5 s and on step change) and connection banner, contrast WCAG AA, `prefers-reduced-motion`
respected (countdown ring without animation).

### 12.4 Lists and time

Lists newest first (repository rule) with stable tie-breaker; chat and logs ascending.

Every list of the client is the shared windowed list (`ui/lists/WindowedList.tsx`, ADR-086 §1; a source test,
`tests/scripts/list-markup.test.ts`, allows other list markup only for fixed small lists named there): only a window
of at most `maxRows` rows is rendered (chat 150, tables 200, heavy rows fewer), in follow mode at the newest end or
anchored after a move away from it. The hidden rows at either end are buttons with their count ("4 812 more entries –
show", "Show 12 newer entries"), plus "Jump to the newest"; when the window reaches the loaded end and the hub has
more, "Load more" loads the next page (no automatic infinite scroll, never all pages in a loop) and the page is shown
when it arrives – its rows may land anywhere in the list (a chat ordered by time is paged by scope seq; rows of one
millisecond), so the window and its focus target are always taken from the rows actually shown, never from an index
of the page. Buttons are at least 44 × 44 px. After a move the focus goes to the first newly shown row (after a loaded
page that fit into the window: its first shown row; no such row: focus stays) and the reading position is kept by correcting the scroll offset; rows carry `aria-setsize`/`aria-posinset`; a polite live
region announces the shown range ("Showing entries 51–200 of 4 862") after a user action only, never on live inflow.
Every list has an id of the closed set `core/lists/ids.ts` (the volume gate measures each). Heavy row content
(Markdown, patches, diffs, JSON) is built when its row comes within one screen of the viewport and stays while the
row is rendered; before that the row shows the first line of the text. A large single text shows 20 000 characters at
a time ("Show more"); "Download the full text" saves it as a file made in the browser (`blob:` URL of a download
link; the CSP of §10.2 restricts fetches and embedded content, not downloads); JSON is pretty-printed before it is
clipped. Event times (chat items,
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

### 12.9 UI health (early warning, ADR-086 §5)

`core/monitoring/longtasks.ts` counts main-thread blocks over 200 ms (`PerformanceObserver('longtask')`; where the
browser lacks it, the lateness of a 1 s timer; hidden pages and gaps over 60 s are ignored) per view type of
`contracts/enums/ui-views.json` (`core/monitoring/views.ts` maps the route pattern and tab to it; anything else is
`other`) – never an ID, a path or content. Per view: `count`, `blocked_ms`, `max_bucket` (index into the contract's
`buckets_ms`). At most once an hour and only when something was counted (checked every minute and on page hide) it
sends `{version, views}` to `POST /v1/telemetry/ui` with a plain `fetch` (`credentials: 'omit'`, `keepalive`), without
a signature; `version` is `config.version`. A failed report keeps its counters (clamped to the contract's limits);
the time of the last report and unsent counters are kept best effort in `localStorage` on page hide. It starts once
after the configuration check (`ui/app/boot.tsx`).

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
