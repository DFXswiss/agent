# agent.dfx.swiss – Implementation specification: 00 Overview

> As built: where the implementation refines or deviates from this document, see the register
> [`spec-deviations.md`](../spec-deviations.md#00-overview) (DOC-01).

Status: binding implementation specification, version 1 (2026-11-21).
Scope: the complete product described in `KONZEPT.md` §1–§13, with every open point of §14 decided
(see `docs/adr/`; numbers of the short concept version, mapped in [`KONZEPT.md`](../KONZEPT.md#teil-a--das-konzept)).
The concept in full (47 ideas) and the client's binding rules, which take precedence over this specification, are
[`docs/platform/KONZEPT.md`](../KONZEPT.md) – read it first. Documents of this specification:

| File | Content |
|---|---|
| `00-overview.md` | this file: architecture, components, trust boundaries, technology, layout, branches, coexistence, glossary |
| `01-requirements.md` | traceability: every requirement with ID, owning component and tests |
| `02-data-model.md` | hub schema, runner store, guest journal, event format, migrations |
| `03-api.md` | hub API, live channels, device sync, guest control protocol, MCP platform API, operation types |
| `04-crypto.md` | key hierarchy, flows, formats, threat model |
| `05-runner.md` | VM management, sandbox, harness, hooks, session lifecycle, terminal recording, model access, subagents, pings |
| `06-web.md` | screens and flows of the web client |
| `07-testing.md` | test strategy, coverage gates, E2E, live tests, CI |
| `08-monitoring.md` | metrics device → VM → session, reuse of the existing monitoring stack, display |
| `09-milestones.md` | milestones and acceptance criteria |
| `14-community.md` | the community: one group chat of all users (data, API, crypto, web; KOR-26, ADR-069) |
| `15-prerequisites.md` | the register of every code a task can wait for or be refused for, with its justification and the step the platform starts (KOR-49, ADR-079) |
| `../adr/NNN-*.md` | one decision per file |

Not part of the specification: `docs/host-*.md` are workspace-internal operator notes required by `PROJECT.md`
(installations on the work devices). They name concrete machines and are **never** copied into the repositories.

Normative words: **MUST**, **MUST NOT**, **SHOULD**, **MAY** as in RFC 2119. Everything not marked as
an example is binding. Where this specification and a spike prototype disagree, this specification
wins. Where it is silent, the implementer follows the existing code base (`agent-core`, `agent`) and
records the decision in a new ADR before merging.

Devices are named by role only: **hub device** (runs the hub), **runner device** (an execution device
that runs Lima VMs), **second device** (a further machine used for two-device tests). No document, test,
fixture or commit names internal host names, IP addresses or secrets.

---

## 1. Architecture in one picture

```
 Browser / phone (web client)                         Execution device (macOS, "runner device")
 ┌──────────────────────────────┐                     ┌──────────────────────────────────────────────────┐
 │ web/ (TS, React)             │                     │ agent-runner (host daemon, Python, launchd)      │
 │  core/: crypto, log verify,  │                     │  - device identity (Ed25519 + X25519)            │
 │   sync, protocol             │  HTTPS (signed)     │  - scope keys, membership-log verification       │
 │  ui/: screens                │◄──── SSE ──────────►│  - outbox (gapless) ⇄ hub, inbox cursors         │
 │ Passkey + PRF → identity     │                     │  - scheduler side: VM pool / clone / restart     │
 └──────────────┬───────────────┘                     │  - operation executor (GitHub REST, subagents)   │
                │ signed requests                     │  - credential vault (decrypt in memory only)     │
                ▼                                     │  - metrics sampler → hub + textfile collector    │
 ┌──────────────────────────────┐   WebSocket(hints)  │  - local PostgreSQL (own cluster)                │
 │ Hub (agent-core: agent_hub)  │◄───────────────────►│       │ HTTP over Lima-forwarded unix socket     │
 │ FastAPI + PostgreSQL         │   HTTPS (signed)    │       ▼                                          │
 │ - ciphertext store, metadata │                     │  Lima VM (one per workgroup and device)          │
 │ - membership log, rights     │                     │   agent-guest (system user `platform`)           │
 │ - tasks/sessions/ops state   │                     │    /mcp  (platform API for sessions)             │
 │ - dedupe on opaque tags      │                     │    /hooks/<Event> (policy, logging)              │
 │ - scheduler (device choice)  │                     │    /model/… (model gateway, credential inject)   │
 │ - audit + read log           │                     │    journal (SQLite, append-only)                 │
 │ - fan-out (LISTEN/NOTIFY)    │                     │    tmux + asciicast recorder                     │
 │ - serves web/ build          │                     │   session sandboxes (one UID + systemd unit +    │
 └──────────────────────────────┘                     │    Landlock each) running Claude Code (TUI)      │
                                                      └──────────────────────────────────────────────────┘
 Outside services: model providers (OpenRouter, Anthropic), GitHub (App, REST), mailbox (IMAP) – reached
 only from the execution device. The hub never talks to them on behalf of content.
```

## 2. Components

| ID | Component | Repo / path | Runs on | Responsibility |
|---|---|---|---|---|
| HUB | Hub | `agent-core`: `src/agent_hub/` | hub device | Authentication (passkey session keys, device keys), membership-log enforcement, rights, ciphertext storage, plaintext metadata (tasks, sessions, operations, metrics), deterministic duplicate detection on opaque tags, device scheduling, live fan-out, audit and read log, serving the web build. Never sees confidential content. |
| WEB | Web client | `agent-core`: `web/` | browser | All user flows; encryption and decryption of user content; identity bundle, PRF, recovery; membership-log verification; key wrapping and rotation; countdown and live views. |
| RUN | Runner (host daemon) | `agent`: `src/agent_runner/` | runner device (macOS) | Device enrollment and identity, scope keys, outbox/inbox sync with resume, VM lifecycle (golden image, pool, clone, stop, delete), session start/stop, prompt delivery, operation validation and execution (GitHub, subagents, task title), credential handling, model-gateway credential provisioning, metrics. |
| GST | Guest agent | `agent`: `src/agent_guest/` (stdlib only) | inside each Lima VM | Session sandboxes, MCP platform API, HTTP hooks with deterministic policy, model gateway, prompt-origin check, transcript and terminal capture, append-only journal, control endpoint for RUN. |
| SBX | Sandbox profile | `agent`: `images/` + `src/agent_guest/sandbox/` | inside each Lima VM | Golden image definition, managed Claude Code settings, harness text, Landlock launcher, systemd unit properties, nftables rules, mounts. |
| DBL | Test doubles | `agent`: `src/agent_doubles/` (stdlib only) | test machines | Model double (Anthropic Messages API), GitHub double (REST + device flow + Git Data API), mail double (SMTP in, IMAP out), login-CLI double. |
| E2E | End-to-end suite | `agent-core`: `e2e/platform/` | test machines | Orchestrates hub, runner, VMs, doubles and Playwright across topologies T1 and T2. |
| MON | Monitoring assets | `agent`: `deploy/monitoring/` | runner device / monitoring hub | Textfile collector output of RUN, launchd plist, Grafana dashboard JSON and alert rules prepared as patch for the infrastructure repository. |
| CON | Contracts | `agent-core`: `contracts/` (copied to `agent`: `contracts/`) | both | JSON Schemas of every message in `03-api.md`, crypto test vectors, golden fixtures. Single source of truth for cross-component formats. |

## 3. Trust boundaries

| Boundary | Trusted side | Untrusted side | Enforced by |
|---|---|---|---|
| B1 Browser ↔ Hub | browser (after passkey + identity unlock) | hub | E2E encryption (`04-crypto.md`), client-side membership-log verification, signed events. The hub can withhold or delay data but cannot read, forge or inject. |
| B2 Runner ↔ Hub | runner (device keys) | hub | same as B1; runner verifies the membership log and every author signature before acting on an event (prompt, setting, assignment). |
| B3 Hub ↔ Internet | hub | everybody | signed requests, passkey ceremonies, rate limits, input limits, strict CSP. |
| B4 Runner host ↔ VM | runner host | VM (guest agent and sessions) | Only the host initiates connections (Lima-forwarded unix socket); the guest never holds device keys or scope keys; it receives exactly the plaintext of its own workgroup that it needs. |
| B5 Guest agent ↔ session sandbox | guest agent (`platform` user) | Claude Code session | Two independent layers (`05-runner.md` §4): Claude Code managed settings + HTTP hooks, and OS sandbox (UID, systemd, Landlock, noexec, nftables). Sessions reach only `127.0.0.1:7700`. |
| B6 Execution device ↔ outside services | execution device | model providers, GitHub, mailbox | TLS, per-credential scoping (GitHub App, OpenRouter guardrail), formal operation checks. |
| B7 Agent tool ↔ Hub (ADR-056, `11-agent-access.md`) | agent tool `agent-platform` on the user's computer: a trusted client of the user (holds the user's identity, decrypts, verifies like a browser) | hub | as B1: the tool verifies logs, wraps and the platform log itself and pins the hub key at pairing (`11-agent-access.md` §8.3); the identity reaches it only sealed with a secret the hub never sees. The user trusts the agent like an unlocked browser of their own: it can do everything the user can. |

KONZEPT §5 trust statement made concrete: the user trusts the **execution device** that serves their
workgroup with plaintext; the user does **not** trust the hub with any confidential content. Confidential
= prompts, answers, tool inputs/outputs, terminal bytes, file changes, whiteboard content, task titles and
descriptions, names of organizations/workgroups/members/devices, operation parameters and results,
credentials. Hub-visible metadata is listed exhaustively in `04-crypto.md` §7.

### 3.1 Where each check happens (binding)

| Check | HUB | RUN | GST | WEB |
|---|---|---|---|---|
| Authentication of browsers (passkey, session key) and devices (Ed25519) | ✔ | – | – | – |
| Authentication of sessions (session token) | – | – | ✔ | – |
| Rights (org role, wg access, device assignment) from the membership log | ✔ enforcing | ✔ verifying (refuses to act on events from non-writers) | – | ✔ verifying (UI and refusal on read) |
| Event header schema, author signature, kind allowed for author kind, epoch current, size | ✔ | ✔ | – | ✔ |
| Tool policy (PreToolUse), prompt origin (UserPromptSubmit) | – | – | ✔ | – |
| MCP argument schema (syntactic) | – | ✔ (again) | ✔ | – |
| Operation formal checks on plaintext parameters (repo allow-list, branch rules, paths, sizes, secret patterns) | – | ✔ | – | – |
| Operation rights (actor is writer, type allowed, limits such as subagent depth/count) | ✔ | ✔ (pre-check) | – | – |
| Duplicate detection | ✔ on opaque dedupe tag (authoritative, cross-device) | computes tag | – | – |
| Execution of GitHub operations with the user's token | – | ✔ (host, REST only) | – | – |
| Start of subagent sessions | decides (limits) | ✔ starts | ✔ creates sandbox | – |
| Encryption / decryption | – | ✔ | – | ✔ |

## 4. Technology decisions (with reasons)

| Topic | Decision | Reason |
|---|---|---|
| Hub language/framework | Python ≥ 3.12, FastAPI, uvicorn, psycopg 3 (+ `psycopg_pool`), `cryptography` ≥ 50 | Same stack as `agent-core`; reuse of `agent_core.signing`, `agent_core.passkey`, record migration patterns. 3.12 is the CI version of both repos and the guest OS version. |
| Hub database | PostgreSQL ≥ 16, one database, schema `hub`, owner/writer roles, append-only triggers | Same pattern as the Team Record (`record/migrate.py`); drops the SQLite part of the legacy hub; enables multiple hub processes via `LISTEN/NOTIFY`. |
| Hub live channels | Browser: server-sent events read with `fetch` streaming (signed request headers). Device: WebSocket carrying **hints only**; all data moves by signed HTTP pull/push | Same transport split as the legacy hub; "push is a hint, pull is the truth" makes reconnect trivial (`03-api.md` §6). |
| Web | TypeScript (strict), Vite, React 18, Tailwind 3, `@xterm/xterm` for the terminal view; crypto/protocol core framework-free in `web/src/core/` | Spike D recommendation; consistent with the DFX `app` repo; Vitest + Playwright give a merged 100 % gate. |
| Web crypto | WebCrypto only (X25519, Ed25519, HKDF, AES-GCM, P-256); no JS crypto dependency | Proven byte-identical with pyca `cryptography` in Spike D. |
| Runner | Python ≥ 3.12, asyncio, httpx, websockets, psycopg 3, `cryptography`; LaunchDaemon on macOS | Same stack as `agent`; reuse of `pg.py` patterns, key file handling, mailbox polling, usage parsing. |
| Runner store | Own local PostgreSQL cluster under `$AGENT_RUNNER_HOME/pg` | Same as `agent`; separate from any existing `agent` installation. |
| Guest agent | Python 3.12 **standard library only** (`http.server`, `sqlite3`, `ctypes`); installed into the golden image | Boot speed, no package manager in the hot path, small audit surface; Spike B prototype is stdlib. |
| VMs | Lima ≥ 2.3, `vmType: vz`, Ubuntu 24.04 arm64, golden image + warm pool | Spike A: 0.1 s pool assignment, ≤ 6.0 s clone fallback. |
| Session sandbox | per-session UID, transient systemd unit with hardening, Landlock launcher (Python, stdlib `ctypes`), noexec mounts, nftables per UID, Claude Code managed settings + `--restricted` + HTTP hooks | Spike B: each layer alone stopped all 27 scripted and 42 model-driven attacks. |
| Agent software | Claude Code, pinned version (baseline 2.1.330), interactive TUI in tmux | KONZEPT §4/§6 (native terminal view); auto-update disabled; upgrades gated by the attack suite. |
| Model access | OpenRouter default `deepseek/deepseek-v4.1-flash-1017`; Anthropic API key; Claude subscription token; all through the guest model gateway | Spike C compatibility matrix; credentials never enter the sandbox. |
| GitHub | GitHub App, user-to-server tokens obtained by device flow on the execution device; REST and Git Data API only; no `git` binary | Least privilege, no token at the hub, no execution of untrusted repository content. |
| Monitoring | Prometheus text format via node_exporter textfile collector on the runner device + samples to the hub for the web UI; Grafana dashboards and alert rules as files | Reuses the infrastructure repository conventions (Alloy push, textfile, provisioning). |
| Tests | pytest + coverage (branch) 100 %, Vitest + Playwright + monocart merged 100 %, real PostgreSQL, real Lima, real Claude Code against doubles | `PROJECT.md`; patterns from both repos and Spikes C/D. |

## 5. Repository layout

**Amendment (INF-06, [ADR-061](../adr/061-one-repository.md)):** the two repositories are one, `DFXswiss/agent`;
agent-core was merged into it with its history and **every path below stays as it is** (5.1 and 5.2 now describe the
parts by their former repository). There is one `contracts/` (no copy, no synchronisation) and one `docs/platform/`.
The import rule below ("within the same repository") reads: the hub may import the listed `agent_core` modules, the
runner the listed `agent_cli` modules, and the hub, the runner (with the agent tool), the guest and the doubles do not
import each other (`tests/platform/test_import_boundaries.py`). One distribution `dfx-agent` with the extras
`platform` (runner, agent tool) and `hub` (hub image) replaces the two (ADR-061 §2).

### 5.1 `agent-core` (hub, web, contracts, E2E)

```
src/agent_hub/                 new hub package (no import from agent_core.app, db, teams, tokens, github)
  __init__.py  __main__.py     `python -m agent_hub` / console script `agent-hub`
  config.py                    fail-closed env loading (prefix AGENT_HUB_)
  app.py                       FastAPI factory, middleware (signature gate, CSP, limits)
  auth/                        passkey ceremonies, session certificates, device auth, request gate
  crypto/                      reference crypto implementation (used by the vector generator and for verification)
  log/                         membership-log + platform-log verification and projection
  events/                      event validation, storage, cursors, fan-out
  tasks/ sessions/ operations/ scheduler/ whiteboards/ notifications/ credentials/ requests/
  monitoring/                  metric ingestion, rollups, queries
  audit/                       append-only audit + read log, checkpoints
  stream/                      SSE (browser) and WebSocket (device) endpoints
  db/                          migrations (SQL files), pool, migrate CLI
  static/                      built web assets (generated by `npm run build`, git-ignored except .keep)
web/                           Vite project (package.json, src/core, src/ui, tests, e2e unit)
contracts/                     JSON Schemas, enums/ (closed lists), vectors/, tools/make_vectors.py, VERSION
e2e/platform/                  cross-repo E2E: orchestrator (Python), Playwright specs (TS)
docs/platform/                 README.md (entry), PLATFORM-DESIGN.md, spec/ + adr/ (same in both repos), decisions/,
                               acceptance/, runbooks/, manual-checks/, spec-deviations.md
tests/platform/                pytest suite for agent_hub (separate from legacy tests/)
```

### 5.2 `agent` (runner, guest, doubles, images)

```
src/agent_runner/              host daemon and CLI `agent-runner`
  config.py identity.py keys.py store/ (PostgreSQL) sync/ (outbox, inbox, ws) crypto/ (mirror of web core)
  vm/ (lima, golden, pool) guestlink/ (control client) sessions/ operations/ github/ credentials/
  model/ (credential selection, quota polling) login/ (setup-token relay, mailbox automation)
  metrics/ service/ (launchd) cli.py
src/agent_guest/               in-VM agent, stdlib only
  server.py (127.0.0.1:7700: /mcp /hooks /model) control.py (unix socket API) journal.py policy.py
  mcp.py gateway.py sessions.py sandbox/ (landlock.py, unit.py) recorder.py transcript.py
src/agent_doubles/             model/, github/, mail/, login_cli/ (stdlib only)
images/                        golden.yaml, provision scripts, managed-settings template, harness text, nftables
deploy/monitoring/             dashboards, alert rules, launchd plist template for the textfile writer
contracts/                     copy of agent-core/contracts (checked by hash, see 07-testing.md)
tests/platform/                pytest suites for agent_runner, agent_guest, agent_doubles
docs/platform/                 same layout as in agent-core (spec/ and adr/ identical); runner decisions and acceptance
```

Rules for both repos:
- New code MUST NOT modify legacy modules. It MAY import the following legacy modules read-only, **within the
  same repository** (`agent` cannot import `agent_core` and vice versa). In `agent-core`: `agent_core.signing` (pure helpers: `request_text`, `response_text`, `b64url_encode/decode`, `parse_time`,
  `format_time`, `sha256_hex`, `ed25519_public_key_valid`, `p256_public_key_valid`, `HubSigner`),
  `agent_core.passkey.{errors,encoding,webauthn,certificate,relying_party}`,
  `agent_core.record.{connection,checkpoint}` and the helpers `event.record_hash`, `event.next_received_at`;
  `HubSigner` requires a key ID matching `[a-z0-9-]`. In `agent`: `agent_cli.pg` (cluster management),
  `agent_cli.keys.request_text`/`response_text` (the client-side `agent-request-v1` texts) and
  `agent_cli.account_login.newest_magic_link` (IMAP magic-link search; it takes a `Mailbox` value that the runner
  builds from the decrypted `mailbox` credential – `load_mailbox` is not used). The usage parsers of
  `agent_cli.usage` (`windows_snapshot`, `spend_snapshot`) are **copied and adapted** (they depend on
  `drivers.json` specs and the legacy store); the copy maps the windows explicitly: `five_hour` → `five_hour`,
  `weekly`/`seven_day` → `seven_day`, per-model weekly → `weekly:<model>`. `_parse_login_credentials` is not
  used. Any other reuse is by copying into the new package (and then counts as new code under the 100 % gate).
- If a legacy function needed by the new code is not importable without side effects, it is copied and the
  copy names its origin in the module docstring.

## 6. Integration branches and work-package branches

**Amendment (INF-06, ADR-061):** one repository, integration branch `ad5f2026-live` of `DFXswiss/agent`
(PROJECT.md decision 12); the table below is history.

| Repo | Integration branch | Base |
|---|---|---|
| `agent-core` | `ad5f2026-agent-platform` | `develop` |
| `agent` | `ad5f2026-agent-platform` | `develop` |

- **Superseded by ADR-027 §1:** work packages branch off the integration branch as `ad5f2026-<package id>` and land
  by a serial fast-forward merge (`tools/wp/merge.sh`) without per-package PRs; the milestone acceptance packages
  open one draft PR per repository from the integration branch to `develop`. Original rule: work packages branch
  off the integration branch as `<8-hex id>-<slug>` (repo convention) and open draft PRs **against the integration
  branch**. PR titles: `<8-hex id> - <sentence>`; PR text `EN:` summary of at
  most four sentences (agent repo additionally `DE:`), "Reused/Added" list with file:line (agent `REVIEW.md`).
- Integration branches are merged back into `develop` only by a human at cutover (not part of this work).
- `develop` is merged into the integration branch at the start of each milestone (no rebase, no force push).
- Commits: one English sentence ending with a period, signed.

## 7. Coexistence with the legacy code

| Aspect | Legacy | New |
|---|---|---|
| Process | `python -m agent_core` (port 8787) | `python -m agent_hub` (default port 8788) |
| Env prefix | `AGENT_CORE_*` | `AGENT_HUB_*` (hub), `AGENT_RUNNER_*` (runner), `AGENT_GUEST_*` (guest) |
| Database | SQLite + PostgreSQL schema `record` | PostgreSQL database `agent_hub`, schema `hub`; never shares a database with the record |
| URL space | `/api/…`, `/sync/…`, `/auth/…`, `/pair…`, `/health` | `/v1/…` for API and streams, `/` and `/assets/…` for the web client, `/v1/health` |
| Runner home | `~/.local/share/agent` | `~/.local/share/agent-runner` (`AGENT_RUNNER_HOME`) |
| launchd label | `swiss.dfx.agent` (LaunchAgent) | `swiss.dfx.agent-runner` (LaunchDaemon with `UserName`) |
| Tests | `tests/` | `tests/platform/`, separate coverage configuration (`07-testing.md` §3) |
| CI | existing workflows unchanged | new workflow files `platform-*.yml` |
| Docs | `DESIGN.md`, `*-PROTOCOL.md` unchanged | `docs/platform/README.md` is the entry; `docs/platform/PLATFORM-DESIGN.md` states that, for the platform packages, this specification and its ADRs replace DESIGN.md §20, §24, §25 and the other sections listed in ADR-001 |

Cutover (not part of this work, documented in `09-milestones.md` M4 as a plan only): new hub behind the
production name, legacy hub retired, legacy runner untouched on developer machines. No data migration:
the new platform starts empty; the legacy Team Record remains readable from its own database.

## 8. Main flows (summary; details in the referenced documents)

1. **Invitation and first login** (`04-crypto.md` §5.1–5.3, `06-web.md` §3): code → passkey with PRF →
   identity bundle (PRF required; recovery code on request, ADR-055) → accept → private organization (genesis) with default workgroup, or join
   the organization named in the invitation.
2. **First task** (`03-api.md` §4.6, `05-runner.md` §6, `06-web.md` §4): prompt → browser encrypts, ensures a
   device is assigned to the workgroup (auto-assigns a platform device for private organizations) → hub
   creates task + session, picks the device, sends a hint → runner assigns a pool VM, starts the sandboxed
   Claude Code, delivers the prompt → status transitions stream to the countdown → answers stream to chat.
   Target: `session.working` ≤ 10 s after the hub accepted the task.
3. **Operation** (`03-api.md` §8–9): session calls `request_operation` → guest checks syntax → runner checks
   formally, computes dedupe tag → hub checks rights/limits/duplicates and records → runner executes →
   result encrypted to hub and back to the session.
4. **Whiteboard entry with notification** (`03-api.md` §4.9, `05-runner.md` §9): browser encrypts entry →
   hub stores, creates content-free notifications → runner receives a content-free ping → guest attaches a
   content-free notice to the next hook answer of each active session in scope.

## 9. Glossary

| Term | Meaning |
|---|---|
| Organization (org) | Top level of the hierarchy. Has members (admin or member), workgroups, devices, an org whiteboard and an org key. |
| Private organization | Organization created automatically for a user whose invitation names no organization. |
| Workgroup (wg) | Second level. Has members with read or write access, assigned devices, a wg whiteboard and a wg key. Every org has exactly one **default workgroup**, in which every org member has write access. |
| Task | Third level. Created for every user request; has an ID, an encrypted short description (title), an owner, a state and one main session plus subagent sessions. No project level exists. |
| Session | One Claude Code process in one sandbox in the workgroup VM on one device. Main session of a task or subagent session. |
| Subagent session | Session started by the platform after an accepted `subagent.request` operation; linked to the same task and to the requesting (parent) session. |
| Hub | `agent_hub`, central coordinator. Zero knowledge of confidential content. |
| Runner / runner device | Execution device with `agent-runner`. **Platform device**: provided by DFX and certified in the platform log. **Org device**: owned by an organization, enrolled with an org device invitation. |
| Guest agent | `agent-guest` inside a VM. |
| Golden image | Stopped, versioned Lima VM used as clone source. |
| Warm pool | Running neutral clones of the golden image without workgroup data. |
| Membership log | Per-organization signed, hash-chained, append-only log of membership, rights, device assignments and key epochs. |
| Platform log | Platform-wide signed log of operator registrations, platform device certifications and platform roles. |
| Scope / scope key | Encryption domain: `org` (org key OK), `wg` (workgroup key WK), `cred` (per-user credential key CK; per organization for company GitHub accounts, readable by its devices only, ADR-078). Keys have epochs. |
| Wrap | HPKE encryption of a scope key for one recipient (user identity or device). |
| Identity bundle | A user's private X25519 and Ed25519 keys, stored at the hub only encrypted (per passkey slot and recovery slot). |
| Recovery code | 128-bit code, shown once when it is made (on request in the settings, after a recovery; ADR-055); unlocks the identity bundle without a passkey. |
| Invitation code | 128-bit single-use code; the hub only sees a derived invitation ID. |
| Event | Encrypted, signed record in one scope; ordered by the hub per scope (`scope_seq`). |
| Outbox | Runner-side queue of events with gapless `origin_seq`, pushed with resume after interruption. |
| Operation | A platform action requested by a session (`03-api.md` §9). Every request gets a hub record. |
| Dedupe tag | HMAC over the canonical deduplication fields of an operation, keyed with a workgroup-key derivative; lets the hub detect duplicates without seeing parameters. |
| Idempotency key | Optional caller-provided key that marks a repetition as the same request; it never replaces the derived dedupe fields of a type that has them (KOR-28). |
| Device request | User-initiated request routed by the hub to one device (e.g. GitHub connect, Claude login), encrypted end to end. |
| Ping | Content-free notice that something new is available. |
| Harness | Fixed rules text plus generated context given to every session. |
| Hook | Claude Code HTTP hook handled by the guest agent. |
| Model gateway | Guest endpoint `/model/…` that injects the provider credential and records usage. |
| T1 / T2 | Test topologies: everything on the runner device / hub on the second device and runner on the runner device. |
