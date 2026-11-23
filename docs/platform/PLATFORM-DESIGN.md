# Platform design (agent.dfx.swiss)

Status: **final for milestone 3** (DOC-01, 2026-11-22); binding for the platform packages on the integration branch
`ad5f2026-agent-platform` of both repositories (ADR-001). Entry page of the documentation: [README.md](README.md).

This document is the design overview of the platform. The details are in the implementation specification
[`spec/`](spec/00-overview.md), the architecture decisions [`adr/`](adr/README.md), the work-package decisions
[`decisions/`](decisions/README.md) and the as-built register [`spec-deviations.md`](spec-deviations.md), which lists
every place where the implementation refines or deviates from the specification text.

## 1. Purpose and principles

The platform runs Claude Code sessions for teams on execution devices, coordinated by a central hub that **never
sees confidential content** (KONZEPT §5, [ADR-003](adr/003-zero-knowledge-hub.md)):

- **The hub coordinates, it does not read** ([ADR-002](adr/002-hub-as-coordinator.md)): it authenticates, enforces
  rights from a signed membership log, stores ciphertext and the metadata listed in
  [04 §7](spec/04-crypto.md#7-what-the-hub-sees-in-plaintext-exhaustive), schedules devices and fans out changes.
- **Users trust the execution device that serves their workgroup**, not the hub. Browsers and runners encrypt,
  decrypt and verify; keys never leave them unwrapped ([ADR-006](adr/006-passkey-prf-key-management.md),
  [ADR-007](adr/007-devices-receive-org-key.md)).
- **Identity by invitation, passkey and membership log** without a keys file
  ([ADR-004](adr/004-invitation-identity-without-keys-file.md), [ADR-005](adr/005-invitation-code-lifecycle.md),
  registration only with a proof of the code: [ADR-043](adr/043-invitation-proof-at-registration.md));
  platform devices and roles are anchored in an offline platform root key
  ([ADR-023](adr/023-platform-root-key.md), [ADR-026](adr/026-platform-device-entitlement.md)).
- **Sessions are sandboxed twice**: Claude Code managed settings with HTTP hooks, and an OS layer (own UID, systemd
  unit, Landlock, noexec, nftables) inside a per-workgroup Lima VM ([ADR-008](adr/008-session-isolation-in-shared-vm.md),
  [ADR-044](adr/044-session-token-peer-binding.md)).
- **Start below ten seconds** from a warm pool of golden-image clones ([ADR-009](adr/009-vm-start-under-ten-seconds.md),
  [ADR-029](adr/029-m0-measurements-and-live03.md)).
- **Every action is formal and auditable**: operations with formal checks and idempotency
  ([ADR-012](adr/012-operations-contracts-and-idempotency.md), [ADR-041](adr/041-runner-operation-pipeline.md)),
  append-only audit and read log, read-only stored terminal ([ADR-016](adr/016-stored-read-only-terminal.md)).

## 2. Components

| Component | Repository / path | Runs on | Responsibility |
|---|---|---|---|
| Hub | agent-core `src/agent_hub/` | hub device | passkey and device authentication, membership- and platform-log enforcement, rights, ciphertext store, task/session/operation state, duplicate detection on opaque tags, device scheduling, SSE/WebSocket fan-out, audit and read log, serves the web build ([03](spec/03-api.md), [02 §2](spec/02-data-model.md#2-hub-schema-hub)) |
| Web client | agent-core `web/` | browser, phone | all user flows; identity bundle with PRF and recovery code; log verification; encryption, wrapping, rotation; countdown, chat, read-only terminal, whiteboards, monitoring ([06](spec/06-web.md), ADR-019, ADR-020) |
| Contracts | agent-core `contracts/` (hash-checked copy in agent) | both | JSON Schemas of every message, closed enums, crypto vectors ([ADR-027 §2–§3](adr/027-work-package-conventions.md)) |
| Runner | agent `src/agent_runner/` | runner device (macOS, LaunchDaemon) | identity and enrollment, scope keys, outbox/inbox sync, reconcile, VM pool and lifecycle, sessions, prompt delivery, operations (GitHub REST, subagents), credentials and model access, metrics ([05 §1–§3, §6, §10–§14](spec/05-runner.md), ADR-013, ADR-022, ADR-028, ADR-030–ADR-041) |
| Guest agent | agent `src/agent_guest/` (stdlib only) | each Lima VM | session sandboxes, MCP platform API, hook policy, prompt-origin check, model gateway, terminal recording and transcript, journal, control socket ([05 §4–§9](spec/05-runner.md#4-sandbox-two-independent-layers-adr-008)) |
| Golden image | agent `images/` | runner device → Lima | Ubuntu 24.04 VM with pinned Claude Code, Node, guest agent, managed settings, nftables ([05 §3.1](spec/05-runner.md#31-golden-image), ADR-021, ADR-025) |
| Test doubles | agent `src/agent_doubles/` | test machines | model, GitHub, mail, login CLI ([07 §9](spec/07-testing.md#9-test-data-and-doubles), ADR-034) |
| E2E suite | agent-core `e2e/platform/` | test machines | cross-repository scenarios on topologies T1/T2, PERF, LIVE ([07 §5](spec/07-testing.md#5-e2e-setup), ADR-036) |
| Monitoring | agent `deploy/monitoring/` | runner device, monitoring stack | textfile metrics, dashboards, alert rules as a patch for the infrastructure repository ([08](spec/08-monitoring.md), ADR-018, ADR-042) |

## 3. Trust boundaries

| Boundary | Trusted side | Enforced by |
|---|---|---|
| B1 browser ↔ hub | browser | end-to-end encryption, client-side log verification, signed events; the hub can withhold, not read or forge |
| B2 runner ↔ hub | runner | same as B1; the runner verifies the log and every author signature before acting |
| B3 hub ↔ internet | hub | signed requests, passkey ceremonies, rate limits, input limits, strict CSP |
| B4 runner host ↔ VM | runner host | only the host initiates connections (Lima-forwarded socket); the VM never holds device or scope keys |
| B5 guest agent ↔ session | guest agent | managed settings + hooks, and the OS sandbox; sessions reach only `127.0.0.1:7700` with a peer-bound token |
| B6 execution device ↔ outside services | execution device | TLS, scoped credentials (GitHub App, OpenRouter key guardrail), formal operation checks |

Where each check happens: [00 §3.1](spec/00-overview.md#31-where-each-check-happens-binding); threat model and
residual risks: [04 §9](spec/04-crypto.md#9-threat-model).

## 4. Main data flows

1. **Invitation and first login** ([04 §5.1–§5.3](spec/04-crypto.md#5-flows), [06 §3](spec/06-web.md#3-invitation-registration-login-recovery-passkeys)):
   code → passkey with PRF → identity bundle (PRF required; a recovery code on request, ADR-055) → accept → private organization or join.
2. **First task** ([05 §6](spec/05-runner.md#6-session-lifecycle), [06 §4](spec/06-web.md)): the browser encrypts the
   prompt and `task.meta`; the hub creates task and session, picks a device and hints it; the runner verifies, takes a
   pool VM, starts the sandboxed Claude Code and delivers the prompt; transitions stream to the countdown.
3. **Operation** ([03 §8–§9](spec/03-api.md)): session → guest (syntax) → runner (formal checks, dedupe tag) → hub
   (rights, limits, duplicates, record) → runner executes → encrypted result to hub and session.
4. **Whiteboard entry with notification** ([05 §9](spec/05-runner.md#9-pings-adr-020)): encrypted entry → content-free
   notifications and pings → notice in the next hook answer of each active session in scope.
5. **Monitoring** ([08](spec/08-monitoring.md)): runner samples host, VM and session values → outbox `metrics` to
   the hub (browser views) and the textfile for the existing Prometheus/Grafana stack.

## 5. Relation to DESIGN.md

`DESIGN.md` of the `agent` repository stays unchanged on `develop`. **For the platform packages, this document, the
specification and its ADRs replace the following sections of DESIGN.md:**

| DESIGN.md section | Replaced by |
|---|---|
| §20 Refused: hub as a coding control plane | ADR-002 (hub as coordinator), ADR-001 |
| §24 Team record and key identity, in particular §24.2, §24.3, §24.10 | ADR-002, ADR-003, ADR-004, ADR-001 |
| §25 Central operation, in particular §25.1, §25.3, §25.7, §25.9, §25.10, §25.11, §25.15, §25.18, §25.21 | ADR-002, ADR-003, ADR-004, ADR-016, ADR-001 |
| §3 hub-owned lifecycle and leases | ADR-002 |
| §1, §2 (hub role "full replica"), §6, §9, §11, §13 | ADR-003 (zero-knowledge hub) |
| §5, §7 (GitHub OAuth pairing, device token) | ADR-004 (invitation identity without keys file) |
| §17, §19.4, §23 (terminal input frames, ephemeral terminal bytes) | ADR-016 (stored read-only terminal) |
| §19.1 (GitHub through the device's git) | ADR-015 (GitHub App, device flow, REST only); §19.1's "execution on the device" is kept |
| §19.3 (operation checks) | ADR-012 (kept in spirit, formalized) |

The owner decision to build the platform in these repositories despite the refusals of DESIGN.md is recorded in the
project brief; DESIGN.md's change rule (line 5, §24.10, §25.21) is met by documenting the deviation here, on the
integration branches, in the same series of changes that is reviewed as draft pull requests before any human merge.

## 6. Coexistence with the legacy code

| Aspect | Legacy | Platform |
|---|---|---|
| Process | `python -m agent_core` (port 8787) | `python -m agent_hub` (default port 8788), exactly one process per database |
| Env prefix | `AGENT_CORE_*` | `AGENT_HUB_*`, `AGENT_RUNNER_*`, `AGENT_GUEST_*` |
| Database | SQLite + PostgreSQL schema `record` | PostgreSQL database `agent_hub`, schema `hub`; runner: own loopback cluster, schema `runner` |
| URL space | `/api/…`, `/sync/…`, `/auth/…`, `/pair…`, `/health` | `/v1/…`, `/`, `/assets/…`, `/v1/health` |
| Runner home | `~/.local/share/agent` | `~/.local/share/agent-runner` |
| launchd label | `swiss.dfx.agent` (LaunchAgent) | `swiss.dfx.agent-runner` (LaunchDaemon with `UserName`) |
| Tests | `tests/` | `tests/platform/` (own `pytest.ini`, coverage configuration, 100 % gate) |
| CI | existing workflows | `.github/workflows/platform-*.yml` |
| Gates | `pytest` | `make platform-ci` (agent also `make platform-ci-linux`) |

Legacy modules (`agent_core`, `agent_cli`) are not modified; the only read-only imports are listed in
[00 §5](spec/00-overview.md#5-repository-layout).

## 7. Operation

Runbooks: [runbooks/](runbooks/README.md) (platform root key, device certification, golden image release, Claude
Code upgrade gate, runner installation, hub operation, monitoring patch, export/restore, local development and
tests). The cutover to production is planned in [CUTOVER.md](CUTOVER.md); nothing is deployed by this work.

## 8. Known limits and residual risks (state M3)

| Topic | State | Reference |
|---|---|---|
| Web E2EE depends on the delivered JavaScript | strict CSP, SRI on the start page, reproducible build with published hash; lazily loaded chunks carry no SRI yet | 04 §9, 06 §10.2, AUD-01 A-08 (KOR-04) |
| PRF on real passkey providers | automated with the virtual authenticator only; real platforms open | [manual-checks/0.1.0.md](manual-checks/0.1.0.md), ADR-006 |
| Anonymous rate limit per source address | behind a proxy: configure the trusted proxy (KOR-06); behind NAT one bucket per public address | [runbooks/hub-operation.md](runbooks/hub-operation.md) |
| Loss of the platform root key | no rotation; re-keying is a new platform | [runbooks/platform-root-key.md](runbooks/platform-root-key.md) |
| Hub state per process | one hub process per database | [03 §2](spec/03-api.md#2-configuration-of-the-hub-agent_hub_-fail-closed-like-agent_coreconfig) |
| Permanently failing runner inbox consumer blocks its scope | operator procedure | [runbooks/runner-installation.md](runbooks/runner-installation.md), ADR-039 |

The full, audited list of open findings is kept outside the repositories by the acceptance packages (ACC-M3,
ACC-FINAL); result documents of the acceptance, performance and attack-suite packages are in
[`acceptance/`](acceptance/) and the files they name.
