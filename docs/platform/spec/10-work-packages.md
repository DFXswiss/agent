# 10 Work packages

Status: binding plan of work (2026-11-21). Splits `09-milestones.md` into work packages (WP) that the dispatcher
(`tools/dispatch.py`, board `board/`) hands to parallel agents. Every agent sees only `board/PREAMBLE.md` and its package
text `board/wp/<ID>.md`; this document is the shared map they are pointed to. Conventions: ADR-027.

## 1. Packages per milestone

| Milestone | Packages | Development | Scenario (SCN) | Acceptance |
|---|---|---|---|---|
| M0 Foundation and contracts | 29 | 27 | SCN-00 | ACC-M0 |
| M1 Vertical slice | 20 | 17 | SCN-01, SCN-02 | ACC-M1 |
| M2 Expansion (a–f) | 35 | 28 | SCN-03 … SCN-08 | ACC-M2 |
| M3 Completion | 7 | 5 (DR-01, ATS-03, PFM-01, LVT-01, DOC-01) | SCN-09 | ACC-M3 |
| M4 Cutover plan | 2 | DOC-02 | – | ACC-FINAL |
| **Total** | **93** | | | |

Package ID prefixes: FND foundation, CTR contracts, CRY Python crypto, WCR TypeScript crypto, HUB hub, RUN runner,
GST guest agent, SBX sandbox/image, VM runner VM management, DBL test doubles, ATS attack suite, PRB early probes,
WEB web client, SCN E2E scenarios, MON monitoring assets, DR disaster recovery, PFM performance, LVT live tests,
DOC documentation, ACC milestone acceptance. (Prefixes deliberately differ from the test IDs `E2E-*`, `ATK-*`,
`PERF-*`, `LIVE-*`, `CON-*` of `07-testing.md`.)

Longest dependency chain: 26 packages
`FND-00 → CRY-01 → CRY-02 → CRY-03 → RUN-02 → SCN-00 → ACC-M0 → HUB-05 → HUB-06 → HUB-07 → WEB-04 → SCN-01 → SCN-02 →
ACC-M1 → GST-07 → RUN-07 → RUN-09 → RUN-10 → SCN-04 → WEB-13 → SCN-08 → ACC-M2 → DR-01 → ACC-M3 → DOC-02 → ACC-FINAL`
(7 within M0, 7 within M1, 8 within M2). At most four packages run at a time; the index order of `board/index.json`
puts the critical path first.

## 2. Dependency graph

Every first package of a milestone depends on the previous acceptance package (milestone exit gate of
`09-milestones.md`); every acceptance package depends on all packages of its milestone (omitted below).

```
M0  FND-00 ─┬─ CTR-01 ─┬─ CTR-02 ─┬─ GST-03 (+GST-01)
            │          ├─ CTR-03 ─ RUN-02 (+RUN-01, CRY-03) ─────┐
            │          │          └─ GST-04 (+GST-01, DBL-01)
            │          ├─ HUB-02 (+HUB-01) ─────────────────┐
            │          ├─ WEB-01 ─┬─ WCR-01 (+CRY-01) ─ WCR-02 (+CRY-02)
            │          │          └──────────────────────────┤
            ├─ CRY-01 ─ CRY-02 ─ CRY-03 ────────────────────┤
            ├─ HUB-01                                        ├─ SCN-00 (+DBL-01)
            ├─ RUN-01 ─┐                                     │
            ├─ SBX-01 ─┼─ VM-01 (+GST-01) ─ VM-02 ─┬─ ATS-01 (+GST-02, GST-03, DBL-01)
            ├─ SBX-02 ─┼─ GST-02 (+GST-01)         └─ PRB-01 (+GST-02, GST-03, GST-04)
            │          └─ GST-01 (Linux test job of SBX-02)
            ├─ DBL-01 ─ DBL-02, DBL-03
M1  ACC-M0 ─┬─ HUB-03 ──────────────┐
            ├─ HUB-04 ─────────┐    ├─ WEB-03 (+WEB-02, HUB-05) ─ WEB-04 (+HUB-07, HUB-09) ─ WEB-05
            ├─ HUB-05 ─ HUB-06 ┼─ HUB-08 ─┐                          │
            │                  ├─ HUB-07   ├─ HUB-10                 ├─ SCN-01 (+HUB-08, HUB-10, RUN-05, GST-05)
            │                  └─ HUB-09 ──┘                         │        └─ SCN-02 (+WEB-05)
            ├─ RUN-03 ─ RUN-04 ─ RUN-05 (+GST-06) ───────────────────┘
            ├─ GST-05, GST-06, WEB-02
M2  ACC-M1 ─┬─ a: HUB-11 ─ WEB-08 ─ WEB-14 ;  WEB-06 ─ WEB-07 ;  RUN-06      → SCN-03
            ├─ b: GST-07 ─┬─ RUN-07 ─┬─ RUN-09 ─ RUN-10 ─ ATS-02            → SCN-04 (+HUB-12, HUB-13, WEB-09)
            │             │          └─ RUN-11
            │             ├─ RUN-08 ;  HUB-12, HUB-13 ─ WEB-09
            ├─ c: RUN-09 ─┬─ RUN-12 ;  HUB-12 ─ HUB-14 ─ WEB-10 (+HUB-13)  → SCN-05
            │             └─ RUN-13
            ├─ d: HUB-15 ─ WEB-11 ;  RUN-11 ─ RUN-14                         → SCN-06 (+RUN-08)
            ├─ e: RUN-15 ─ MON-01 ;  HUB-16 ─ WEB-12                         → SCN-07
            └─ f: WEB-13 (after WEB-07/09/10/11/12/14 and SCN-03…07)          → SCN-08
M3  ACC-M2 ─ DR-01, SCN-09, ATS-03, PFM-01, LVT-01, DOC-01 ─ ACC-M3
M4  ACC-M3 ─ DOC-02 ─ ACC-FINAL
```

## 3. Package table (scope and ownership)

"Owns" = paths the package creates or changes; other packages extend them only through the registration points of
ADR-027 §4 or after the owner has merged (strict dependency). Spec references point to the binding sections.

### M0 – Foundation and contracts

| ID | Repo | Scope (spec) | Owns |
|---|---|---|---|
| FND-00 | workspace | Integration branches in both repos; `docs/platform/` (spec 00–10 + ADRs, `PLATFORM-DESIGN.md`, ADR-001); package skeletons `agent_hub`, `agent_runner`, `agent_guest`, `agent_doubles` (importable, console scripts `agent-hub`, `agent-runner`, `agent-guest`); `pyproject.toml` extras + coverage configs (07 §3); ruff/mypy configs; `Makefile` targets `platform-ci`, `platform-ci-linux`, `contracts-version`, `sync-contracts`; `platform-*.yml` workflows (07 §8); hygiene + legacy-untouched tests; workspace tools `tools/wp/*` (start, env, testpg, gates, remote, e2e, merge, cleanup, golden-cache, pr) with tests; test environment on the work devices (workspace-internal host notes); ADR-027 copied | `tools/wp/`, both repos: `Makefile`, `pyproject.toml`, `.coveragerc-platform`, `.github/workflows/platform-*.yml`, `docs/platform/**`, `src/agent_*/__init__.py`, `tests/platform/conftest.py`, `tests/platform/test_hygiene.py`, `tests/platform/test_legacy_untouched.py`, `scripts/guest-ci.sh` (stub) |
| CTR-01 | agent-core | Conventions (`common.json`, keyword whitelist matching the GST-01 validator), JSON Schemas for 03 §4 browser/anonymous/root API, `api/INDEX.json`, `contracts/enums/*` (all closed lists), Python + TS validation harness (`contracts/tools/validate.mjs`), fixtures valid/invalid, agent-side `scripts/sync-contracts.sh` + hash test (CON-01, CON-04) | `contracts/{README.md,common.json,api/ (browser),enums/,fixtures/,package.json,tools/validate.mjs}`, `agent/scripts/sync-contracts.sh`, contract tests |
| CTR-03 | agent-core | Schemas for 03 §5, §6.1–§6.2, §10, 04 §4.3–§4.9 (device API, outbox items, WS hints, SSE frames, event headers/bodies, credential records, device requests, log/platform entries, invitation package) | `contracts/{api/device-*,outbox,ws,events,credentials,requests,log}/` |
| CTR-02 | agent-core | Schemas for 03 §7 (guest control + journal bodies), §8 (MCP tools in/out, hooks bodies, deny-reason fixtures), §9 (op params/results), model catalog (05 §10.5), `audit-actions.json` detail schemas | `contracts/{guest,mcp,ops}/`, `contracts/model-catalog.json`, `contracts/audit/` |
| CRY-01 | agent-core | `agent_hub/crypto` (incl. generator logic `vectors.py`): 04 §1–§4 (canonical JSON, b64u, UUIDv7, HKDF, HPKE, AES-GCM, bundle, wrap, event seal/open, credential record, device request/response, invitation code/package, enrollment text, recovery proof, dedupe tag), generator `contracts/tools/make_vectors.py`, vectors of 04 §10 except logs; `test_vectors_file_is_current`; from Spike D `e2ee.py`, `keyflow.py`, `make_vectors.py` | `src/agent_hub/crypto/`, `contracts/tools/make_vectors.py` (thin wrapper), `contracts/vectors/` |
| CRY-02 | agent-core | `agent_hub/log`: 04 §4.7 (entry types, rights derivation, rotation set R), §4.8, §6.1–§6.4 (L1–L10, P1–P4, W1–W3, E1–E9 rule tables, CON-03 matrix), log/platform-log vectors incl. one broken variant per rule | `src/agent_hub/log/`, log vectors |
| CRY-03 | agent | `agent_runner/crypto` = copy of `agent_hub.crypto` + `agent_hub.log` (origin docstrings), storage key, tests against synced vectors (CON-02) | `src/agent_runner/crypto/` |
| HUB-01 | agent-core | 02 §1, §2 (all tables), §6 hub: SQL migrations, roles owner/writer, `refuse_change` triggers, `agent-hub migrate`, pool, audit append with chain + advisory lock, checkpoints (record/checkpoint format), MIG tests on PG 16/17, U-HUB-schema (no project table, no plaintext columns) | `src/agent_hub/db/`, `src/agent_hub/audit/{chain,checkpoint}.py`, `tests/platform/hub/conftest.py` (DB fixture) |
| HUB-02 | agent-core | 03 §1–§2, §11: config (fail-closed), app factory with ADR-027 discovery, `agent_core.signing` gate (window, nonce store, key lookup interface), response signing, error shape, rate limits, CSP/security headers, static + SPA serving (06 §10.1), `/v1/health`, `/v1/config`, read-log writer + route declaration, job runner, test client fixtures (signed browser/device/root clients, fixed clock) | `src/agent_hub/{config,app,signals}.py`, `__main__.py` (replaces the FND-00 placeholder), `auth/gate.py`, `http/`, `jobs/`, `stream/publish.py` (interface), hub conftest fixtures |
| RUN-01 | agent | 05 §1.1–§1.4, 02 §4, §6 runner: config (fail-closed, `unsupported_os`, `AGENT_RUNNER_VM_PREFIX`), PG cluster (`agent_cli.pg`), store migrations (all tables), identity files, CLI skeleton with ADR-027 discovery, supervisor (restart limits, SIGTERM), `service install/uninstall`, `uninstall` | `src/agent_runner/{config,cli,identity,service,supervisor}.py`, `store/`, runner conftest |
| RUN-02 | agent | 05 §2.3, 03 §1, §6.1–§6.2: outbox (gapless, storage-key encrypted bodies, seal-once/reseal, push loop, `acked_through`, gap/conflict handling), signed HTTP client (`agent_cli.keys.request_text`, response verification, pinned hub key), WS client (signed upgrade, heartbeat, backoff), hint dispatch registry | `src/agent_runner/sync/` |
| GST-01 | agent | 05 §5.1, 03 §7 (health, assign, journal, ack, pending answer plumbing), 02 §5: `agent-guest-control` (root, unix sockets, bearer token, HTTP/1.1), `agent-guest-api` skeleton (127.0.0.1:7700 router), api.sock JSON-lines protocol, SQLite journal + prune, session table cache, stdlib JSON-Schema subset validator, config `AGENT_GUEST_*` | `src/agent_guest/{control,server,journal,schema,config,apisock}.py`, `control_routes/`, `api_routes/`, guest conftest |
| GST-02 | agent | 05 §4.3–§4.4, §5.1, §6.3 (stop), §8 (render), ADR-021: `POST /v1/sessions` (user, dirs, `.claude.json`, harness render, token hash, unit via `sandbox/unit.py` in tmux via launcher), control `stop` (other actions dispatched to GST-06), AgentDriver interface | `src/agent_guest/{sessions,harness,driver}.py`, `control_routes/sessions.py` |
| GST-03 | agent | 05 §5.2–§5.5, 03 §8.2: `/hooks/<Event>` for 18 events, PreToolUse policy (realpath, §4.6.2), UserPromptSubmit origin + `prompt_registry` + `POST …/prompts` registration, journaling (Stop handled by GST-06 addon), `FAULT_HOOKS` test switch, hook-addon registry | `src/agent_guest/{hooks,policy,prompts}.py`, `control_routes/prompts.py` (registration), hook-addon mechanism |
| GST-04 | agent | 05 §10.2–§10.4, 03 §7 `PUT …/credential` (in-memory gateway credentials), 03 §8 (transport, initialize, tools/list, get_context forwarded as `platform.query`): model gateway (session token, per-kind injection, allow-list, budget 429, usage from SSE/JSON, OpenRouter cost/provider, provider audit), MCP JSON-RPC server with tool registry (tools return `pending`/not-yet until GST-07) | `src/agent_guest/{gateway,mcp}.py`, `mcp_tools/get_context.py`, `api_routes/{model,mcp}.py` |
| SBX-01 | agent | 05 §3.1 (template/provision), §4.1–§4.2, §4.3 (nftables, mounts, users), §8 (RULES.md), §15 (`images/versions.json`): `images/golden.yaml`, `images/provision/*.sh`, `images/files/`, `agent_guest/sandbox/settings.py` (managed settings generator), mcp.json, units incl. `agent-guest-harden.service`; from Spike A `golden.yaml`/`slim.yaml`, Spike B `gen_managed_settings.py`/`setup-vm.sh` | `images/`, `src/agent_guest/sandbox/settings.py` |
| SBX-02 | agent | 05 §4.3 (launcher, unit properties), §4.5, §4.6: `sandbox/landlock.py` (from `landlock_exec.py`), `sandbox/unit.py`, `sandbox/selftest.py` (from `sandbox_probe.py`), `scripts/guest-ci.sh` + `make platform-ci-linux` (Linux coverage combine) | `src/agent_guest/sandbox/{landlock,unit,selftest}.py`, `tests/platform/guest_linux/`, `scripts/guest-ci.sh` |
| VM-01 | agent | 05 §3.1 (build steps, exec_libs, build.json, smoke, rename, registry), §1.2 `image build/list/retire`, Lima ≥ 2.3 wrapper (injected `limactl`), prefix | `src/agent_runner/vm/{lima,golden}.py`, `vm/cli.py` |
| VM-02 | agent | 05 §3.2–§3.5, 03 §7 transport: pool fill/refill, clone fallback with retry, control token param + hash, guest health poll, assign, restart, idle stop, delete, golden replacement, startup reconciliation, capacity §3.4, `pool status`, guestlink client (HTTP over forwarded unix socket) | `src/agent_runner/vm/{pool,lifecycle,capacity}.py`, `src/agent_runner/guestlink/` |
| DBL-01 | agent | 07 §9 model double: port Spike C `modeldouble/` to `agent_doubles/model` (Messages API + SSE, count_tokens, hello, scenarios expect/reply/error, OpenRouter extras, subscription beta check, token per credential kind, journal, `/_double/state`, unknown routes fail), adversary scenarios from Spike B `adversary_double.py`/`attacks-scripted.json`; CLI `python -m agent_doubles.model` | `src/agent_doubles/{model,common}/` incl. `scenarios/atk-*.json`, doubles conftest |
| DBL-02 | agent | 07 §9 GitHub double (device flow states, REST subset of 03 §9/05 §12.1, Git Data API with real git object SHAs, tarball, faults, journal, seeding API) | `src/agent_doubles/github/` |
| DBL-03 | agent | 07 §9 mail double (SMTP in, IMAP/IMAPS INBOX + `\Junk`, test CA), login-CLI double (`claude setup-token` mimic), auth-page double | `src/agent_doubles/{mail,login_cli}/` |
| ATS-01 | agent | 07 §2.3 ATK-01..ATK-32, 05 §4: pytest suite `tests/platform/atk/` driving real Claude Code in a VM cloned from the golden against adversary scenarios `atk-NN.json`, two modes (hooks on / hooks 500 + poisoned settings), OS probe; report | `tests/platform/atk/` (scenario files `atk-*.json` belong to DBL-01) |
| PRB-01 | agent | 09 WP-M0-14: measured clone path end to end (clone → boot → harden → health → assign → session → prompt accepted), Claude TUI start under Landlock, LIVE-03 read-only subscription token through the gateway + usage endpoint check; results into ADR-009/ADR-010 notes; M1 go/no-go (> 9 s ⇒ plan revision) | `tools/probes/` (agent repo `scripts/probe_*.py`), report |
| WEB-01 | agent-core | 06 §2.1–§2.5, §10.1–§10.4, §13, ADR-019: Vite/React 18/Tailwind/TS strict project, ESLint/Prettier, all runtime deps, router + glob registration (ADR-027 §4), i18n loader + parity test + error catalog from contracts, SRI plugin, reproducible build check, `__TEST__`/globals check, Vitest + Playwright + monocart merged gate, CSP violation listener fixture, xterm/uPlot style-injection check | `web/` (config, `src/main.tsx`, `src/ui/router.tsx`, `src/core/i18n/` loader, `e2e/fixtures/`) |
| WCR-01 | agent-core | 06 §2.2 `core/encoding`, `core/crypto`, `core/passkey` (option/response (de)serialisation, PRF extraction), `core/tasks/title.ts` (06 §4.2); byte-identical against vectors | `web/src/core/{encoding,crypto,passkey,tasks}/` |
| WCR-02 | agent-core | `core/log`: L1–L10, P1–P4, rights projection, W1–W3, E-rules, rotation-batch builder (R), catch-up planner; vectors incl. negative | `web/src/core/log/` |
| SCN-00 | agent-core | 07 §5.1–§5.5: orchestrator (PG cluster, hub, doubles from agent checkout, runner instances, egress proxy, control API, cleanup, VM budget check), `e2e/platform/run.sh --topology t1|t2 --runner --live --only`, Playwright config (virtual authenticator incl. PRF, mobile projects), coverage collection, CON-04 pre-check, `traceability.json` + `test_traceability.py` (R-P.4), smoke spec (PRF on hub-served page) | `e2e/platform/` |
| ACC-M0 | workspace | 09 M0 acceptance + exit gate on the work devices; integration fixes; draft PRs | – |

### M1 – Vertical slice

| ID | Repo | Scope (spec) | Owns |
|---|---|---|---|
| HUB-03 | agent-core | 03 §4.1 (register/login begin/finish, session end, `/v1/me`, bundles, settings, `GET /v1/me/bundles/recovery`), 04 §4.9, §5.1, §5.4, ADR-006, ADR-026 binding (`registered_user`, 24 h orphan cleanup job), `CK_1` self-wrap at registration, ceremonies, `browser_session` key lookup for the gate | `src/agent_hub/auth/` (except `gate.py`), `src/agent_hub/me/` |
| HUB-04 | agent-core | 03 §4.2 platform part, §4.3 platform log + enrollments, §1 root-signed requests, 04 §4.8, §5.3 (entitlement), §5.9, ADR-023/026: platform log append/verify, `device_decertify` effects (refuse, `rotation_pending`, hint), `POST /v1/invites/lookup` (platform and org shapes), platform invites, operator CLI `agent-hub platform init|invite-user|invite-device|certify-pending|grant-org-access|grant-role|decertify` | `src/agent_hub/platform/`, `src/agent_hub/invites/lookup.py` |
| HUB-05 | agent-core | 03 §4.3 (orgs, log GET/POST, wraps, catch-up), 04 §4.7, §5.3, §5.10, §6.1, §6.3, 02 §2.2 projections + rebuild: genesis (private with platform invite / non-private), L1–L8 + rotation obligation + platform-access rule, wraps W3, `GET /v1/orgs/{org}` | `src/agent_hub/orgs/`, `src/agent_hub/wraps/` |
| HUB-06 | agent-core | 03 §4.4, 02 §2.3, §3, 04 §6.4: `POST /v1/events` (E1–E9, grace epoch, idempotency), `GET /v1/scopes/{id}/events`, scope heads, NOTIFY signal API for fan-out | `src/agent_hub/events/` |
| HUB-07 | agent-core | 03 §5, §6.2: SSE `/v1/stream` (frames, cursors replay, resync, heartbeat, LISTEN/NOTIFY across processes), device WS (signed upgrade, hints, heartbeats, presence, online window), `agent_hub.stream.publish` | `src/agent_hub/stream/` |
| HUB-08 | agent-core | 03 §6.1 except outbox processing: enroll (org + platform), `GET /v1/device/state` composition (registry), `/device/logs`, `/device/platform-log`, `/device/wraps`, `/device/pull`, device tasks endpoints | `src/agent_hub/device/` |
| HUB-09 | agent-core | 03 §4.5, §4.6 (tasks, prompts, close, control, sessions), §4.6.1–§4.6.3, 02 §2.4 (+2.4.1 derivation): scheduler, estimates, re-scheduling on presence/capacity, `capacity_low`, candidates, workgroup/org device lists, `session.start`/`prompt` hints, `sessions` device-state section | `src/agent_hub/{tasks,sessions,scheduler,workgroups}/` |
| HUB-10 | agent-core | 03 §6.1 outbox: `POST /v1/device/outbox` ordered processing, `device_stream`, replay/conflict/reseal/rejected, item handlers for all kinds (event, session_transition, vm_state, operation_transition, usage, quota, metrics, audit, wrap) via registry; audit outbox actions | `src/agent_hub/outbox/` |
| RUN-03 | agent | 05 §2.1 platform enrollment (`enroll --platform`), §2.2 reconcile engine + steps 1–3, §2.3 pull loop + cursors, §2.4 inbound verification, hint handlers, log alerts | `src/agent_runner/{enroll,reconcile,inbox,orgs}/` |
| RUN-04 | agent | 05 §6.1 (steps 1–5, budget), §6.4 runner side, §10.1/§10.3 platform kind, §14 n/a: session start orchestration, guestlink journal loop (`guest_cursor`) → device events (`session.*`) + transitions + outbox, session stop/close, `agent_kind` | `src/agent_runner/sessions/`, `src/agent_runner/model/selection.py` |
| RUN-05 | agent | 05 §2.2 step 6, §6.2–§6.3, 03 §4.6.3: prompt verification, `prompt_delivery`, ordering, delivered list after wipe, suspend/resume/restart with history, interrupt/stop controls from hints | `src/agent_runner/prompts/` |
| GST-05 | agent | 05 §6.4, §7, §4.6.5–6: transcript tailer (session.output/tool), asciicast recorder (pipe-pane FIFO, monotonic offsets, `m` markers, incremental UTF-8, 100 ms/16 KiB), workspace scanner + PostToolUse file records | `src/agent_guest/{transcript,recorder,scanner}.py` |
| GST-06 | agent | 05 §6.2 control side, §6.3: input clearing, send-keys chunks, bracketed paste, Enter + capture-pane verification, `delivery_failed`, interrupt/suspend/resume (`--resume`), Stop-hook idle state | `src/agent_guest/delivery.py`, `control_routes/control.py`, `hook_addons/idle.py`; extends `control_routes/prompts.py` (GST-03, M0) |
| WEB-02 | agent-core | 06 §2.2–§2.3, §2.6, §11.1–11.2, 03 §5: `core/net` (signed fetch, response check, SSE-over-fetch, gaps, resync), `core/store` + slices registry + `useStore`, `core/persist` (IndexedDB), jobs runner, log/wrap verification jobs | `web/src/core/{net,store,persist,jobs}/` |
| WEB-03 | agent-core | 06 §2.4 (shell routes `/`, `/login`, `/invite`, `/settings` shell), §3.1 (platform path), §3.1.1, §3.2, §3.4, §11.7, §12.6 base: app shell (header slots, nav, connection banner), registration with PRF, recovery code, login/unlock, private genesis | `web/src/ui/{shell,screens/invite,screens/login,settings}/` |
| WEB-04 | agent-core | 06 §4 (entry, suggestions, send sequence incl. auto-assign batch, countdown), §5.1 | `web/src/ui/screens/{home,tasks}/`, `web/src/ui/countdown/` |
| WEB-05 | agent-core | 06 §5.2 (chat base items), §5.3, §5.5 | `web/src/ui/screens/{task,session}/`, `web/src/ui/chat-items/{prompt,session.*}.tsx`, `web/src/ui/terminal/`, `task-tabs/{chat,sessions}/` |
| SCN-01 | agent-core | E2E-01, E2E-06, E2E-07, E2E-08, PERF-01, PERF-02 (07 §5.3) on T1+T2 | `e2e/platform/specs/e2e-0{1,6,7,8}-*`, `e2e/platform/perf/` |
| SCN-02 | agent-core | E2E-10, -11, -12, -13, -14, -23, -34, -35 (+ plaintext DB scan of 09 M1) | corresponding specs/scenarios |
| ACC-M1 | workspace | 09 M1 acceptance (3 consecutive runs T1+T2, PERF, ATK on M1 golden) | – |

### M2 – Expansion

| ID | Repo | Track | Scope (spec) | Owns |
|---|---|---|---|---|
| HUB-11 | agent-core | a | 03 §4.2 org part (`POST/GET /v1/orgs/{org}/invites`, revoke), 04 §5.2, §5.2a, §5.5–§5.7, ADR-005: invite rows from `invite` entries, package storage/deletion on use/revoke/expiry (job), `invite_registered`, recovery begin/finish + rate limits, add passkey, delete passkey, `invite.accepted` hook | `src/agent_hub/invites/` (except lookup), `src/agent_hub/auth/recovery.py`, `auth/passkeys_manage.py` |
| RUN-06 | agent | a | 05 §2.1 org enrollment (`enroll --code`), §2.5 catch-up, §3.5 unassign/archive/removal/decertify handling, §1.2 `status`, `doctor` | `src/agent_runner/{catchup,assignments,doctor}/`, `enroll/org.py` |
| WEB-06 | agent-core | a | 06 §7.1, §7.2, §7.5, §11.3–§11.4 | `web/src/ui/screens/{members,workgroups}/`, `web/src/ui/shell/header/org-switcher.tsx`, `core/jobs/{catch-up,rotation}.ts` |
| WEB-07 | agent-core | a | 06 §7.3, §7.4, ADR-026 UI | `web/src/ui/screens/{devices,platform-invites}/` |
| WEB-08 | agent-core | a | 06 §3.1 org path (`org_verify`, `existing_account`, accept, display name), §11.6 | `web/src/ui/screens/invite/org/`, `core/jobs/self-wraps.ts` |
| WEB-14 | agent-core | a | 06 §3.3, §3.5, §3.6 | `web/src/ui/screens/recover/`, `settings/sections/passkeys/`, `core/persist/device-key.ts` |
| HUB-12 | agent-core | b | 03 §8.3 (H1–H6), §4.7, §9 dedupe windows, §12 expiry, ADR-012/017: operation registry, `dedupe_slot`, expiry jobs, subagent child-session creation + limits, budget hook H4, `operation.finished`/`subagent.finished` inputs | `src/agent_hub/operations/` |
| HUB-13 | agent-core | b/c | 03 §4.8, §4.10, §6.1 credential + request endpoints, 02 §2.7: credentials CRUD/rotate (hard delete, hints), device requests (states, expiry 15 min, follows), `/v1/me/devices`, `credentials_changed`/`requests` device-state sections | `src/agent_hub/{credentials,requests}/` |
| GST-07 | agent | b | 03 §8.1 (all tools), 02 §5 `pending`, 05 §12 step 1: tool schemas, platform.query/operation.request journaling, waits, `get_operation`, `list_operations`, `get_subagent_result` plumbing | `src/agent_guest/mcp_tools/*` (except get_context, get_notifications); extends GST-01's pending library (M0, merged) |
| RUN-07 | agent | b | 05 §12 (pipeline, steps 1–7), 03 §9 formal rules for every type, 04 §4.10, `task.set_title`, duplicate result loading, expiry, retries framework | `src/agent_runner/operations/` |
| RUN-08 | agent | b | 03 §8.1 read tools runner side, ADR-024, 05 §2.2 local projections (tasks, messages, whiteboards from pulled events), `platform.read` events, `read.session.<tool>` audit | `src/agent_runner/queries/` |
| RUN-09 | agent | b | 05 §11.1, §11.4, §2.6, §12.1 (list_repositories, get_pull_request, create_branch, create_pull_request incl. existing PR, comment), GitHub HTTP client rules | `src/agent_runner/{requests,credentials,github}/` (github: client + executors I) |
| RUN-10 | agent | b | 05 §12.1 checkout/commit/push, 03 §7 files import/export (guest side), 03 §9 commit snapshot + git object SHA + secret scan | `src/agent_runner/github/{checkout,commit,push,gitobj}.py`, `src/agent_guest/files.py`, `control_routes/files.py` |
| RUN-11 | agent | b | 05 §13, ADR-017: child start, one-shot completion, final text, runtime limit, `subagent.finished` ping, `get_subagent_result` | `src/agent_runner/subagents/` |
| WEB-09 | agent-core | b | 06 §8.1, §5.2 operation cards, §5.3 children, §5.4 | `settings/sections/github/`, `screens/wg-settings/`, `task-tabs/operations/`, `chat-items/operation*.tsx` |
| ATS-02 | agent | b | 07 §2.3 ATK-33..ATK-40 | `tests/platform/atk/` (new files), scenarios `atk-33..40` |
| HUB-14 | agent-core | c | 03 §4.1 `/v1/me/quotas`, §2 budget, §6.1 `platform_budget`/`quota_poll_owners` sections, H4 budget implementation, `/v1/config.models` check | `src/agent_hub/usage/` |
| RUN-12 | agent | c | 05 §10 (all kinds, catalog, cost, context, quotas, budget), copied usage parsers | `src/agent_runner/model/` (except selection base), `src/agent_runner/quota/` |
| RUN-13 | agent | c | 05 §11.2–§11.3, ADR-011, `credential.check`, `mailbox.check` | `src/agent_runner/login/` |
| WEB-10 | agent-core | c | 06 §8.2, §5.2 usage strip, §11.5 | `settings/sections/{model,credentials,quotas}/`, `core/jobs/credential-rewrap.ts` |
| HUB-15 | agent-core | d | 03 §4.9, 02 §2.6, ADR-020: whiteboard projection + rebuild, endpoints, history, notify fan-out, notifications + other rules, `session_ping` + `ping` hints + `pings` device-state section | `src/agent_hub/{whiteboards,notifications}/` |
| RUN-14 | agent | d | 05 §9, 03 §8.2 additionalContext, `get_notifications`: runner ping routing, guest ping table + hook addon + tool | `src/agent_runner/pings/`, `src/agent_guest/{pings.py,hook_addons/pings.py,mcp_tools/get_notifications.py}` |
| WEB-11 | agent-core | d | 06 §6.1, §6.2 | `screens/whiteboard/`, `task-tabs/whiteboard/`, `screens/notifications/`, `shell/header/bell.tsx` |
| RUN-15 | agent | e | 08 §1–§3, 05 §14, guest `/v1/metrics` (cgroup readers) | `src/agent_runner/metrics/`, `src/agent_guest/metrics.py` |
| MON-01 | agent | e | 08 §3 (dashboard side), §5, §7 E2E stack, ADR-018 | `deploy/monitoring/` |
| HUB-16 | agent-core | e | 03 §4.11, 08 §4, §6, 02 §2.8–§2.9: rollup job, series/overview, audit list/verify, hourly checkpoint job, read-log completeness test over all routes | `src/agent_hub/monitoring/`, `src/agent_hub/audit/{routes,jobs}.py` |
| WEB-12 | agent-core | e | 06 §9 | `screens/{monitoring,audit}/`, `task-tabs/audit/` |
| WEB-13 | agent-core | f | 06 §12, §10.5 | `web/src/ui/{responsive,a11y}/`, `screens/{security,errors}/`, cross-screen layout fixes |
| SCN-03 | agent-core | a | E2E-02, -03, -04, -05, -18, -30, -31, -37, -38, -39 | specs |
| SCN-04 | agent-core | b | E2E-19, -20, -21, -22, -24 | specs |
| SCN-05 | agent-core | c | E2E-16, -27, -28, -36; LIVE-01 + LIVE-03 smoke once | specs |
| SCN-06 | agent-core | d | E2E-25, -26 | specs |
| SCN-07 | agent-core | e | E2E-15, -17 | specs |
| SCN-08 | agent-core | f | E2E-29, mobile project for all web scenarios, PERF-03, PERF-04 | specs, perf |
| ACC-M2 | workspace | – | 09 M2 exit gate | – |

### M3 / M4

| ID | Repo | Scope | Owns |
|---|---|---|---|
| DR-01 | agent-core | 02 §3 export/restore (`agent-hub export/restore`), `rebuild-projections` CLI over all registries, 07 §5.6 E2E-33, runner store wipe path | `src/agent_hub/backup/`, `e2e-33` spec |
| SCN-09 | agent-core | E2E-09 (full canary over E2E-01/06/21/25/27), E2E-32, egress assertions (R-P.8) | specs |
| ATS-03 | agent | 09 WP-M3-02: final ATK (scripted + model-driven red team with double + one LIVE DeepSeek run + OS probes) | `tests/platform/atk/redteam/`, report in `docs/platform/` |
| PFM-01 | agent-core | 09 WP-M3-03, PERF-01..PERF-05 | `e2e/platform/perf/` |
| LVT-01 | agent-core | 07 §2.4, §6 LIVE-01..LIVE-04 | `e2e/platform/live/` |
| DOC-01 | workspace | 09 WP-M3-06/07: `docs/platform/` final, runbooks, `PLATFORM-DESIGN.md`, manual PRF matrix template (`manual-checks/`) | `docs/platform/**` (both repos) |
| ACC-M3 | workspace | 09 M3 acceptance, WP-M3-01, WP-M3-08 | – |
| DOC-02 | workspace | 09 M4 `docs/platform/CUTOVER.md` | `docs/platform/CUTOVER.md` |
| ACC-FINAL | workspace | final acceptance | – |

## 4. Conventions against collisions (summary of ADR-027)

1. Never two runnable packages on the same file: every shared mechanism is created by one early owner (FND-00,
   HUB-02, RUN-01, GST-01, WEB-01, SCN-00) and extended only by **adding files** at the registration points of ADR-027
   §4 (hub routers/jobs/CLI/outbox handlers/device-state sections, runner tasks/reconcile steps/journal handlers/
   executors/request handlers/hint handlers, guest routes/MCP tools/hook addons, web screens/nav/catalogs/settings
   sections/task tabs/chat items/header items/store slices/jobs, E2E specs/control routes/scenarios).
2. Schemas: complete in M0 (HUB-01, RUN-01); later migrations take the next free number; duplicates refused by the
   merge script.
3. Contracts: CTR-01/02 in M0; later only additive or defect fixes with `contracts/changes/<ID>.md`; `VERSION` derived;
   agent copy synced by the merge script.
4. Fixtures: component conftests owned by HUB-01/HUB-02, RUN-01, GST-01, DBL-01, WEB-01, SCN-00; areas use
   sub-directory conftests.
5. Dependencies fixed by FND-00 (Python) and WEB-01 (web).
6. Devices: `WP_PORT_BASE` (20 ports per package), VM prefix `wp-<tag>-`, remote homes `~/wp/<tag>/`, golden caches
   `cache-golden-<sha12>`.
7. Merges are serial (`tools/wp/merge.sh`), fast-forward, never into `develop`.

## 5. Requirement → package → test

Columns: requirement ID (`01-requirements.md`), implementing packages, E2E/gate tests (from `01-requirements.md`),
package that writes and first runs the E2E test. Every requirement has at least one implementing package and at
least one E2E test or gate; ACC-FINAL re-checks the matrix with `e2e/platform/test_traceability.py`.

<!-- generated table follows -->

| ID | Implementing packages | Tests / gates (01-requirements) | E2E written/run first by |
|---|---|---|---|
| R-0.1 | HUB-03, HUB-04, HUB-05, HUB-06, WEB-03, WCR-01, RUN-02 | E2E-01, E2E-09 | SCN-01, SCN-09 |
| R-0.2 | HUB-01, HUB-05, HUB-09, WEB-04, WEB-05 | E2E-08, U-HUB-schema | SCN-01 |
| R-0.3 | HUB-09, HUB-12, HUB-07 | E2E-06, E2E-38 | SCN-01, SCN-03 |
| R-0.4 | HUB-09, RUN-04 | E2E-06, E2E-38 | SCN-01, SCN-03 |
| R-0.5 | WEB-03, WEB-04, HUB-05, HUB-09 | E2E-01, E2E-05 | SCN-01, SCN-03 |
| R-1.1 | HUB-03, HUB-04, WEB-03 | E2E-01 (incl. `register/begin` without invitation refused), E2E-02, U-HUB-passkeys | SCN-01, SCN-03 |
| R-1.2 | HUB-04, HUB-05, WEB-03, WEB-07 | E2E-01 | SCN-01 |
| R-1.3 | HUB-04, HUB-08, HUB-11, RUN-03, RUN-06 | E2E-04 (incl. enroll with used/invalid code → `invite_unknown`), E2E-05 | SCN-03 |
| R-1.4 | CRY-01, WCR-01, HUB-04, HUB-11, WEB-06, WEB-07 | E2E-02, E2E-03 | SCN-03 |
| R-1.5 | HUB-05, HUB-11, WEB-08 | E2E-02 | SCN-03 |
| R-1.6 | HUB-03, WEB-03, WCR-01 | E2E-01, E2E-30 | SCN-01, SCN-03 |
| R-1.7 | HUB-03, WEB-03 | E2E-01, E2E-30 | SCN-01, SCN-03 |
| R-1.8 | RUN-03, RUN-06, HUB-08 | E2E-04 | SCN-03 |
| R-1.9 | HUB-02, HUB-07, HUB-08, RUN-02 | E2E-04, U-HUB-auth | SCN-03 |
| R-1.10 | HUB-04, HUB-05, HUB-11, CRY-02 | E2E-03 | SCN-03 |
| R-1.11 | HUB-04, HUB-11, WEB-06 | E2E-03 | SCN-03 |
| R-1.12 | HUB-04, HUB-05, HUB-11, RUN-06, WEB-06, WEB-07 | E2E-03, E2E-04, E2E-05 (decertification steps) | SCN-03 |
| R-1.13 | HUB-11, WEB-08 | E2E-39 | SCN-03 |
| R-2.1 | WEB-04 | E2E-01, E2E-06 | SCN-01 |
| R-2.2 | WEB-08, HUB-11 | E2E-02 | SCN-03 |
| R-2.3 | WEB-03, HUB-05 | E2E-01 | SCN-01 |
| R-2.4 | HUB-05, WEB-06 | E2E-37 | SCN-03 |
| R-2.5 | WEB-04 | E2E-37, E2E-06 | SCN-03, SCN-01 |
| R-2.6 | HUB-09, WEB-04, WEB-05 | E2E-08 | SCN-01 |
| R-2.7 | HUB-09, WCR-01 | E2E-08 | SCN-01 |
| R-2.8 | WCR-01, WEB-04, RUN-07 | E2E-08 | SCN-01 |
| R-2.9 | HUB-09 | E2E-08, E2E-37 | SCN-01, SCN-03 |
| R-2.10 | HUB-01, WEB-04 | E2E-08, U-HUB-schema | SCN-01 |
| R-3.1 | RUN-04, VM-02 | E2E-06 | SCN-01 |
| R-3.2 | HUB-11, RUN-06, WEB-07 | E2E-04 | SCN-03 |
| R-3.3 | HUB-05, WEB-07, WCR-02 | E2E-38 | SCN-03 |
| R-3.4 | HUB-04, HUB-09, RUN-03, WEB-04 | E2E-05 | SCN-03 |
| R-3.5 | RUN-06, HUB-08 | E2E-04, E2E-36 | SCN-03, SCN-05 |
| R-3.6 | RUN-01, RUN-06 | E2E-04 (enrollment on the macOS runner device), U-RUN-config (non-darwin refused, R-P exception: a non-macOS host is not available in the E2E topologies), `agent-runner doctor` assertion in E2E-04 | SCN-03 |
| R-3.7 | VM-02, RUN-04, GST-01 | E2E-11 | SCN-02 |
| R-3.8 | VM-02, GST-02 | E2E-10 | SCN-02 |
| R-3.9 | SBX-01, SBX-02, GST-02, GST-03 | E2E-10, ATK-01..ATK-40 | SCN-02, ATS-01 |
| R-3.10 | VM-01, VM-02, RUN-04, HUB-09, PRB-01 | E2E-06, PERF-01, PERF-02 ; PERF-01 includes 10 runs "new workgroup in an existing org" | SCN-01, SCN-01/PFM-01 |
| R-3.11 | WEB-04 | E2E-06 | SCN-01 |
| R-3.12 | WEB-04, HUB-09 | E2E-07 | SCN-01 |
| R-3.13 | SCN-00, HUB-02, RUN-02 | E2E-32 | SCN-09 |
| R-4.1 | SBX-01, GST-02 | E2E-06 | SCN-01 |
| R-4.2 | HUB-13, RUN-12, RUN-13, WEB-10 | E2E-27, E2E-28, LIVE-03 | SCN-05, PRB-01/SCN-05/LVT-01 |
| R-4.3 | HUB-13, RUN-12, GST-04, WEB-10 | E2E-27, LIVE-04 | SCN-05, LVT-01 |
| R-4.4 | RUN-13, DBL-03 | E2E-28 | SCN-05 |
| R-4.5 | RUN-04, RUN-12, GST-04, HUB-14 | E2E-27, E2E-36, LIVE-01 | SCN-05, SCN-05/LVT-01 |
| R-4.6 | RUN-04, GST-04, CTR-02 | E2E-27, LIVE-01 | SCN-05, SCN-05/LVT-01 |
| R-4.7 | PRB-01, SCN-05, LVT-01 | LIVE-01, E2E-27 | SCN-05/LVT-01, SCN-05 |
| R-4.8 | HUB-09, GST-02 | E2E-06 (asserts `agent_kind = claude-code`) | SCN-01 |
| R-5.1 | WCR-01, RUN-04, WEB-03, WEB-05 | E2E-09, CON-03 | SCN-09, CRY-02/WCR-02 |
| R-5.2 | HUB-01, HUB-06 | E2E-09 | SCN-09 |
| R-5.3 | HUB-01 | E2E-09, U-HUB-schema | SCN-09 |
| R-5.4 | WCR-02, HUB-05, WEB-06 | E2E-31, E2E-18 | SCN-03 |
| R-5.5 | RUN-03, CRY-03 | E2E-05, E2E-09 | SCN-03, SCN-09 |
| R-5.6 | CRY-01, WCR-01, HUB-03, HUB-05 | E2E-30, E2E-31, CON-02 | SCN-03, CRY-01/CRY-02/CRY-03/WCR-01/WCR-02 |
| R-6.1 | GST-01, RUN-02 | E2E-14 | SCN-02 |
| R-6.2 | RUN-02, HUB-10 | E2E-14, PERF-03 | SCN-02, SCN-08/PFM-01 |
| R-6.3 | GST-05, RUN-04, HUB-06 | E2E-13, E2E-21 | SCN-02, SCN-04 |
| R-6.4 | GST-04, RUN-12, HUB-10, HUB-14, WEB-10 | E2E-16 | SCN-05 |
| R-6.5 | RUN-12, HUB-14, WEB-10 | E2E-16, LIVE-03 | SCN-05, PRB-01/SCN-05/LVT-01 |
| R-6.6 | WEB-13, WEB-05 | E2E-29 | SCN-08 |
| R-6.7 | HUB-01, HUB-16, HUB-10 | E2E-15  (asserts `audit.event_id` links to the encrypted change event) | SCN-07 |
| R-6.8 | RUN-02, HUB-06, HUB-10, WEB-02 | E2E-14, E2E-33 | SCN-02, DR-01 |
| R-6.9 | HUB-07, WEB-02 | PERF-03, PERF-04, E2E-14 | SCN-08/PFM-01, SCN-02 |
| R-6.10 | WEB-05 | E2E-13 | SCN-02 |
| R-6.11 | GST-05, WEB-05 | E2E-12 | SCN-02 |
| R-6.12 | WEB-05, GST-03, GST-06 | E2E-12, E2E-35 | SCN-02 |
| R-7.1 | RUN-15, HUB-16, WEB-12 | E2E-17 | SCN-07 |
| R-7.2 | RUN-15, HUB-16, WEB-12 | E2E-17 | SCN-07 |
| R-7.3 | HUB-16, MON-01 | E2E-17 | SCN-07 |
| R-7.4 | MON-01, RUN-15 | E2E-17 | SCN-07 |
| R-7.5 | MON-01 | E2E-17 | SCN-07 |
| R-7.6 | HUB-16, WEB-12 | E2E-17 | SCN-07 |
| R-7.7 | HUB-09, MON-01, WEB-07 | E2E-17 | SCN-07 |
| R-7.8 | HUB-09 | E2E-38 | SCN-03 |
| R-8.1 | HUB-11, WEB-06 | E2E-02, E2E-18 | SCN-03 |
| R-8.2 | HUB-05, WEB-06 | E2E-18  (step: `grant` of an existing member at workgroup level) | SCN-03 |
| R-8.3 | CRY-02, HUB-05, WEB-06 | E2E-02 (default role member = read), E2E-18 | SCN-03 |
| R-8.4 | CRY-02, HUB-05 | E2E-18, E2E-02 | SCN-03 |
| R-8.5 | HUB-02, HUB-05, HUB-06, HUB-09 | E2E-18, U-HUB-rights | SCN-03 |
| R-8.6 | WEB-06, WCR-02 | E2E-18 | SCN-03 |
| R-8.7 | RUN-08, GST-07 | E2E-19 | SCN-04 |
| R-8.8 | RUN-08 | E2E-19 | SCN-04 |
| R-8.9 | HUB-02, HUB-16, RUN-08 | E2E-15, E2E-19 | SCN-07, SCN-04 |
| R-9.1 | HUB-13, RUN-09, WEB-09 | E2E-20 | SCN-04 |
| R-9.2 | RUN-09, RUN-10 | E2E-21, LIVE-02 | SCN-04, LVT-01 |
| R-9.3 | RUN-09, RUN-10 | E2E-21, LIVE-02 | SCN-04, LVT-01 |
| R-9.4 | RUN-07, RUN-09 | E2E-22  (incl. `repo_not_accessible`) | SCN-04 |
| R-9.5 | HUB-12, RUN-07 | E2E-15, E2E-21 | SCN-07, SCN-04 |
| R-9.6 | RUN-09, DBL-02 | E2E-20, LIVE-02 | SCN-04, LVT-01 |
| R-9.7 | SBX-01, SBX-02, RUN-10 | E2E-23, ATK-02 | SCN-02, ATS-01 |
| R-10.1 | GST-02, SBX-01 | E2E-23 | SCN-02 |
| R-10.2 | SBX-01 | E2E-23 | SCN-02 |
| R-10.3 | SBX-01, SBX-02, GST-03 | E2E-23, ATK-01..ATK-12 | SCN-02, ATS-01 |
| R-10.4 | SBX-01, GST-03 | E2E-23, ATK-05 | SCN-02, ATS-01 |
| R-10.5 | SBX-01, SBX-02, GST-03 | E2E-34, ATK-13..ATK-20 | SCN-02, ATS-01 |
| R-10.6 | GST-07, RUN-07, RUN-11, HUB-12 | E2E-24 | SCN-04 |
| R-10.7 | GST-03 | E2E-23, ATK-13 | SCN-02, ATS-01 |
| R-10.8 | SBX-02 | ATK-21..ATK-32, E2E-34 | ATS-01, SCN-02 |
| R-10.9 | GST-07, RUN-07 | E2E-22, ATK-33 | SCN-04, ATS-02 |
| R-10.10 | RUN-07, HUB-12 | E2E-22 | SCN-04 |
| R-10.11 | RUN-09, RUN-10 | E2E-21 | SCN-04 |
| R-10.12 | GST-03, RUN-04, HUB-10 | E2E-23, E2E-15 | SCN-02, SCN-07 |
| R-11.1 | HUB-12, RUN-07, GST-07 | E2E-21, E2E-22 | SCN-04 |
| R-11.2 | RUN-07, GST-07 | E2E-22 | SCN-04 |
| R-11.3 | HUB-12 | E2E-22, E2E-18 | SCN-04, SCN-03 |
| R-11.4 | RUN-07, HUB-12, CRY-01 | E2E-22 | SCN-04 |
| R-11.5 | RUN-07, HUB-12 | E2E-22 | SCN-04 |
| R-11.6 | GST-07, RUN-07, HUB-12 | E2E-22 | SCN-04 |
| R-11.7 | HUB-12 | E2E-22, E2E-15 | SCN-04, SCN-07 |
| R-11.8 | GST-07, RUN-07, RUN-09, HUB-12 | E2E-21, LIVE-02 | SCN-04, LVT-01 |
| R-11.9 | HUB-01, HUB-12 | E2E-21 (asserts no PR table) | SCN-04 |
| R-12.1 | HUB-15, WEB-11 | E2E-25 | SCN-06 |
| R-12.2 | HUB-15, HUB-01 | E2E-25 | SCN-06 |
| R-12.3 | HUB-15, WEB-11, CRY-02 | E2E-25 | SCN-06 |
| R-12.4 | HUB-15, WEB-11 | E2E-25 | SCN-06 |
| R-12.5 | WEB-11, WCR-01 | E2E-25, E2E-09 | SCN-06, SCN-09 |
| R-12.6 | HUB-07, HUB-15, WEB-11 | E2E-25, PERF-03 | SCN-06, SCN-08/PFM-01 |
| R-12.7 | HUB-15, WEB-11 | E2E-25 | SCN-06 |
| R-12.8 | HUB-15, WEB-11 | E2E-26 | SCN-06 |
| R-12.9 | HUB-15, RUN-14 | E2E-26 | SCN-06 |
| R-12.10 | RUN-14, RUN-08 | E2E-26  (asserts a working session's turn continues without a new prompt and the tmux pane receives no input) | SCN-06 |
| R-13.1 | GST-07 | E2E-24 | SCN-04 |
| R-13.2 | RUN-07, HUB-12 | E2E-24 | SCN-04 |
| R-13.3 | SBX-01, RUN-11 | E2E-24, ATK-05 | SCN-04, ATS-01 |
| R-13.4 | RUN-11, GST-02, RUN-15 | E2E-24, E2E-17  (asserts the child session appears in the monitoring overview) | SCN-04, SCN-07 |
| R-13.5 | HUB-12 | E2E-24 | SCN-04 |
| R-13.6 | WEB-09 | E2E-24 | SCN-04 |
| R-13.7 | GST-07, HUB-12 | E2E-24 | SCN-04 |
| R-13.8 | HUB-12, RUN-11 | E2E-24 | SCN-04 |
| R-14.1 | CRY-01, CRY-02, WCR-01, WCR-02, HUB-05 | E2E-30, E2E-31, CON-02 | SCN-03, CRY-01/CRY-02/CRY-03/WCR-01/WCR-02 |
| R-14.2 | SBX-01, SBX-02, GST-02, ATS-01 | ATK-01..ATK-40, E2E-10 | ATS-01, SCN-02 |
| R-14.3 | VM-01, VM-02, PRB-01 | PERF-01, PERF-02 | SCN-01/PFM-01 |
| R-14.4 | GST-04, RUN-12, PRB-01 | LIVE-01, LIVE-03, E2E-16 | SCN-05/LVT-01, PRB-01/SCN-05/LVT-01, SCN-05 |
| R-14.5 | CTR-01, CTR-02, CTR-03, RUN-07 | CON-01, E2E-22 | CTR-01/CTR-02, SCN-04 |
| R-14.6 | HUB-09, RUN-02, RUN-03 | E2E-14, E2E-38 | SCN-02, SCN-03 |
| R-14.7 | HUB-01, HUB-12 | E2E-21 | SCN-04 |
| R-15.1 | FND-00 | CON-04, E2E-32 | CTR-01/SCN-00, SCN-09 |
| R-15.2 | FND-00 | CON-04, E2E-32 | CTR-01/SCN-00, SCN-09 |
| R-15.3 | MON-01 | E2E-17 | SCN-07 |
| R-P.1 | ACC-FINAL | this matrix; M3 acceptance | FND-00 gates / ACC-FINAL |
| R-P.2 | FND-00 | CON-04, U-HUB-legacy-untouched, U-RUN-legacy-untouched | CTR-01/SCN-00 |
| R-P.3 | FND-00 | coverage gates in `make platform-ci` and CI | FND-00 gates / ACC-FINAL |
| R-P.4 | SCN-00 | traceability test `e2e/platform/test_traceability.py` | FND-00 gates / ACC-FINAL |
| R-P.5 | SCN-00, DBL-01, DBL-02, DBL-03 | E2E-01..E2E-39 (network egress check) | SCN-01, SCN-03 |
| R-P.6 | LVT-01 | LIVE-01..LIVE-04 | SCN-05/LVT-01 |
| R-P.7 | SCN-00 | E2E-32, all E2E marked "both" | SCN-09 |
| R-P.8 | SCN-00, SCN-09 | E2E-32 (asserts no non-LAN egress) | SCN-09 |
| R-P.9 | FND-00 | U-HUB-hygiene, U-RUN-hygiene (repository scan test) | FND-00 gates / ACC-FINAL |
| R-P.10 | PRB-01, LVT-01 | LIVE-03 (asserts profile files unchanged) | PRB-01/SCN-05/LVT-01 |
| R-P.11 | FND-00, ACC-M3 | M3 acceptance (branch protection check) | FND-00 gates / ACC-FINAL |
| R-P.12 | HUB-04, HUB-05, WEB-07 | E2E-01 (private org gets platform device), E2E-05 (non-private org refused with `platform_access_denied` until granted) | SCN-01, SCN-03 |
| R-P.13 | HUB-14, RUN-12, GST-04 | E2E-27 (budget step) | SCN-05 |

Rows without an `E2E-*` ID in `01-requirements.md` are decision/guardrail rows (R-14, R-P); their E2E evidence:
R-14.3 → E2E-06 (asserts `first_working_at − created_at < 10 s`) + PERF-01/02; R-P.1 → the whole matrix
(`test_traceability.py`, ACC-FINAL); R-P.2 → CON-04 pre-check of every E2E run + legacy-untouched tests;
R-P.3 → coverage gates (E2E coverage collected for information, `07-testing.md` §3); R-P.4 → `test_traceability.py`
runs at the start of every E2E run (SCN-00); R-P.6 → LIVE-01..04 drive the full E2E chain against real providers
(LVT-01); R-P.9 → hygiene tests + artefact secret scan after every E2E run (SCN-00); R-P.10 → LIVE-03 (PRB-01, LVT-01);
R-P.11 → branch/PR check in ACC-M3 and ACC-FINAL.

## 6. Specification section → package

| Spec | Sections → packages |
|---|---|
| 00 | §2 components/§5 layout/§6 branches/§7 coexistence → FND-00; §3 trust boundaries → all, verified by SCN-09, ATS-*; §4 tech → FND-00, WEB-01 |
| 01 | traceability → SCN-00 (`traceability.json`, test), every SCN, ACC-FINAL |
| 02 | §1, §2.1–§2.9 schema → HUB-01; §2.1 auth → HUB-03; §2.2 → HUB-05 (projections), HUB-04 (platform); §2.3, §3 → HUB-06, DR-01 (export); §2.4 → HUB-09; §2.5 → HUB-12; §2.6 → HUB-15; §2.7 → HUB-13; §2.8 → HUB-10, HUB-14, HUB-16; §2.9 → HUB-01, HUB-16; §4 → RUN-01; §5 → GST-01; §6 → HUB-01, RUN-01, GST-01 |
| 03 | §1 → HUB-02, RUN-02, CTR-01; §2 → HUB-02; §3 → HUB-02 (decorators), each endpoint package; §4.1 → HUB-03 (+HUB-11 recovery/passkeys, HUB-14 quotas, HUB-13 devices); §4.2 → HUB-04 (platform), HUB-11 (org); §4.3 → HUB-05, HUB-04 (platform log/enrollments); §4.4 → HUB-06; §4.5–§4.6.3 → HUB-09; §4.7 → HUB-12; §4.8 → HUB-13; §4.9 → HUB-15; §4.10 → HUB-13; §4.11 → HUB-16; §5 → HUB-07, WEB-02; §6.1 → HUB-08, HUB-10, RUN-02, RUN-03; §6.2 → HUB-07, RUN-02; §7 → GST-01, GST-02, GST-06, RUN-10 (files), RUN-15 (metrics), VM-02 (transport); §7.2 → GST-05, CTR-02; §8.1 → GST-04, GST-07, RUN-08; §8.2 → GST-03, RUN-14; §8.3 → HUB-12, RUN-07; §9 → RUN-07, RUN-09, RUN-10, RUN-11, HUB-12; §10 → CTR-01, producers; §11 → CTR-01 enums, all; §12 → owners of each limit |
| 04 | §1–§4.6, §4.9–§4.10, §10 → CRY-01, WCR-01, CRY-03; §4.7–§4.8, §6 → CRY-02, WCR-02, HUB-05, HUB-04; §5.1, §5.4 → HUB-03, WEB-03; §5.2–§5.2a → HUB-11, WEB-08; §5.3 → HUB-05, WEB-03; §5.5–§5.6 → HUB-11, WEB-14; §5.7 → HUB-11, WEB-06; §5.8 → RUN-06, HUB-08; §5.9 → HUB-04, RUN-03, WEB-04, WEB-07; §5.10 → HUB-05, WEB-06, RUN-06; §5.11 → WEB-06, RUN-06; §5.12 → RUN-02, WEB-02; §7 → HUB-01, SCN-09; §8 → CTR-01; §9 → ATS-03, SCN-09, DOC-01 |
| 05 | §1.1–§1.4 → RUN-01; §1.2 CLI: `enroll` RUN-03/RUN-06, `image` VM-01, `pool` VM-02, `status`/`doctor` RUN-06; §1.5 → RUN-01, RUN-02, RUN-12, RUN-13; §2.1 → RUN-03, RUN-06; §2.2 → RUN-03 (+ steps by RUN-04/05/06/09/14); §2.3 → RUN-02, RUN-03; §2.4 → RUN-03; §2.5 → RUN-06; §2.6 → RUN-09; §3.1 → SBX-01, VM-01; §3.2–§3.5 → VM-02, RUN-06 (§3.5 removal); §4.1–§4.2 → SBX-01; §4.3–§4.6 → SBX-02, GST-02, SBX-01 (nftables/mounts); §5.1 → GST-01; §5.2–§5.5 → GST-03; §6.1 → RUN-04; §6.2–§6.3 → GST-06, RUN-05; §6.4 → GST-05, RUN-04; §6.5 → RUN-07; §7 → GST-05; §8 → SBX-01 (RULES), GST-02 (render); §9 → RUN-14, HUB-15; §10 → GST-04, RUN-04, RUN-12; §11 → RUN-09, RUN-13; §12 → RUN-07, RUN-09, RUN-10; §13 → RUN-11, HUB-12; §14 → RUN-15; §15 → SBX-01, ATS-03, DOC-01 |
| 06 | §1–§2 → WEB-01, WEB-02; §3.1–§3.2, §3.4 → WEB-03 (platform), WEB-08 (org); §3.3, §3.5, §3.6 → WEB-14; §4 → WCR-01 (title), WEB-04; §5.1 → WEB-04; §5.2–§5.5 → WEB-05, WEB-09; §6 → WEB-11; §7.1–§7.2, §7.5 → WEB-06; §7.3–§7.4 → WEB-07; §8.1 → WEB-09; §8.2 → WEB-10; §9 → WEB-12; §10.1–§10.4 → WEB-01; §10.5 → WEB-13; §11 → WEB-02 (1, 2, 7), WEB-06 (3, 4), WEB-10 (5), WEB-08 (6); §12 → WEB-13 (+ every screen package for its screen); §13 → WEB-01, every web package |
| 07 | §1, §3, §7, §8 → FND-00 (+ SBX-02 Linux job, WEB-01 web gate); §2.1 → SCN-01…SCN-09, DR-01; §2.2 → SCN-01, SCN-08, PFM-01; §2.3 → ATS-01, ATS-02, ATS-03; §2.4, §6 → PRB-01, SCN-05, LVT-01; §2.5, §4 → CTR-01, CTR-02, CRY-*, WCR-*, HUB-01, RUN-01; §5 → SCN-00; §5.3 → SCN-01, PFM-01; §5.6 → DR-01; §9 → DBL-01, DBL-02, DBL-03, SCN-00 (fault switches) |
| 08 | §1–§3 → RUN-15; §4 → HUB-10, HUB-16; §5 → MON-01; §6 → HUB-16, WEB-12; §7 → RUN-15, MON-01, SCN-07 |
| 09 | M0 → FND-00 … SCN-00, ACC-M0; M1 → HUB-03 … SCN-02, ACC-M1; M2a–f → as §3; M3 WP-01 → ACC-M3, WP-02 → ATS-03, WP-03 → PFM-01, WP-04 → LVT-01, WP-05 → DR-01, WP-06/07 → DOC-01, WP-08 → ACC-M3; M4 → DOC-02 |
| ADR | 001 → FND-00; 002 → HUB-09, HUB-12; 003 → HUB-01, SCN-09; 004–006 → HUB-03, HUB-04, HUB-11, WEB-03, WEB-14; 007 → WEB-04, WEB-07, HUB-05; 008 → SBX-*, GST-02/03; 009 → VM-*, PRB-01; 010 → GST-04, RUN-12, PRB-01; 011 → RUN-13; 012 → RUN-07, HUB-12; 013 → HUB-09, RUN-02/03; 014 → HUB-01, HUB-12; 015 → RUN-09, RUN-10, DBL-02; 016 → GST-05, WEB-05; 017 → HUB-12, RUN-11; 018 → RUN-15, MON-01; 019 → WEB-01; 020 → HUB-15, RUN-14; 021 → GST-02; 022 → RUN-01, GST-01; 023 → HUB-04; 024 → RUN-08; 025 → GST-04, GST-05, GST-06; 026 → HUB-04, HUB-05, WEB-07; 027 → FND-00 and all |

## 7. Test catalog → package

| Tests | Written and first green in (first named package owns the final file) | Re-run by |
|---|---|---|
| E2E-01, -06, -07, -08 | SCN-01 | ACC-M1, ACC-M2, ACC-M3 |
| E2E-10, -11, -12, -13, -14, -23, -34, -35 | SCN-02 | ACC-M1 … ACC-M3 |
| E2E-02, -03, -04, -05, -18, -30, -31, -37, -38, -39 | SCN-03 | ACC-M2, ACC-M3 |
| E2E-19, -20, -21, -22, -24 | SCN-04 | ACC-M2, ACC-M3 |
| E2E-16, -27, -28, -36 | SCN-05 | ACC-M2, ACC-M3 |
| E2E-25, -26 | SCN-06 | ACC-M2, ACC-M3 |
| E2E-15, -17 | SCN-07 | ACC-M2, ACC-M3 |
| E2E-29 + mobile project of all web scenarios | SCN-08 | ACC-M2, ACC-M3 |
| E2E-09, E2E-32 | SCN-09 (partial plaintext scan already SCN-02) | ACC-M3 |
| E2E-33 | DR-01 | ACC-M3 |
| PERF-01, -02 | PFM-01 (final owner); SCN-01 writes the first version | ACC-M1, ACC-M3 |
| PERF-03, -04 | PFM-01 (final owner); SCN-08 writes the first version | ACC-M3 |
| PERF-05 | PFM-01 | ACC-M3 |
| ATK-01..ATK-32 | ATS-01 | ACC-M0, ACC-M1 (M1 golden), ATS-03 |
| ATK-33..ATK-40 | ATS-02 | ATS-03 |
| LIVE-03 | LVT-01 (final owner); PRB-01 first check, SCN-05 smoke |
| LIVE-01 | LVT-01 (final owner); SCN-05 smoke |
| LIVE-02, LIVE-04 | LVT-01 | – |
| CON-01 | CTR-01 (owner of the harness), CTR-02, CTR-03 | every merge (gates) |
| CON-02, CON-03 | CRY-01 (owner of the generator), CRY-02, CRY-03, WCR-01, WCR-02 | every merge |
| CON-04 | CTR-01 (owner: hash test), SCN-00 (E2E pre-check) | every E2E run |
| MIG-* | HUB-01, RUN-01 (+ any later migration) | every merge |
| U-*-hygiene, U-*-legacy-untouched | FND-00 | every merge |

## 8. Planning risks

| Risk | Mitigation in the plan |
|---|---|
| Long chain (25) and serial milestone gates | Critical-path packages first in the index; acceptance packages fix integration defects themselves instead of reopening packages |
| FND-00 is large (repos + tools + devices) | Explicitly allowed to use subagents; skeletons are minimal; every later package depends on its scripts, so they are tested hermetically |
| Clone path > 9 s (WP-M0-14) | PRB-01 measures in M0; ACC-M0 must record the go/no-go and, if needed, add a revision package before M1 |
| Web merged 100 % coverage needs Playwright specs per screen | Every WEB package owns its screen specs; WEB-13 closes gaps |
| Contract defects found late | ADR-027 §3 amendment rule with gates in both repositories |
| Shared devices overloaded by four parallel packages with VMs | VM prefixes, memory-budget check of the orchestrator, cleanup duty, golden cache |
| Manual PRF device matrix (WP-M3-06) needs humans | DOC-01 prepares the checklist and runs all automatable variants; open rows are reported as residual risk |
