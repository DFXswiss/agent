# 09 Milestones

Status: binding order of work. Work packages (WP) are the unit a planner assigns to one agent; each WP lands as
one or more draft PRs against the integration branch `ad5f2026-agent-platform` of its repository (ADR-001).
Every WP ships its own tests at 100 % line and branch coverage of the code it adds (`07-testing.md`) and its
JSON Schemas/contract tests where it touches a contract. A WP may only change a contract in `contracts/`
through a dedicated contract WP of the current milestone (owner: the architect role), never as a side effect.

Test categories used below: **CON** contract tests (schemas, vectors), **U-*** unit, **I-*** integration
(real PostgreSQL, real Lima where stated), **E2E** browser → hub → runner → VM → Claude Code (scenario IDs in
`07-testing.md`), **PERF** timing, **ATK** sandbox attack suite, **LIVE** live tests with real providers.
Requirement IDs `R-x.y` are defined in `01-requirements.md`.

## Dependency overview

```
M0 Foundation & contracts
 ├─ contracts + crypto core ──────────────┐
 ├─ hub skeleton + DB ───────┐            │
 ├─ runner skeleton + store ─┤            │
 ├─ guest + sandbox + image ─┤            │
 ├─ web skeleton ────────────┤            │
 └─ doubles + E2E skeleton ──┘            │
                ▼                         │
M1 Vertical slice (invite → live view, T1 + T2, < 10 s) ◄┘
                ▼
M2 Expansion (parallel tracks)
 M2a identity & membership ─┐
 M2b operations, GitHub, subagents ── needs M2a (grants/rights) for rights tests, can start on M1 rights
 M2c model access ──────────┤ (independent of M2b)
 M2d whiteboards & pings ───┤ (needs M2a roles)
 M2e monitoring & audit ────┤ (independent)
 M2f smartphone & UX ───────┘ (needs screens of M2a–M2e; runs continuously)
                ▼
M3 Completion (full matrix, ATK, PERF, LIVE, docs, draft PRs)
                ▼
M4 Cutover plan (document only)
```

## M0 – Foundation and contracts

Goal: everything that later work packages build on exists, is tested and is frozen as contract.

| WP | Repo / component | Scope | Depends on | Parallel group |
|---|---|---|---|---|
| WP-M0-01 | both / – | Integration branches, `docs/platform/` with spec + ADRs + `PLATFORM-DESIGN.md`, package skeletons (`agent_hub`, `web`, `agent_runner`, `agent_guest`, `agent_doubles`), console scripts, coverage configurations (100 % gates), `platform-*.yml` workflows, `make platform-ci` local gate | – | A |
| WP-M0-02 | agent-core / CON | `contracts/` JSON Schemas for every message of `03-api.md` (API, WS, guest, MCP, ops, events, credentials, audit actions), schema test harness for Python and TS, copy + hash check into `agent` | 01 | A |
| WP-M0-03 | agent / RUN crypto | Python crypto core (`04-crypto.md` §1–4, §6): canonical JSON, HKDF/HPKE/AES-GCM, bundle, wraps, events, credential records, device requests, invitation package, log + platform log verification, dedupe tags; vector generator `contracts/vectors/` | 02 | B |
| WP-M0-04 | agent-core / WEB core | TypeScript crypto/log core mirroring WP-M0-03, byte-identical against vectors; Vitest 100 % | 03 (vectors) | B |
| WP-M0-05 | agent-core / HUB | Config (fail-closed), migrate CLI, roles, base migrations for all tables of `02-data-model.md` §2, append-only triggers, audit chain + checkpoints, signed-request gate (reusing `agent_core.signing`), error shape, CSP/security headers, static serving | 01 | C |
| WP-M0-06 | agent / RUN | Runner config, local PostgreSQL cluster (pattern `agent_cli.pg`), store migrations, identity files, outbox with gapless `origin_seq`, signed HTTP client with hub-response verification, WS client with backoff | 01 | C |
| WP-M0-07 | agent / GST + SBX | Golden image definition (`images/`), provisioning, managed settings, harness text, nftables, mounts, users; guest API + control processes skeleton (health, assign, journal, sessions create/stop), Landlock launcher, unit properties; image build command | 01 | D |
| WP-M0-08 | agent / RUN vm | Lima wrapper, golden build/version registry, warm pool, clone fallback, restart, idle stop, delete, control-socket forwarding + token, capacity computation | 07 | D |
| WP-M0-09 | agent / DBL | Model double adopted from Spike C to 100 %; GitHub double (device flow, REST, Git Data API, tarball); mail double (SMTP in, IMAP out); login-CLI double | 01 | E |
| WP-M0-10 | agent / SBX test | Attack suite ATK baseline from Spike B (scripted adversary via model double + OS probe) runnable against a golden VM | 07, 09 | E |
| WP-M0-11 | agent-core / E2E | Orchestrator skeleton (start hub, runner, doubles; topologies T1/T2; Playwright with virtual authenticator incl. PRF; coverage collection) | 05, 06, 09 | E |
| WP-M0-12 | agent-core / WEB | Vite/React/Tailwind skeleton, routing, i18n de/en, CSP-compatible build with SRI, Playwright + monocart merged coverage gate; verify that xterm (`@xterm/addon-webgl`, canvas fallback) and uPlot inject no runtime `<style>` under the CSP (ADR-019 amendment rule otherwise) | 04 | B |
| WP-M0-13 | both / – | Copy the spike sources needed as baseline into the repositories (model double, scripted attack list and adversary double, Landlock launcher prototype, sandbox probe, vector generator, PRF/virtual-authenticator fixtures) – hygiene-scanned (no host names, accounts, secrets) and attributed; the spikes themselves are not public | 01 | A |
| WP-M0-14 | agent / RUN + GST | Early measurements on the runner device: Claude TUI start under Landlock, clone path end to end (clone → boot → harden unit with Lima-param control token → guest health → assign → session → prompt accepted); LIVE-03 subscription gateway check (read-only current access token; does the usage endpoint accept setup tokens?) | 07, 08 | C |

Acceptance criteria:
- CON: all schemas present and validated against example fixtures; Python and TS implementations produce identical vector files (`test_vectors_file_is_current` green in both repos).
- U-/I-: hub migrations apply on PostgreSQL 16 and 17; append-only triggers refuse UPDATE/DELETE/TRUNCATE; runner outbox gapless under concurrent writers.
- Golden image builds reproducibly (< 2 min); pool assignment ≤ 0.5 s and clone + start ≤ 7 s measured on the runner device (PERF smoke).
- ATK baseline: every scripted attack of Spike B refused on the new guest (hooks on and hooks failing with HTTP 500).
- E2E skeleton: a Playwright test registers a passkey with PRF on a virtual authenticator against the new hub.
- WP-M0-14 results recorded: clone path end to end and TUI start under Landlock measured; if the clone path exceeds 9 s the M1 plan is revised before M1 starts; LIVE-03 outcome recorded in ADR-010.
- No runtime `<style>` injection under the CSP (or the ADR-019 amendment is recorded).
- Closed enumerations exist in `contracts/enums/` (error codes, operation error codes, session failure reasons, outbox item kinds, outbox audit actions, audit actions).

Exit gate: all coverage gates 100 % (Python per package; web merged), contract tests green, `make platform-ci` green in both repositories.

## M1 – Vertical slice: invitation to live view

Goal: the complete chain works end to end for the main path on both topologies.

| WP | Repo / component | Scope | Depends on |
|---|---|---|---|
| WP-M1-01 | HUB | Passkey register/login/session certificates (reuse `agent_core.passkey`, ES256 only), invitation binding (`registered_user`, 24 h orphan cleanup), user credential key `CK_1` at registration, recovery slot storage, `/v1/me`, platform invitations, platform entitlement (ADR-026), root-signed operator requests and CLI (`platform init`, `invite-user`, `invite-device`, `certify-pending`, `grant-org-access`) | M0 |
| WP-M1-02 | WEB | Invitation page, passkey + PRF registration, recovery code display/confirm, login/unlock, private org genesis | M1-01 |
| WP-M1-03 | HUB | Org genesis, membership-log append/verify (L1–L8), wraps, events append/read, scope heads, SSE stream, device WS hints | M0 |
| WP-M1-04 | RUN | Platform device enrollment + certification, reconcile, pull/push loop, log verification, wrap handling | M1-03 |
| WP-M1-05 | HUB | Tasks, sessions, transitions, scheduler, `POST /v1/tasks`, device candidates, auto-assignment support | M1-03 |
| WP-M1-06 | RUN + GST | Session start in pool VM: sandbox, Claude Code TUI, harness, hooks, prompt delivery + origin check, transcript → `session.output`, terminal recorder → `session.terminal`, platform model access via gateway (model double) | M0-07/08, M1-04 |
| WP-M1-07 | WEB | Workgroup task list, new task with countdown and status steps, chat view, read-only terminal view (xterm.js) | M1-02, M1-05 |
| WP-M1-08 | E2E | Scenarios: invitation → passkey → private org → first task → answer in chat → live terminal; follow-up prompt; reconnect after hub restart; on T1 and T2 | all above |

Acceptance criteria:
- E2E scenarios covering R-0.x, R-1.x (user invitation and passkey), R-2.x, R-3.x (VM, sandbox, countdown), R-4.8, R-6.x (chat, terminal live and history; R-6.8 offline/reconnect via E2E-14) and the session-side execution ban R-10.1–R-10.5, R-10.7, R-10.8, R-10.12 (E2E-23, E2E-34, E2E-35) pass on T1 and T2, three consecutive runs each.
- PERF: time-to-working (`first_working_at − task.created_at`) < 10 s in 20 of 20 runs with warm pool and in 10 of 10 runs with empty pool (clone fallback) on the runner device; countdown shows real status on an injected delay.
- Hub database contains no plaintext of prompts, answers, titles or terminal bytes (automated plaintext scan of the database dump in E2E).
- ATK suite green against the M1 golden image.

Exit gate: coverage 100 %, contract tests green, M1 E2E green on T1 and T2.

## M2 – Expansion (parallel tracks)

All tracks start after M1. Each track ends with its E2E scenarios green on T1 (and on T2 where the scenario
involves the device link).

### M2a – Identity and membership complete
WPs: org invitations (user/device) with package and expiry/revocation; roles; workgroup create/archive; grant/ungrant;
member removal and device unpairing with atomic rotation; catch-up wrapping; org device enrollment
(`agent-runner enroll`); platform device decertification + `rotation_pending`; recovery flow; add/remove passkey;
PRF-missing path with IndexedDB device key; existing user joining a further org (E2E-39); operator grant of platform
access to non-private orgs (ADR-026); members/rights and devices screens; rights enforcement in hub and UI.
Acceptance: E2E covering R-1.x (all, incl. proposals), R-2.x (org named in invitation, default workgroup),
R-3.x (device to org/workgroup), R-5.x and R-8.x (UI + backend enforcement, removed member refused, malicious-hub
tests of log and wraps) pass; CON vectors for every log entry type.

### M2b – Operations, GitHub, subagents
WPs: MCP platform API (all tools), operation pipeline (guest → runner → hub H1–H6 → execution → result), dedupe
tags and windows, retries, expiry; GitHub connect via device flow (device requests) and disconnect; executors for
every `github.*` type (REST/Git Data API only); workgroup settings (repository allow-list, protected branches);
`task.set_title`; `subagent.request` with limits, child sessions, `get_subagent_result`, subagent views in web;
operations list in chat.
Acceptance: E2E covering R-9.x, R-10.x (operation side), R-11.x and R-13.x pass against the GitHub double
(including duplicate comment refused, rejected and duplicate requests audited, PR created and ID/URL returned,
subagent depth/parallel limits enforced); ATK extended with operation side-channel attempts.

### M2c – Model access
WPs: credential records (CK scope, rotation), API key storage and use, subscription setup-token relay, mailbox
login automation (headless browser, IMAP via mail double), model selection per user preference, gateway
credential injection for all three kinds, model/provider allow-lists, cost basis per kind, context %, quota
polling (usage endpoint, OpenRouter key), budget limit per org/day.
Acceptance: E2E covering R-4.x and R-6.4/R-6.5 (usage, cost, context, quotas – E2E-16) pass hermetically (double for API key, subscription and OpenRouter shapes, login
doubles); no credential value appears in sandbox environment, files or logs (scan); LIVE OpenRouter/DeepSeek
smoke runs once green.

### M2d – Whiteboards, notifications, pings
WPs: whiteboard events and projection for org/workgroup/task level, history view, edit/delete, rights; notify flag,
notification centre, live delivery; content-free pings to sessions via hook `additionalContext` and
`get_notifications`; `subagent.finished` / `operation.finished` pings.
Acceptance: E2E covering R-12.x pass (entry with notification reaches all entitled users and not others; active
session receives only the content-free notice, is not interrupted, and can fetch the entry via MCP).

### M2e – Monitoring, audit, read log
WPs: runner metrics sampler (device, VM, session), textfile writer with atomic writes, outbox metrics, hub
ingestion/rollups/series API, monitoring screens; Grafana dashboards and alert rules + launchd plist in
`deploy/monitoring/` and prepared infrastructure patch; audit for every action, read log for every read endpoint
and MCP read, audit view and verify.
Acceptance: E2E covering R-6.x (audit fields) , R-7.x and R-8.x (read logging) pass; Prometheus in the local test
monitoring VM scrapes the textfile output and the dashboards load in Grafana of the pinned version; audit chain
verification detects a tampered row.

### M2f – Smartphone and UX completion
WPs: responsive layouts for all screens, mobile navigation, touch targets, offline/reconnect indicators,
accessibility checks, i18n completeness, security page with build hash.
Acceptance: R-6.6 (E2E-29, event times `HH:MM:SS.mmm`) green; all E2E web scenarios also pass in a mobile viewport project (Chromium device emulation); no
horizontal scrolling at 360 px width; event latency hub → browser p95 < 300 ms on LAN.

Exit gate for M2: coverage 100 %, contract tests green, all M2 E2E scenarios green on T1; device-link scenarios
green on T2.

## M3 – Completion

| WP | Scope |
|---|---|
| WP-M3-01 | Full E2E matrix on T1 and T2 (every scenario of `07-testing.md`, desktop and mobile viewport), three green consecutive runs |
| WP-M3-02 | ATK gate final: scripted + model-driven (red-team harness, model double and one LIVE DeepSeek run) + OS probes; documented results |
| WP-M3-03 | PERF final: time-to-working distribution, 30 parallel first tasks, reconnect latency, terminal latency |
| WP-M3-04 | LIVE tests: OpenRouter/DeepSeek full chain, GitHub test repository (branch, commit, push, PR, comment, cleanup), subscription access token read-only mini turn |
| WP-M3-05 | Disaster recovery: export/restore of an org, rebuild of projections, runner database wipe with identity retained |
| WP-M3-06 | PRF device matrix (manual): real platforms listed in ADR-006, results documented |
| WP-M3-07 | Documentation into repos (`docs/platform/`), operator runbooks (platform root, device certification, golden image release, Claude Code upgrade gate), `PLATFORM-DESIGN.md` final |
| WP-M3-08 | Draft PRs of both integration branches against `develop` with summary, Reused/Added lists, local CI reports; **no merge** |

Traceability rule for every milestone: the exit of M0–M3 requires that the traceability test lists the R-IDs
assigned to that milestone (by the acceptance criteria above) as green; M3 requires all of them.

Acceptance criteria: every requirement in `01-requirements.md` has at least one passing E2E test (traceability
report generated from test markers); all coverage gates 100 %; no open defect of severity high; secret and
host-name scan of both branches clean.

## M4 – Cutover plan (document only, not executed)

Deliverable: `docs/platform/CUTOVER.md` describing deployment roles (hub device runs hub + PostgreSQL in a Lima VM
following the infrastructure repository's stack pattern; runner device runs `agent-runner` as LaunchDaemon),
required infrastructure changes (DNS, tunnel ingress, access policy for invitation-based external users) which
require owner approval and the infrastructure repository's PR process, fixing the WebAuthn RP ID before the first
real passkey, retirement of the legacy hub, and rollback. No deployment, DNS or tunnel change is part of this work.

## Risks per milestone

| Milestone | Risk | Mitigation |
|---|---|---|
| M0 | Contract churn after M0 forces rework in many WPs | Contract WPs owned centrally; changes only via contract WP + version bump |
| M0 | Lima socket forwarding or guest permissions behave differently than assumed | Verified first in WP-M0-07/08; fallback: host-initiated `limactl shell` transport |
| M0 | Hook `additionalContext` / `--restricted` behaviour differs in the pinned Claude Code version | Verified in WP-M0-07 with the model double; ADR update if needed |
| M1 | Claude Code TUI start + prompt acceptance inside the sandbox exceeds the 10 s budget; clone path margin is thin (≈ 6 s measured for clone + boot alone) | Measured in WP-M0-14; self-test off the hot path; control token as Lima param; pool sized for expected bursts; argv initial prompt |
| M1 | Two-device topology network differences (LAN, TLS) | T2 from M1 on; hub reachable by plain HTTP only in test topology |
| M2a | PRF unavailable on target platforms | Recovery code mandatory; IndexedDB device key; matrix in M3 |
| M2a | Concurrent rotations by two admins | `seq_conflict` + automatic retry; dedicated test |
| M2b | GitHub API semantics differ between double and real GitHub | LIVE tests against the test repository; double shapes recorded from LIVE runs |
| M2c | Subscription token via gateway injection rejected by the provider | LIVE read-only access-token test early in M2c; fallback documented |
| M2c | Login page automation brittle | Optional feature with setup-token fallback; hermetic tests only |
| M2e | Prometheus cardinality from session labels | IDs only, 15-day retention; aggregate-only fallback switch |
| M3 | Flaky E2E on real VMs | Three-run stability requirement; deterministic doubles; timeouts derived from PERF data |
| M4 | Access policy of the production host blocks external users | Owner decision documented as prerequisite |
