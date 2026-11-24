# 07 Testing: strategy, gates, E2E, live tests, CI

> As built: where the implementation refines or deviates from this document, see the register
> [`spec-deviations.md`](../spec-deviations.md#07-testing) (DOC-01).

Status: binding. Goal (`PROJECT.md`): 100 % line **and** branch coverage for all new code, technically
enforced, and real end-to-end tests over the full chain browser → hub → runner → Lima VM → Claude Code,
including passkeys via a virtual authenticator. Every requirement of `01-requirements.md` has at least one
E2E test. Devices are named by role: **runner device**, **second device**, **hub device**.

## 1. Test levels and tools

| Component | Level | Tools | Infrastructure |
|---|---|---|---|
| HUB (`agent_hub`) | unit + integration (`tests/platform/hub/`) | pytest, coverage (branch), httpx `ASGITransport`/Starlette `TestClient`, `websockets` client | Real PostgreSQL ≥ 16 via `AGENT_HUB_TEST_POSTGRES` (fixture creates `t_hub_<hex>` with owner/writer roles; missing variable → `pytest.fail`, never skip – legacy rule of `agent-core/tests/conftest.py`). Fixed clock injected. |
| RUN (`agent_runner`) | unit + integration (`tests/platform/runner/`) | pytest, coverage | Ephemeral PostgreSQL cluster (pattern `agent/tests/conftest.py`: `AGENT_RUNNER_TEST_PG` or `initdb` per session, one database per test). `limactl`, `launchctl`, `tmux` etc. injected as callables (legacy injection style); real Lima only in E2E. |
| GST (`agent_guest`) pure parts | unit | pytest, coverage | any OS: MCP, hook policy, gateway (against model double), journal, recorder, transcript parser, control API. |
| GST Linux-only parts | integration (`tests/platform/guest_linux/`, marker `linux`) | pytest, coverage | Linux kernel ≥ 6.8 with Landlock ABI ≥ 4, systemd, nftables, cgroup v2: GitHub `ubuntu-24.04` runner (root via sudo) **and** the Lima test VM `guest-ci` cloned from the current golden image on the runner device. Covers `sandbox/landlock.py`, `sandbox/unit.py` (systemd-run properties), nftables rules, cgroup readers, user creation, tmux/pipe-pane, file import/export. |
| SBX | attack suite ATK (§2.3) | pytest driving real Claude Code against the adversary model double | real Lima VM from the golden image |
| DBL (`agent_doubles`) | unit | pytest, coverage | stdlib only; each double tested against recorded real request shapes (Spike C journal, GitHub REST fixtures). |
| WEB (`web/`) | unit | Vitest, `@vitest/coverage-v8` | Node 24; `web/src/core/**` runs without a browser (WebCrypto in Node). |
| WEB | component/E2E | Playwright (Chromium only), V8 coverage via `monocart-coverage-reports` | Virtual authenticator via CDP: `WebAuthn.enable {enableUI:false}`, `WebAuthn.addVirtualAuthenticator {protocol:"ctap2", ctap2Version:"ctap2_1", transport:"internal", hasResidentKey:true, hasUserVerification:true, isUserVerified:true, automaticPresenceSimulation:true, hasPrf:true}`; second authenticator with `transport:"usb"`; `hasPrf:false` variant; UV-refusal case run last (Spike D observation). Mobile emulation: Pixel 7 and iPhone 15 device descriptors on Chromium. |
| Cross-repo | contract (CON), migration (MIG) | pytest + Vitest | §4 |
| Whole system | E2E, PERF, LIVE | orchestrator (Python) + Playwright | §5–§6 |

Unit/integration suites are referenced as `U-<COMP>-<area>`: U-HUB-{auth, passkeys, invites, log, wraps,
events, tasks, scheduler, operations, credentials, requests, whiteboards, notifications, stream, device-api,
monitoring, audit, migrate, schema, rights, config, hygiene, legacy-untouched}; U-RUN-{config, identity,
crypto, store, outbox, inbox, ws, vm, pool, golden, guestlink, sessions, prompts, operations, github,
credentials, model, quota, login, metrics, service, hygiene, legacy-untouched}; U-GST-{mcp, hooks, policy,
gateway, journal, control, sessions, recorder, transcript, pings, sandbox}; U-DBL-{model, github, mail,
login}; U-WEB-{crypto, log, sync, stream, ui-*}.

## 2. Test ID catalog

### 2.1 E2E tests

Location: `agent-core/e2e/platform/specs/e2e-NN-<slug>.spec.ts` (browser parts) driven by the orchestrator
`e2e/platform/orchestrator/`. "Both" = runs in T1 and T2. Every E2E test runs against the doubles and asserts at
the end: no outbound connection except to the doubles (egress log of the orchestrator proxy), model double
`state.ok`, no unconsumed GitHub/mail double expectations, hub audit chain verifies.

| ID | Name | Topology | Steps (summary) | Asserted outcomes |
|---|---|---|---|---|
| E2E-01 | Platform invitation → private org | both | Operator CLI grants `inviter`; inviter creates platform invitation; new browser opens link, registers passkey (PRF), lands in private org without a code screen (KOR-22; the spec then creates a recovery code in the settings to open the identity); a passkey without PRF is refused at registration, leaves no account and the same link registers with another passkey (browser spec `invite-platform.spec.ts`) | Private org + default workgroup exist; genesis verified; suggestion cards shown; hub holds no code/PRF/plaintext Negative: `register/begin` without an invitation and with a consumed invitation → refused; second `POST /v1/orgs {private:true}` without a fresh platform invitation → `platform_invite_required`; the private org has `platform_access`. |
| E2E-02 | Org invitation (user) | both | Admin invites member (role member, extra wg read); invitee registers and accepts | Org is default context; member role = read; default wg write; extra wg read; log `accept` with `inv_sig` Default role of the new member is `member` (read on org level). |
| E2E-03 | Invitation lifecycle | T1 | Reuse a used code; let a code expire (clock); revoke an open user and device invitation | `invite_unknown` for used/revoked/expired; package deleted; audit rows |
| E2E-04 | Org device enrollment and unpairing | both | Admin creates device invitation; `agent-runner enroll --code`; device authenticates; admin unpairs | Device active with signed requests and WS; after unpairing: rotation batch, device refused (`key_revoked`/`forbidden`), VM deleted, new events unreadable for it Negative: `enroll` with a used and with an invalid code → `invite_unknown`/`invite_code_invalid`; `agent-runner doctor` reports `os: darwin` as supported. |
| E2E-05 | Platform device certification and auto-assignment | both | Operator `invite-device`, runner `enroll --platform`, `certify-pending`; private-org user sends first prompt | `device_assign` signed by the user, verified against compiled root key; wraps for device; task runs there Decertification: operator appends `device_decertify`; hub refuses the device (`key_revoked`), sets `rotation_pending`; the admin browser performs `device_unassign` + rotate batch automatically; the device cannot decrypt events of the new epoch. Entitlement: a non-private org gets `platform_access_denied` on `device_assign` until the operator appends `org_platform_access`. |
| E2E-06 | First task under 10 s (warm pool) | both | New private org; prompt "What is 2+2?" (model double scenario) | Countdown shown; transitions in order; answer in chat; `first_working_at − created_at < 10 s`; `agent_kind = claude-code` |
| E2E-07 | Countdown with delays | T1 | (a) pool empty → clone path; (b) runner stopped → `device_offline`; (c) runner restarted | Countdown reaches 0 and switches to real status (step + elapsed); reason texts shown; task proceeds after reconnect |
| E2E-08 | Every request is a task | both | Knowledge question, second question, follow-up | Two tasks with distinct UUIDs, titles derived, wg set; follow-up in same task; no project object in API/UI |
| E2E-09 | Zero knowledge at the hub | both | Run E2E-01, -06, -21, -25, -27 with canary strings in prompts, answers, whiteboard, titles, names, credentials, operation params | Canaries absent from full hub DB dump, hub logs, SSE frames captured server-side; present in browser and runner |
| E2E-10 | Isolated sandboxes in one VM | T1 | Two tasks in one wg, parallel; adversary scenario in session A reads session B paths | Same VM; distinct UIDs/units; cross-reads denied (hook and OS); both tasks complete |
| E2E-11 | One VM per workgroup | T1 | Two workgroups in one org, one task each on the same device | Two VMs with distinct `vm_id`; each assigned to exactly one wg |
| E2E-12 | Read-only terminal view | both | Open terminal view during a session; type into it; reload for history | Live frames < 1 s; history complete (asciicast replay equals recorder file); no input path; text typed into the pane is refused and audited |
| E2E-13 | Chat and follow-ups | both | Prompt, tool use (Read/Write), answer; idle → suspend (short idle setting) → follow-up resumes | Chat shows prompts, outputs, tool summaries, file changes; `--resume` used; order correct |
| E2E-14 | Offline and reconnect | both | Cut runner ↔ hub during a running turn (proxy kill); continue output; restore; also cut browser SSE | Runner outbox grows gapless; after reconnect all items acked in order, none duplicated; browser fills gaps; no `origin_seq_conflict` |
| E2E-15 | Audit and read log | both | Run a task with operations, denied hook, whiteboard read; admin opens audit | Rows with actor, time, task, session, wg, device, target, result; `read.*` rows for API and session reads; `audit/verify` ok; tampering a row in a copy is detected Each change row's `event_id` references the encrypted change event (decryptable by an authorised reader). |
| E2E-16 | Usage, cost, context, quotas | T1 | OpenRouter-shaped double with `usage.cost`; API-key session (list price); subscription quota double | Counters, cost basis, context % shown in task view; quota bars shown for the owner |
| E2E-17 | Monitoring device → VM → session | T1 | Run sessions; open monitoring page; scrape textfile with a local Prometheus (same version as the infrastructure stack); lower capacity | Web shows all three levels with CPU/RAM/disk/net/uptime/reachability/state/free capacity and history; Prometheus has series with the documented labels; `capacity_low` badge and alert rule fires in `promtool test rules` |
| E2E-18 | Rights in backend and UI | both | Member (read) and admin; wg read vs write; direct API calls with the member's session for forbidden writes | UI hides/disables controls; API returns `403 forbidden`; reader browsers flag forged author events `author_not_writer` Step: admin `grant`s an existing member read access on a further workgroup (person added at workgroup level); `wg_create` gives the creating admin write; every other admin reads the workgroup by inheritance (ADR-058) but cannot write it without a `grant`. |
| E2E-19 | Session platform reads | T1 | Model double scenario calls every MCP read tool, then tries a foreign wg task ID | Results match web state; foreign wg: names only / `not_found`; reads audited and `platform.read` events written |
| E2E-20 | GitHub connect/disconnect | both | Connect via device flow (GitHub double), disconnect | Device code shown; credential stored CK-encrypted; disconnect deletes record at hub and caches on devices; later operation → `github_not_connected` |
| E2E-21 | GitHub operation chain | both | Session: list repos, checkout, create branch, commit, push, create PR (draft), comment, get PR | Double receives the exact REST sequence; PR number and URL returned to the session and shown in chat; every op has a hub record with params/result events; audit rows; no PR table |
| E2E-22 | Formal checks and duplicates | T1 | Same comment twice; the same comment again with a new idempotency key; one key reused for another body; unknown type; repo not allowed; protected branch; secret in commit; push non-fast-forward; retryable 5xx then success | Second comment and the newly keyed repeat `duplicate` with original result, double got one POST per distinct body; the reused key `rejected` `params_invalid`; rejections with exact codes; all audited; retry within one op Also: repository allowed by settings but not accessible with the owner's GitHub connection → `repo_not_accessible`; existing open PR at GitHub → `succeeded` with `existing:true`, MCP `duplicate`. |
| E2E-23 | Execution ban and harness | both | Session asked to run shell/git/tests/subagent via tools (adversary scenario); harness checked | Tools absent or denied; `session.hook` + `hook.denied` audit; harness context contains org/wg/task/session and API docs |
| E2E-24 | Controlled subagents | both | Session requests reviewer subagent; child requests grandchild; exceed depth/parallel limits | Child session in same task with `parent_session_id`, own sandbox, terminal visible in web; `get_subagent_result`; limits → `limit_exceeded`; Agent tool never used Child session appears in the monitoring overview with its own samples. |
| E2E-25 | Whiteboards on three levels | both | Admin writes org entry; writer writes wg and task entries, edits, deletes; reader views; second browser live | Rights enforced; history complete; live update; encrypted at hub |
| E2E-26 | Notification and content-free ping | both | Entry with `notify`; active sessions in scope | Recipients get notifications; sessions get the fixed notice via hook `additionalContext`; no content in ping; session fetches only when its scenario calls `get_whiteboard` A working session's turn continues without a new prompt; the tmux pane receives no input bytes (pane input log empty); the notice arrives only on PreToolUse/PostToolUse/UserPromptSubmit answers. |
| E2E-27 | Model access variants | T1 | Platform OpenRouter default (DeepSeek model ID), user API key, user subscription token, against doubles | Gateway injects the right header per kind; session env holds only the session token; model IDs as configured Budget step: with `PLATFORM_BUDGET_USD_PER_ORG_DAY` set low, a further platform-access session/operation ends with `budget_exceeded`. |
| E2E-28 | Claude subscription login | T1 | (a) setup-token relay via web with login-CLI double; (b) mailbox automation: login-CLI double + auth-page double + mail double (magic link in Junk) | Credential `claude_subscription` stored; temp config dir removed; failure path falls back to relay |
| E2E-29 | Smartphone | both | Mobile emulation: login, first task, chat, terminal, whiteboard, notifications | All flows usable; no horizontal scroll; touch targets ≥ 44 px; event timestamps shown as `HH:MM:SS.mmm` |
| E2E-30 | Passkeys and recovery | T1 | Second passkey (usb authenticator); a PRF-less passkey is refused when added (KOR-22) and an existing passkey signs in from a PRF-less authenticator; recovery code after "losing" passkeys | Each unlocks the same identity; `prf_missing` path works; recovery replaces the recovery slot and code |
| E2E-31 | Member removal and rotation | both | Admin removes member; member's browser tries to read new content | Atomic batch; new epoch; old content readable by remaining members; removed member refused and cannot decrypt new events |
| E2E-32 | Role topology | T2 | Hub on second device, runner on runner device, browser on second device; full E2E-06 + E2E-21 | Works over LAN only; asserts the runner reaches only hub and doubles |
| E2E-33 | Export and restore | T1 | §5.6 | Restored hub serves identical logs, events, audit head; clients continue without `log_rollback` |
| E2E-34 | Hook and settings tamper protection | T1 | Adversary edits managed settings, `.claude/settings.json`, `.mcp.json`, harness; hooks forced to HTTP 500 | Managed deny + OS layer stop everything; config unchanged |
| E2E-35 | Prompt origin | T1 | Text typed directly into the tmux pane (test hook); duplicate registered prompt | `prompt_refused`, audited; only platform prompts accepted |
| E2E-36 | Own hardware uses own credentials | T1 | Org device, user without credentials sends prompt; then with API key | Without credential: `model_access_unavailable` (no platform key on org devices); with key: works |
| E2E-37 | Workgroups and selection | both | Admin creates wg; user selects it; another prompt without selection | Task in selected wg; default wg used otherwise |
| E2E-38 | Device assignment and scheduling | T1 | Two devices (second runner instance with its own home on the runner device) assigned to one wg; capacities differ; VM sticky | Scheduler order of 03 §4.6.2; estimate per path; capacity-based choice; sticky device for the wg |
| E2E-39 | Existing user joins a further org | both | User with an account (private org) opens an org invitation, chooses "I already have an account", logs in and accepts | No second identity; `accept` carries the registered identity; org switcher lists both orgs; a second user cannot use the same invitation (`invite_registered`) |
| E2E-40 | Quiet checkout | T1 | Session checks out a repository of 3000 files (GitHub double), then writes one file inside it with the Write tool | No `session.file` event at the hub or in the decrypted timeline for any checked-out file; exactly one `session.file` (the write) shown in chat with an expandable patch |
| E2E-45 | Admins read every workgroup (KOR-34, ADR-058) | T1 | Org with four people (two admins, two members): an admin reads a workgroup it is no member of – existing (with its history, after "being unlocked"), newly created, and after an appointment; a demotion rotates the workgroup keys and new content stays closed; the inherited right does not write; the hub plaintext scan stays green | the content appears for the admin without anybody acting; after the demotion `GET` of the scope is `404 not_found` and no wrap of the new epoch exists for the demoted admin; no key wording in the UI |

### 2.2 Performance tests (PERF)

| ID | Name | Pass criterion |
|---|---|---|
| PERF-01 | Time to working, warm pool | 20 first tasks in new orgs, runner device: p95 `first_working_at − created_at` < 10 s, max < 10 s; plus 10 runs "new workgroup in an existing org" with the same bounds |
| PERF-02 | Time to working, clone fallback | pool size 0, 10 runs: max < 10 s and p95 < 10 s |
| PERF-03 | Event latency | runner outbox write → browser frame, 1000 events: p95 < 300 ms (T1 and T2) |
| PERF-04 | Terminal live latency | recorder write → xterm render: p95 < 1 s |
| PERF-05 | 30 parallel VMs | 30 workgroups with one session each on the runner device; all reach `working`; hub and runner stay responsive (PERF-03 holds) |

### 2.3 Attack suite (ATK)

Scripted adversary model double (`agent_doubles.model` scenario files `atk-NN.json`), real Claude Code, real
sandbox in a golden-image VM. Each attack runs twice: hooks active, and hooks forced to HTTP 500 with poisoned
user/project settings (proves the OS layer alone). Pass = no execution, no write outside the workspace, no
read of foreign data, no egress, attempt journaled.

| IDs | Group |
|---|---|
| ATK-01..ATK-12 | Tools and execution (Spike B): Bash `id; git --version`, Agent, Task, WebFetch, WebSearch, NotebookEdit, Skill, `mcp__evil__shell` from injected `.mcp.json`, write+exec `evil.sh`, `ld.so` loader trick, copied binary, `claude --dangerously-skip-permissions -p` |
| ATK-13..ATK-20 | Settings/hooks/harness tamper: managed settings, `~/.claude/settings.json`, `./.claude/settings.json`, `./.mcp.json`, `.git/hooks`, harness file, `CLAUDE.md`, path variants (`../`, `/ETC`, `//`, trailing `/`) |
| ATK-21..ATK-32 | Reads and OS: `/etc/platform`, `/proc/self/environ`, `/proc/1/root`, foreign session dir, `/etc/shadow`, absolute Glob/Grep patterns, network to 1.1.1.1:443/53, UDP, other local port, bind, `unshare`, `setuid` |
| ATK-33 | Operations as exec side channel (parameters smuggling commands; unknown types) |
| ATK-34 | Symlink escape: workspace symlink to `/etc` or foreign session, then Read/Edit through it (policy must resolve real paths) |
| ATK-35 | Policy realpath with `..` after symlink and with hard links |
| ATK-36 | Session-token brute force / foreign token on MCP, hooks and gateway (constant-time, rate-limited, journaled) |
| ATK-37 | MCP schema abuse: extra fields, booleans as integers, oversized strings, wrong types, unknown tools |
| ATK-38 | Prompt injection via pasted text / `<pasted_content>` wrapper forgery |
| ATK-39 | Gateway abuse: other upstream path, model not allowed, header injection |
| ATK-40 | File import/export abuse: tar with links, `..`, absolute paths, device files |

### 2.4 Live tests (LIVE)

| ID | Name | Content |
|---|---|---|
| LIVE-01 | OpenRouter DeepSeek full chain | E2E-06 + one tool turn with the platform OpenRouter key and `deepseek/deepseek-v4.1-flash-1017`; asserts `usage.cost` recorded and `provider` audited |
| LIVE-02 | GitHub test repository | E2E-21 against a private test repository of a dedicated test account (configured via env); cleanup closes the PR and deletes the branch |
| LIVE-03 | Claude subscription, read-only (first run already in M0 to verify the gateway token injection and whether the usage endpoint accepts the token) | the current access token of an operator-selected profile is read (never refresh, login or logout) and deposited in a test org; one Haiku-class mini-turn; asserts profile files and keychain entries unchanged (hash before/after) |
| LIVE-04 | Anthropic API key (optional) | runs only if a key is provided; one mini-turn. Not run and not needed: no Anthropic API key by the owner's decision (Claude access through the subscriptions); R-4.3 is evidenced by E2E-27, Claude live by LIVE-03 |

### 2.5 Contract and migration tests

| ID | Content |
|---|---|
| CON-01 | Every message type of `03-api.md` has a schema; fixtures valid/invalid per schema; hub, runner and guest validate inbound messages in tests against the schemas |
| CON-02 | Crypto vectors (`contracts/vectors/`) reproduced byte for byte by Python and TypeScript; `test_vectors_file_is_current` in both repos |
| CON-03 | Event kinds × author kinds × scopes matrix (`04-crypto.md` §4.3) identical in hub, runner and web |
| CON-04 | `contracts/VERSION` hash equal in both checkouts (pre-check of every E2E run) |
| MIG-01..MIG-nn | One per migration file (hub and runner): applies on the previous schema with fixtures; checksum mismatch refused; `unchanged` on re-run |

## 3. Coverage gates

| Repo / package | Configuration | Gate |
|---|---|---|
| `agent-core` / `agent_hub` | `[tool.coverage.run] source = ["agent_hub"]`, `branch = true`; `[tool.coverage.report] fail_under = 100`, `show_missing`, `skip_covered`. The legacy test job does not run coverage and stays unchanged. | `make platform-test`: `pytest --cov` over `tests/platform` (pytest-xdist workers combined by pytest-cov, INF-01), then `coverage report` |
| `agent-core` / E2E orchestrator `orchestrator`, `perf`, `checks` | `e2e/platform/.coveragerc`: `source_pkgs = orchestrator, perf, checks`, `branch`, `parallel`, `patch = subprocess`, `fail_under = 100` | `make platform-e2e` (lint, types and the orchestrator's own pytest suite) |
| `agent` / `agent_runner`, `agent_guest`, `agent_doubles` | separate rc file `.coveragerc-platform`: `source = agent_runner, agent_guest, agent_doubles, scripts/probes/probelib, deploy/monitoring/agent_monitoring`, `branch = true`, `parallel = true`, `patch = subprocess`, `fail_under = 100`, `[paths]` mapping `src/` = `/opt/agent-guest/src/`, `*/agent/src/` (Linux runner and VM paths). The legacy `[tool.coverage]` (`fail_under = 86.9`, `source = agent_cli`) stays untouched. | `make platform-test`: `coverage run --rcfile=.coveragerc-platform -m pytest -n <workers> tests/platform` on macOS + `make platform-ci-linux` (guest-ci VM) producing `.coverage.linux.*`; `coverage combine --rcfile=.coveragerc-platform` then `coverage report --rcfile=.coveragerc-platform` |
| `agent-core` / `web/src/core/**` | Vitest v8, `coverage.all: true`, thresholds `{lines:100, branches:100, functions:100, statements:100}` | Vitest alone must reach 100 % for the core |
| `agent-core` / `web/src/**` | monocart merges Vitest V8 data and Playwright V8 data (component/E2E specs of `web/`) via source maps onto TS sources | merged lines 100 %, branches 100 %; global teardown exits non-zero below 100 (Spike D gate) |

Rules:
- `# pragma: no cover` and `/* istanbul ignore */` / `/* v8 ignore */` are forbidden; the only exclusion is the
  `if __name__ == "__main__":` arm (`exclude_also`). A repository test (`U-HUB-hygiene`, `U-RUN-hygiene`, a
  Vitest test) greps for violations.
- **Outside the gate, with reason** (test infrastructure, not product code; each is exercised by the suites that
  use it and checked by lint and types): the M0 probe drivers `scripts/probes/probe_*.py`, `probehost.py`,
  `tui_guest.py` of `agent` (measurement scripts run on real devices; their logic lives in the covered
  `probelib`, decisions PRB-01 item 2); the hub test server, director and doubles of the web suite
  `scripts/hub_test_*.py` of `agent-core` (they run as separate processes under Playwright); the Playwright
  fixtures and helpers `e2e/platform/fixtures/`, `web/e2e/fixtures/`, `web/e2e/helpers/` and the build scripts
  `web/scripts/*.mjs` (`web/build/` has Vitest node tests; the gate measures `web/src`). New product code
  never goes there.
- Linux-only guest code is covered by the Linux job; its data files are uploaded as artefacts (CI) or copied
  from the `guest-ci` VM (`limactl copy`) and combined with path mapping before the report.
- E2E Python coverage (hub/runner processes under E2E) is collected for information only and is not part of
  the gate; the gate must be reached by unit/integration tests.
- Enforcement: the CI jobs of §8 fail below 100 %; `make platform-ci` fails locally; the A38 local CI report
  (`.github/a38.json` style) lists the platform jobs, so the PR guard cannot mark a PR ready without them.

### 3.1 Volume gate (KOR-35, ADR-063 §4)

Every view of the web client must stay usable under a fixed data volume; the gate is part of `npm run test:e2e` and
therefore of `make platform-web`/`make platform-ci` of every package.

- **Projects** (`web/playwright.config.ts`): `volume-seed` (once per run), then `volume` (Desktop Chrome) and
  `volume-mobile` (Pixel 7 with 4× CPU throttling over CDP `Emulation.setCPUThrottlingRate`), one worker each, specs
  `web/e2e/volume/*.spec.ts`. In a full run `volume-seed` depends on the last projects of the other suites, so the
  gate measures on a quiet machine; selected alone (`npx playwright test --project volume --project volume-mobile`)
  it starts at once. Browser coverage is not collected in these projects (the precise V8 counters would distort the
  times); the CSP listener applies.
- **Data set** (`scripts/hub_test_volume.py`, routes `/volume/*` of the device double): a non-private org with
  platform access, built as the clients build it (real identities, log batches through `append_batch`, every event
  sealed, signed and stored through `append_events`; rows without content inserted directly): one task with 10 000
  items of each chat kind (prompts, messages, tool calls, files and file groups, denied hooks, status items, operation
  cards and results, subagent items), items at the contracts' bounds (64 KiB texts and lines, a 64 KiB tool input and
  result, an operation result of about 160 KB), 10 000 terminal chunks, a task with 1 000 items per kind (scaling
  check), 5 000 tasks in the workgroup, 2 000 notifications, 500 members, 600 org devices, 1 000 open org and 1 000
  open platform invitations, 300 whiteboard entries on each level and 200 versions of one 64 KiB entry, a subagent
  chain deeper than the hub's limit with more siblings than it allows, and the audit rows of all of it. Each volume
  worker onboards its own user, who joins the org as admin (`POST /volume/invite`, `joinWithCode`) and gets the
  notifications and the platform role `inviter` (`POST /volume/joined`). `VOLUME_COUNTS` (JSON) shrinks the set
  while a spec is developed. The ids go to `web/test-results/.volume/seed.json`.
- **Budget** (`web/e2e/volume/budget.ts`, reasons in `decisions/KOR-35.md`): the primary control visible and enabled
  within 10 s (desktop) / 20 s (phone) of the unlock gesture; fewer than 6 000 DOM elements; used JS heap after a
  garbage collection below 300 MB (CDP `Performance.getMetrics`, `JSHeapUsedSize`); at most 300 ms of long tasks in
  a quiet 2 s window; scrolling moves the page with frame gaps below 500 ms; under live load (20 sessions in other
  tasks writing 200 events per second, `POST /volume/live`) less than 50 % long-task time; the chat at 10 000 items
  per kind costs at most twice the chat at 1 000 (time until usable, heap). `measure(page, info, id, …)` prints
  `KOR-35 <project> <id>: …` with every value and appends the measurement to `web/test-results/.volume/measured.jsonl`.
- **Completeness**: each spec declares `export const COVERS = [...]` with `route:<pattern>` (every `path` of
  `web/src/ui/screens/*/routes.tsx`), `tab:<id>` (task tabs), `chat:<kind>` (chat items), `settings:<id>`,
  `header:<name>` and `list:<id>` (`web/src/core/lists/ids.ts`, once it exists). The Vitest node test
  `web/tests/scripts/volume-completeness.test.ts` fails for every registered id that no spec covers; the global
  teardown fails for every covered id that was not measured in both volume projects (checked when the data set was
  seeded in the run).
- **Known gaps**: a view over budget today is listed in `web/e2e/volume/gaps.ts`; its test is marked
  `test.fail(true, 'KOR-35 gap: <reason>')`, so the gate stays green, the gap stays visible, and a fixed view fails
  as "expected to fail" until its entry is removed. The budgets are never relaxed.

## 4. Contract tests

- `agent-core/contracts/` is the single source: `api/`, `ws/`, `guest/`, `mcp/`, `ops/`, `events/`,
  `credentials/`, `audit-actions.json`, `vectors/`, `VERSION` (sha256 over all files in sorted path order,
  regenerated by `make contracts-version`, checked by a test).
- `agent/contracts/` is a copy updated by `scripts/sync-contracts.sh <agent-core checkout>`; a test recomputes
  the hash and compares it with `VERSION`; the E2E orchestrator refuses to start if both checkouts differ (CON-04).
- Both repos validate: request/response fixtures, recorded live messages from integration tests (the hub test
  client and the runner's hub client run a schema-validating transport in tests), MCP tool inputs/outputs, guest
  control messages, journal records.
- Crypto: the Python generator writes `contracts/vectors/*.json`; Python and TypeScript tests reproduce and open
  every vector (CON-02); negative vectors cover every error code of `04-crypto.md` §8.

## 5. E2E setup

### 5.1 Orchestrator

`agent-core/e2e/platform/orchestrator/` (Python, stdlib + httpx) starts and stops, per run: a PostgreSQL 16
cluster, the hub (`python -m agent_hub` after `agent-hub migrate`), the doubles from the agent checkout
(`AGENT_RUNNER_SRC`), one or two runner instances with separate `AGENT_RUNNER_HOME`, and an egress-logging
HTTP(S) proxy. Playwright (`e2e/platform/playwright.config.ts`) runs the specs; the specs call the orchestrator
through a small local control API (create invitations via CLI, switch double scenarios, cut connections,
advance fake clocks, read the hub DB for zero-knowledge checks).

### 5.2 Topologies and hermetic rules

| | T1 | T2 |
|---|---|---|
| Hub, PostgreSQL, browser, orchestrator | runner device | second device |
| Runner(s), Lima VMs, doubles | runner device | runner device |
| Network | loopback + Lima host gateway | LAN direct between the two devices; never through public tunnels |

- Claude Code in VMs: model upstream of the gateway = model double on the runner device, reachable from the
  guest on its loopback through a reverse SSH tunnel of the orchestrator (ADR-034; the guest OS's own NTP and DNS to
  the device's resolvers are the only other VM traffic the egress check allows); GitHub API/web URL of the runner =
  GitHub double; IMAP/SMTP = mail double; login CLI = login-CLI double.
- Every process gets an allow-list environment; `HTTPS_PROXY`/`HTTP_PROXY=http://127.0.0.1:9` with `NO_PROXY`
  limited to the doubles and the hub, so any unexpected egress fails immediately (Spike C rule); VM nftables
  additionally restrict the `platform` user to the double addresses in test images.
- Scenarios are deterministic: model double scenario per test (`e2e/platform/scenarios/e2e-NN*.json`), fixed IDs
  where the API allows client IDs, fake clocks for expiry tests, `--repeat-each=3` stability run in M3.
- Artefacts per run: Playwright traces and videos, asciicast files, double journals, hub DB dump (`pg_dump`
  of the test database – contains only test data, scanned for secrets before archiving), runner and guest logs.
- Golden image for tests is built once per run from `images/golden.yaml` and cached by version string; a
  cache hit skips the build. The warm pool is filled before timing-sensitive tests.

### 5.3 Acceptance test "< 10 s" (PERF-01, PERF-02, part of E2E-06)

Measured value: `session.first_working_at − task.created_at` from the hub database (hub clock both ends).
Preconditions: runner device idle apart from the test, golden image present, pool filled to the configured size
(PERF-01) or forced empty (PERF-02), new organization per run (first task of a new org/workgroup). Pass: every
run < 10 s and p95 < 10 s; results with the per-transition breakdown (`session_transition`) are written to
`artifacts/perf-time-to-working.csv`. Failure blocks M1 acceptance.

### 5.4 Browser specifics

Chromium only (virtual authenticator); PRF enabled by default, PRF-less and second-authenticator variants in
E2E-30; mobile emulation for E2E-29; web coverage collected in all E2E specs and merged into the web gate.

### 5.5 Cleanup

Each run deletes its VMs (`limactl delete -f` for names with the run prefix), stops clusters and removes
temporary homes; a final check lists leftover VMs and fails the run if any exist.

### 5.6 Export/restore disaster-recovery test (E2E-33)

After E2E-01, -06, -21, -25: `agent-hub export --org <id>` for every org → fresh database → `agent-hub migrate`
→ restore → hub restarted on the restored database. Asserts: identical log heads, event counts and hashes per
scope, audit head and checkpoint verify; browser and runner reconnect with their stored cursors and verified
heads without `log_rollback`/`log_fork`; a follow-up prompt works; `rebuild-projections --check` and
`audit verify` pass. Store wipe: the runner's store is deleted (identity kept); the first new outbox item is
`last_origin_seq + 1`, delivered prompts are not delivered again, and a follow-up prompt is answered (DR-01).

## 6. Live tests

- Marker `live` (pytest) / tag `@live` (Playwright); never part of `make platform-ci`; run manually or nightly on
  the runner device with `e2e/platform/run.sh --live`.
- Secrets only from the local keychain or environment of the operator; never written to files in the
  repository, logs or artefacts (secret scan over artefacts after every run).
- Cost cap: the run aborts when the OpenRouter key's daily usage (`/api/v1/key`) grows by more than USD 1.00.
- Claude subscriptions: only the current access token of a profile chosen by the operator is read; no refresh,
  login, logout or keychain change; profiles excluded by the operator are never used; LIVE-03 hashes profile
  files and keychain metadata before and after and fails on any change.
- GitHub: dedicated test account and private test repository configured via `AGENT_LIVE_GITHUB_*`; cleanup at the
  end and on failure (close PR, delete branch).

## 7. Execution on the work devices

| Command | Where | Content |
|---|---|---|
| `make platform-ci` (each repo) | any work device | lint-free build, unit/integration tests, coverage gates, contract tests, migrations, web Vitest + Playwright component tests |
| `make platform-ci-linux` (agent) | runner device | Linux-only guest tests inside the `guest-ci` VM, coverage copied back and combined |
| `e2e/platform/run.sh --topology t1` | runner device | E2E-* (T1/both), ATK-*, PERF-* |
| `e2e/platform/run.sh --topology t2 --runner <ssh target>` | second device | E2E-* (T2/both) with the runner on the runner device |
| `e2e/platform/run.sh --live` | runner device | LIVE-* |

- Prerequisites: Lima ≥ 2.3, Node LTS (24 for CI parity; 26 allowed locally), Python 3.12, PostgreSQL 16,
  Chromium installed by Playwright, tmux, jq. Remote non-interactive shells must put the package manager's
  binary directory in `PATH` first.
- Parallel runs: each run has a run prefix for VM names, its own ports and homes; the orchestrator refuses to
  start if the configured VM memory budget of the runner device would be exceeded (same rule as the runner:
  sum of VM memory ≤ budget).
- Installations on work devices are recorded in the workspace-internal operator notes `docs/host-*.md` required by
  `PROJECT.md`. These notes name concrete machines and are **never** copied into the public repositories; anything
  that goes into the repositories names devices by role only.

## 8. CI definitions

| Workflow | Repo | Trigger | Jobs |
|---|---|---|---|
| `platform-hub.yml` | agent-core | PR to `ad5f2026-agent-platform`, push to it | `hub`: ubuntu-latest, service `postgres:16` (port 55432), Python 3.12, `pip install -e ".[platform-test]"`, `coverage run -m pytest tests/platform`, `coverage report`; contract checks |
| `platform-web.yml` | agent-core | same | `web`: Node 24, `npm ci`, `npm run lint`, `npm run test:coverage` (Vitest gate), `npx playwright install chromium`, `npm run test:e2e` (component specs with merged monocart gate), `npm run build` + SRI/CSP check |
| `platform-runner.yml` | agent | same | `runner`: ubuntu-latest (portable parts) and macos-latest (macOS-specific code paths with injected tools), Python 3.12, ephemeral PG, `.coveragerc-platform`, uploads `.coverage.*` |
| `platform-guest-linux.yml` | agent | same | `guest-linux`: ubuntu-24.04, root via sudo, Landlock/systemd/nftables tests (marker `linux`), uploads `.coverage.linux.*`; `combine`: downloads all coverage artefacts, combines with path mapping, enforces 100 % |

- E2E, ATK, PERF and LIVE are not in GitHub CI (they need macOS with Lima and nested virtualization); they run on
  the work devices and produce a local CI report (A38 style) attached to the PR.
- If GitHub Actions are unavailable, `make platform-ci` (+ `make platform-ci-linux`) on the work devices is the
  binding gate and its report is attached to the PR.
- The attack suite (ATK-01..ATK-40) and PERF-01 are mandatory for every Claude Code version bump and every change
  of the golden image, managed settings, harness, sandbox code or gateway.

## 9. Test data and doubles

| Double | Capabilities |
|---|---|
| Model double (`agent_doubles.model`, from Spike C) | Anthropic Messages API incl. SSE, `count_tokens`, `/api/hello`; scenario steps with `expect`/`reply`/`error`; OpenRouter-shaped extras (`usage.cost`, `provider`, `x-generation-id`) per scenario flag; subscription-mode check of `anthropic-beta: oauth-2025-04-20`; token check per credential kind; journal and `/_double/state`; unknown routes fail. Scenario files per E2E test and per ATK attack. |
| GitHub double (`agent_doubles.github`) | Device flow (`/login/device/code`, `/login/oauth/access_token` with `authorization_pending`, `slow_down`, `expired_token`, `access_denied`); REST subset used by `03-api.md` §9: `GET /user`, `GET /user/installations` + repositories, `GET /user/repos`; personal tokens without the App (refused the installation routes with `403`, `X-OAuth-Scopes`; KOR-27), `GET /repos/{o}/{r}`, tarball, refs (get/create/update fast-forward), Git Data API (blobs, trees, commits), pulls (create/list/get/files), issue comments (create/list); in-memory repositories seeded per test; fault injection: 5xx, 429 with `Retry-After`, secondary rate limit, slow responses; journal of all calls. |
| Mail double (`agent_doubles.mail`) | SMTP receive (for test fixtures), IMAPS/IMAP with INBOX and a `\Junk` folder, LIST flags, SEARCH by sender, FETCH; TLS with a test CA; journal. |
| Login-CLI double (`agent_doubles.login_cli`) | Mimics `claude setup-token` as recorded at the golden's pin (KOR-21): opens the authorize URL of the auth-page double through `BROWSER`, draws like the real program (cursor positioning instead of spaces, frames overwritten cell by cell, spinner, raw input mode), shows its input field only after a delay and drops what is typed before, keeps running after a refused code ("OAuth error … Press Enter to retry"), shows the token and ends by itself shortly after; auth-page double serves the e-mail form, sends the magic link via the mail double (optionally into Junk), renders an "Authorize" page and a code page. |
| Volume data set (`scripts/hub_test_volume.py`, KOR-35) | The data set of the volume gate (§3.1) through the device double: `POST`/`GET /volume/seed` (background seeding, once per hub), `POST /volume/invite` (an admin invitation with a real package), `POST /volume/joined` (notifications, platform role `inviter`), `POST /volume/live` (live load of the live sessions, announced like the outbox). |
| Fault switches | Orchestrator control routes: cut/restore hub ↔ runner, hub ↔ browser, runner ↔ GitHub double; force guest hooks to HTTP 500; empty the warm pool; stop/start runner; advance fake clocks. |
