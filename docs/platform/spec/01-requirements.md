# 01 Requirements and traceability

> As built: the register [`spec-deviations.md`](../spec-deviations.md) has no entries for this document (DOC-01).

Status: binding. The concept and the client's binding rules are [`KONZEPT.md`](../KONZEPT.md) (German; first
source for everyone working on agent.dfx.swiss). Every requirement of its concept (Grundprinzip, §1–§14 and idea 26;
built after the short version of 21 November 2026) is listed sentence by sentence;
bullets that contain several requirements are split. "Vorschläge" and optional points are decided here and
justified in the cited ADR. Block R-P lists the binding guardrails of the assignment (`PROJECT.md`).

Columns: **ID** | **Ref** (link to the anchor in `KONZEPT.md`, numbered as in its full version: § and bullet/sentence,
`b3` = third bullet, `s2` = second sentence; the short version's §13–§15 are idea 26, §13.N and §14 there) |
**Requirement** | **Decision / interpretation** | **Comp** (HUB, WEB, RUN, GST, SBX, DBL, MON, E2E) |
**Spec** | **Tests** (IDs from `07-testing.md` §2; unit/integration suites as `U-<COMP>-<area>`).

Rules: every row names at least one `E2E-*` test (R-14/R-15/R-P rows name the test that proves the decision).
A requirement is done only when all listed tests pass in the hermetic suite (and, where listed, the live suite).

## R-0 Grundprinzip

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-0.1 | [GP s1](../KONZEPT.md#k-gp) | Invitation-based, encrypted platform for AI-supported tasks. | Realised by R-1 (invitation) and R-5 (E2E encryption). | HUB, WEB, RUN | 00 §1, 04 | E2E-01, E2E-09 |
| R-0.2 | [GP s2](../KONZEPT.md#k-gp) | Hierarchy consists exclusively of Organization → Workgroup → Task. | No other container object exists in schema, API or UI. | HUB, WEB | 02 §2.2, §2.4 | E2E-08, U-HUB-schema |
| R-0.3 | [GP s3](../KONZEPT.md#k-gp) | `agent-core` coordinates and synchronises. | Hub is coordinator (scheduler, operation registry, fan-out) – ADR-002. | HUB | 00 §2, 03 §4.6.2, §6 | E2E-06, E2E-38 |
| R-0.4 | [GP s3](../KONZEPT.md#k-gp) | AI sessions run on assigned execution devices. | Only devices assigned to the workgroup are candidates. | HUB, RUN | 03 §4.6.2 | E2E-06, E2E-38 |
| R-0.5 | [GP s4](../KONZEPT.md#k-gp) | Entry without configuration. | Invite link → passkey → first task; private org, default workgroup and platform device are created/assigned automatically. | WEB, HUB | 04 §5.3, 03 §4.6.1, 06 §3–§4 | E2E-01, E2E-05 |

## R-1 Invitation and identity

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-1.1 | [§1 b1](../KONZEPT.md#k-1-b1) | New users are added only by invitation. | No self-registration endpoint; `register/begin` requires a valid `invite_id` (or an authenticated add-passkey). | HUB, WEB | 03 §4.1, 04 §5.1 | E2E-01 (incl. `register/begin` without invitation refused), E2E-02, U-HUB-passkeys |
| R-1.2 | [§1 b1](../KONZEPT.md#k-1-b1) | New customers are added only by invitation. | "Customers" = external users invited by a platform invitation created by a user with platform role `inviter`; they get a private org. ADR-004. | HUB, WEB | 03 §4.2, 04 §4.8, §5.3 | E2E-01 |
| R-1.3 | [§1 b1](../KONZEPT.md#k-1-b1) | New devices are added only by invitation. | Org device invitations (`invite kind=device`) and platform device invitations (operator CLI). | HUB, RUN | 04 §5.8, §5.9 | E2E-04 (incl. enroll with used/invalid code → `invite_unknown`), E2E-05 |
| R-1.4 | [§1 b2](../KONZEPT.md#k-1-b2) | Invitations are generated as codes and passed on. | 128-bit code, text form `XXXXX-…`, link `/invite#<code>`; hub sees only `invite_id`. | WEB, HUB | 04 §3, §5.7 | E2E-02, E2E-03 |
| R-1.5 | [§1 b3](../KONZEPT.md#k-1-b3) | Users accept the invitation. | `accept` log entry signed by invitee and `sk_inv`. | WEB, HUB | 04 §5.2 | E2E-02 |
| R-1.6 | [§1 b3](../KONZEPT.md#k-1-b3) | Users set up a passkey. | WebAuthn with UV required and PRF: a passkey without PRF output is refused at registration, when added and in a recovery (KOR-22); a recovery code is created on request in the settings. ADR-006, ADR-055. | WEB, HUB | 04 §5.1, 03 §4.1 | E2E-01, E2E-30 |
| R-1.7 | [§1 b3](../KONZEPT.md#k-1-b3) | Users sign in at `agent.dfx.swiss`. | Passkey login certifies a session key (12 h) and unlocks the identity in one gesture; RP ID = host of `AGENT_HUB_PUBLIC_URL` (localhost in tests). | WEB, HUB | 04 §5.4, §4.9 | E2E-01, E2E-30 |
| R-1.8 | [§1 b4](../KONZEPT.md#k-1-b4) | Devices are paired with an invitation code. | `agent-runner enroll --code`. | RUN, HUB | 04 §5.8, 03 §6.1 | E2E-04 |
| R-1.9 | [§1 b4](../KONZEPT.md#k-1-b4) | Devices then authenticate cryptographically with a device-specific key, like SSH, without requiring SSH. | Ed25519 device key, `agent-request-v1` signed requests and signed WebSocket upgrade. | RUN, HUB | 03 §1, §6 | E2E-04, U-HUB-auth |
| R-1.10 | [§1 b5](../KONZEPT.md#k-1-b5) (proposal) | One-time codes. | **Adopted**: every code is single use (L7, hub marks used). ADR-005. | HUB, WEB, RUN | 04 §4.7, §5.7 | E2E-03 |
| R-1.11 | [§1 b5](../KONZEPT.md#k-1-b5) (proposal) | Expiry times. | **Adopted**: users 72 h, devices 24 h default, max 30 d. ADR-005. | HUB, WEB | 03 §12 | E2E-03 |
| R-1.12 | [§1 b5](../KONZEPT.md#k-1-b5) (proposal) | Revocation / unpairing. | **Adopted**: `invite_revoke`; device unpairing via `remove`/`device_unassign` with atomic rotation; platform `device_decertify` (hub refuses the device, `rotation_pending`, admin browser performs `device_unassign` + rotate batch). ADR-005. | HUB, WEB, RUN | 04 §5.10 | E2E-03, E2E-04, E2E-05 (decertification steps) |
| R-1.13 | [§1 b3](../KONZEPT.md#k-1-b3), [§2 b2](../KONZEPT.md#k-2-b2), [§8 b1](../KONZEPT.md#k-8-b1) | An existing user can accept an invitation to a further organization without a new identity. | Decided: invitation screen offers "I already have an account"; `accept` with the registered identity (04 §5.2a); since ADR-060 an admin may also admit an existing account directly (`admit`, 04 §5.13). | WEB, HUB | 04 §5.2a, §5.13, 06 §3.1 | E2E-39, E2E-41 |

## R-2 Onboarding and first task

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-2.1 | [§2 b1](../KONZEPT.md#k-2-b1) | Right after passkey login a first task is proposed (e.g. research or GitHub PR). | Start screen offers suggestion cards ("Research", "GitHub pull request", free prompt); selecting one prefills the composer. | WEB | 06 §4 | E2E-01, E2E-06 |
| R-2.2 | [§2 b2](../KONZEPT.md#k-2-b2) | An organization named in the invitation becomes the default context. | | WEB, HUB | 04 §5.2, 06 §4 | E2E-02 |
| R-2.3 | [§2 b3](../KONZEPT.md#k-2-b3) | Otherwise a private organization is created automatically. | Browser appends `genesis{private:true}`. | WEB, HUB | 04 §5.3 | E2E-01 |
| R-2.4 | [§2 b4](../KONZEPT.md#k-2-b4) | Every organization has workgroups. | Genesis always creates the default workgroup; admins create more (`wg_create`). | HUB, WEB | 04 §4.7 | E2E-37 |
| R-2.5 | [§2 b4](../KONZEPT.md#k-2-b4) | Without explicit selection the default workgroup is used. | Composer preselects the default workgroup; selectable otherwise. | WEB | 06 §4 | E2E-37, E2E-06 |
| R-2.6 | [§2 b5](../KONZEPT.md#k-2-b5) | Every user request, even a simple knowledge question, automatically creates a task. | There is no prompt path without a task: `POST /v1/tasks` for new requests, follow-ups belong to their task. | WEB, HUB | 03 §4.6 | E2E-08 |
| R-2.7 | [§2 b5](../KONZEPT.md#k-2-b5) | The task has a unique ID. | UUIDv7, client-generated, unique at the hub. | WEB, HUB | 02 §2.4 | E2E-08 |
| R-2.8 | [§2 b5](../KONZEPT.md#k-2-b5) | The task has a short description. | Deterministic title from the prompt (encrypted `task.meta`); the session may refine it via `task.set_title`. | WEB, RUN | 06 §4.2, 03 §9 | E2E-08 |
| R-2.9 | [§2 b5](../KONZEPT.md#k-2-b5) | The task is assigned to a workgroup. | `task.wg_id` mandatory. | HUB | 02 §2.4 | E2E-08, E2E-37 |
| R-2.10 | [§2 b6](../KONZEPT.md#k-2-b6) | Hierarchy Organization → Workgroup → Task; no separate project level. | Asserted by schema test (no project table/field) and UI navigation test. | HUB, WEB | 02 §2 | E2E-08, U-HUB-schema |

## R-3 Execution devices and infrastructure

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-3.1 | [§3 b1](../KONZEPT.md#k-3-b1) | AI works on tasks on physical execution devices. | Runner on a macOS machine, sessions in Lima VMs there. | RUN | 00 §1, 05 | E2E-06 |
| R-3.2 | [§3 b2](../KONZEPT.md#k-3-b2) | Devices can be added to an organization. | Org device invitation + enrollment. | HUB, RUN, WEB | 04 §5.8 | E2E-04 |
| R-3.3 | [§3 b2](../KONZEPT.md#k-3-b2) | Devices can be assigned to workgroups. | `device_assign` / `device_unassign` log entries + wraps. | HUB, WEB | 04 §4.7, §5.9 | E2E-38 |
| R-3.4 | [§3 b3](../KONZEPT.md#k-3-b3) | By default DFX provides its own execution servers. | Platform devices certified in the platform log; auto-assigned for private orgs. ADR-023. | HUB, WEB, RUN | 04 §5.9, 03 §4.6.1 | E2E-05 |
| R-3.5 | [§3 b3](../KONZEPT.md#k-3-b3) | Users can optionally attach their own hardware. | Org devices (kind `org`). | RUN, HUB | 04 §5.8 | E2E-04, E2E-36 |
| R-3.6 | [§3 b4](../KONZEPT.md#k-3-b4) | Initially only macOS is supported. | Runner refuses non-darwin hosts at start (`unsupported_os`); guest is Linux inside Lima. | RUN | 05 | E2E-04 (enrollment on the macOS runner device), U-RUN-config (non-darwin refused, R-P exception: a non-macOS host is not available in the E2E topologies), `agent-runner doctor` assertion in E2E-04 |
| R-3.7 | [§3 b5](../KONZEPT.md#k-3-b5) | Execution in Lima VMs: one separate VM per workgroup on an execution device. | VM ↔ (device, wg) 1:1. | RUN, GST | 05, 02 §4 | E2E-11 |
| R-3.8 | [§3 b5](../KONZEPT.md#k-3-b5) | Several tasks of the same workgroup may share the VM. | | RUN | 05 | E2E-10 |
| R-3.9 | [§3 b5](../KONZEPT.md#k-3-b5) | … but must run in mutually isolated AI-session sandboxes. | Per-session UID, systemd unit, Landlock, managed settings. ADR-008. | SBX, GST | 05 §4 | E2E-10, ATK-01..ATK-40 |
| R-3.10 | [§3 b6](../KONZEPT.md#k-3-b6) | First task in a new org/workgroup: VM and session ready in < 10 s. | Measured as `first_working_at − task.created_at` (hub clock). Golden image + warm pool + clone fallback. ADR-009. | RUN, HUB | 03 §4.6.1, 07 §5.3 | E2E-06, PERF-01, PERF-02 ; PERF-01 includes 10 runs "new workgroup in an existing org" |
| R-3.11 | [§3 b7](../KONZEPT.md#k-3-b7) | UI shows a countdown instead of a spinner. | Countdown from `estimate_ms`. | WEB | 06 §4 | E2E-06 |
| R-3.12 | [§3 b7](../KONZEPT.md#k-3-b7) | On delays the actual status stays visible. | Status list from session transitions; real reason (`device_offline`, `capacity_exhausted`, step + elapsed). | WEB, HUB | 06 §4, 02 §2.4.1 | E2E-07, E2E-38 |
| R-3.13 | [§3 b8](../KONZEPT.md#k-3-b8) | Initial server allocation: one DFX server hosts the hub, another hosts DFX cloud sessions. | Expressed as roles "hub device" and "runner device" (no host names); proven by topology T2. Production deployment is out of scope (no deploy). | HUB, RUN, E2E | 00 §1, 07 §5 | E2E-32 |

## R-4 AI runtime and model access

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-4.1 | [§4 b1](../KONZEPT.md#k-4-b1) | Version 1 uses Claude Code as the only agent software. | Pinned version, auto-update off. | SBX, GST | 05 | E2E-06 |
| R-4.2 | [§4 b2](../KONZEPT.md#k-4-b2) | Users can deposit their own Claude subscription credentials. | `claude_subscription` credential (setup token), CK-encrypted. | WEB, RUN | 04 §4.4, 03 §4.8 | E2E-27, E2E-28, LIVE-03 |
| R-4.3 | [§4 b2](../KONZEPT.md#k-4-b2) | … or an Anthropic API key. | `anthropic_api_key` credential; injected by the gateway. | WEB, RUN, GST | 04 §4.4 | E2E-27 (LIVE-04 optional, not run: no key by owner decision) |
| R-4.4 | [§4 b3](../KONZEPT.md#k-4-b3) | Reuse the existing e-mail login automation of `DFXswiss/agent` for Claude authentication. | The legacy automation is IMAP (not SMTP): mailbox polling for the magic link reused; headless browser replaces the human clicks; setup-token relay as fallback. Enabled by default on platform devices, off by default on org devices (deliberate, bot-detection risk). ADR-011. Hermetic only (no live login). | RUN, DBL | 05 | E2E-28 |
| R-4.5 | [§4 b4](../KONZEPT.md#k-4-b4) | Without own credentials and own hardware: DFX servers and DFX model access via OpenRouter. | Platform OpenRouter key only on platform devices. ADR-010. | RUN, GST, HUB | 05, 03 §4.6.2 | E2E-27, E2E-36, LIVE-01 |
| R-4.6 | [§4 b4](../KONZEPT.md#k-4-b4) | Initial default model: DeepSeek. | `deepseek/deepseek-v4.1-flash-1017`. ADR-010. | RUN | 05 | E2E-27, LIVE-01 |
| R-4.7 | [§4 b5](../KONZEPT.md#k-4-b5) | Check compatibility of Claude Code with the chosen DeepSeek model via OpenRouter. | Checked in Spike C (12 models); kept as live test of the full chain. | E2E | 07 §6 | LIVE-01, E2E-27 |
| R-4.8 | [§4 b6](../KONZEPT.md#k-4-b6) | Later support of other agent software is optional. | **Decided**: not in v1; `agent_kind` field and a driver interface keep the door open. ADR-021. | HUB, RUN | 02 §2.4 | E2E-06 (asserts `agent_kind = claude-code`) |

## R-5 Encryption and trust boundaries

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-5.1 | [§5 b1](../KONZEPT.md#k-5-b1) | Confidential data (prompts, answers, whiteboards, credentials) are encrypted client-side before transfer to the hub. | Browser and runner encrypt; list of confidential data in 00 §3. | WEB, RUN | 04 §4.3–§4.5 | E2E-09, CON-03 |
| R-5.2 | [§5 b2](../KONZEPT.md#k-5-b2) | The hub must not see confidential content in plaintext. | Canary scan of the whole hub database and hub logs after full flows. ADR-003. | HUB | 04 §7 | E2E-09 |
| R-5.3 | [§5 b2](../KONZEPT.md#k-5-b2) | The hub manages only necessary metadata and encrypted content. | Exhaustive metadata list 04 §7; schema review test. | HUB | 04 §7, 02 §1 | E2E-09, U-HUB-schema |
| R-5.4 | [§5 b3](../KONZEPT.md#k-5-b3) | Decryption only by authorized users and authorized execution devices. | Wraps only for entitled recipients (W1–W3); removed principals rotated out. | WEB, RUN, HUB | 04 §6.3, §5.10 | E2E-31, E2E-18 |
| R-5.5 | [§5 b4](../KONZEPT.md#k-5-b4) | On a DFX execution server the user trusts that server with plaintext; zero knowledge towards the hub only. | Devices hold WK/OK/CK of their assignments. ADR-007. | RUN | 00 §3, 04 §2 | E2E-05, E2E-09 |
| R-5.6 | [§5 b5](../KONZEPT.md#k-5-b5) | Passkey login does not by itself yield an encryption key; key derivation, sharing and device distribution must be defined. | PRF-based identity bundle, recovery code, scope keys with epochs, HPKE wraps. ADR-006. | WEB, RUN, HUB | 04 §2–§6 | E2E-30, E2E-31, CON-02 |

## R-6 Live synchronisation, transparency, audit

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-6.1 | [§6 b1](../KONZEPT.md#k-6-b1) | All relevant events are stored immediately locally on the execution device in its database. | Guest journal (SQLite) and runner outbox (PostgreSQL) before any network send. | GST, RUN | 02 §4, §5 | E2E-14 |
| R-6.2 | [§6 b1](../KONZEPT.md#k-6-b1) | … and synchronised encrypted to the hub with minimal delay. | Outbox push on every new item; hints over WebSocket. | RUN, HUB | 03 §6 | E2E-14, PERF-03 |
| R-6.3 | [§6 b2](../KONZEPT.md#k-6-b2) | Covered: prompts, answers, tool events, results, status, errors, file changes. | Event kinds `prompt`, `session.output/tool/hook/file/status`, `operation.*`. | GST, RUN, HUB | 03 §7.2, §10 | E2E-13, E2E-21, E2E-40 |
| R-6.4 | [§6 b2](../KONZEPT.md#k-6-b2) | Covered: token usage, costs, context-window utilisation. | Gateway usage records; OpenRouter cost from the stream; context % from model catalog. ADR-025. | GST, RUN, HUB, WEB | 02 §2.8, 03 §6.1 | E2E-16 |
| R-6.5 | [§6 b2](../KONZEPT.md#k-6-b2) | Covered: Claude subscriptions and available quotas, as far as technically readable. | Usage endpoint polled per subscription credential (≥ 900 s); `quota_snapshot`. | RUN, HUB, WEB | 02 §2.8 | E2E-16, LIVE-03 |
| R-6.6 | [§6 b3](../KONZEPT.md#k-6-b3) | `agent.dfx.swiss` shows current state and time-precise events also on smartphones. | Responsive UI, mobile emulation tests; event times shown as absolute local `HH:MM:SS.mmm` (relative only as secondary label, 06 §12.4). | WEB | 06 | E2E-29 |
| R-6.7 | [§6 b4](../KONZEPT.md#k-6-b4) | Every action, change and execution is logged with actor, time, task, session, workgroup, device, target, result and changes. | Hash-chained `audit` table (metadata) + reference to the encrypted event holding the change. | HUB, RUN | 02 §2.9 | E2E-15  (asserts `audit.event_id` links to the encrypted change event) |
| R-6.8 | [§6 b5](../KONZEPT.md#k-6-b5) (proposal) | Append-only event log with replay after connection interruptions. | **Adopted**: append-only events, gapless outbox `origin_seq`, per-scope cursors, resume. ADR-013. | HUB, RUN, WEB | 03 §5, §6, 02 §3 | E2E-14, E2E-33 |
| R-6.9 | [§6 b5](../KONZEPT.md#k-6-b5) | Absolutely delay-free network synchronisation cannot be guaranteed. | Expressed as latency targets: SSE p95 < 300 ms on LAN, terminal live < 1 s. | HUB, WEB | 03 §5 | PERF-03, PERF-04, E2E-14 |
| R-6.10 | [§6 b6](../KONZEPT.md#k-6-b6) | Default view: chat with prompts and results. | | WEB | 06 | E2E-13 |
| R-6.11 | [§6 b7](../KONZEPT.md#k-6-b7) | Additional native Claude Code session view: full live terminal output and history. | asciicast capture, encrypted `session.terminal` events, xterm.js view. ADR-016. | GST, WEB | 05, 06 | E2E-12 |
| R-6.12 | [§6 b7](../KONZEPT.md#k-6-b7) | That view is read-only; prompts are entered only via the default view. | No input path; typed text is refused by the prompt-origin hook. | WEB, GST | 05 §5 | E2E-12, E2E-35 |

## R-7 Monitoring

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-7.1 | [§7 b1](../KONZEPT.md#k-7-b1) | Full resource monitoring on the levels device → Lima VM → AI session. | | RUN, GST, HUB, MON | 08 | E2E-17 |
| R-7.2 | [§7 b2](../KONZEPT.md#k-7-b2) | CPU, RAM, storage, network, runtime, reachability, state and free capacity are visible. | | RUN, HUB, WEB | 08, 02 §2.8 | E2E-17 |
| R-7.3 | [§7 b2](../KONZEPT.md#k-7-b2) | … and historised. | Raw 48 h, 5-min rollups 90 d at the hub; Prometheus retention as in the existing stack. | HUB, MON | 02 §2.8 | E2E-17 |
| R-7.4 | [§7 b3](../KONZEPT.md#k-7-b3) | Reuse the monitoring stack of `DFXswiss/server`, in particular Grafana and proven components. | node_exporter textfile collector, Alloy push, Grafana provisioning files. ADR-018. | MON | 08 | E2E-17 |
| R-7.5 | [§7 b4](../KONZEPT.md#k-7-b4) | Monitoring systems may run separately but must be technologically consistent. | Same exposition format, label and file conventions; patch for the infrastructure repo prepared, not pushed. | MON | 08 | E2E-17 |
| R-7.6 | [§7 b5](../KONZEPT.md#k-7-b5) | Relevant metrics are shown in `agent.dfx.swiss`. | | WEB, HUB | 03 §4.11 | E2E-17 |
| R-7.7 | [§7 b6](../KONZEPT.md#k-7-b6) (optional) | Bottleneck warnings. | **Decided, basic version in v1**: `capacity_low` flag + Grafana alert rule. ADR-018. | HUB, WEB, MON | 03 §4.6.2 | E2E-17 |
| R-7.8 | [§7 b6](../KONZEPT.md#k-7-b6) (optional) | Capacity-based task distribution. | **Decided, basic version in v1**: deterministic scheduler orders by free capacity. ADR-013. | HUB | 03 §4.6.2 | E2E-38 |

## R-8 Permissions

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-8.1 | [§8 b1](../KONZEPT.md#k-8-b1) | Persons can be added at organization level. | Org invitation with role. | WEB, HUB | 04 §5.7 | E2E-02, E2E-18, E2E-45 |
| R-8.2 | [§8 b1](../KONZEPT.md#k-8-b1) | Persons can be added at workgroup level. | `grant` for existing members; invitation with `wgs` for new persons. | WEB, HUB | 04 §4.7 | E2E-18  (step: `grant` of an existing member at workgroup level) |
| R-8.3 | [§8 b2](../KONZEPT.md#k-8-b2) | Organization: member = read, admin = write; default for new members: read. | | HUB, WEB | 04 §4.7 | E2E-02 (default role member = read), E2E-18 |
| R-8.4 | [§8 b3](../KONZEPT.md#k-8-b3) | Workgroup: read or write; in the default workgroup all members have write by default. | Decided: the default-workgroup write right is not reducible in v1 (ADR-024 addendum). | HUB, WEB | 04 §4.7 | E2E-18, E2E-02 |
| R-8.5 | [§8 b4](../KONZEPT.md#k-8-b4) | Access rights enforced technically in the backend. | Hub checks every request; direct API calls in tests. | HUB | 03 §3 | E2E-18, U-HUB-rights, E2E-45 |
| R-8.6 | [§8 b4](../KONZEPT.md#k-8-b4) | … and in the user interface. | Controls hidden/disabled; readers verify author rights. | WEB | 06 | E2E-18 |
| R-8.7 | [§8 b5](../KONZEPT.md#k-8-b5) | Sessions may read and discover via platform APIs everything they are authorised for: members, workgroups, tasks, status, whiteboards. | MCP read tools; rights of the task owner. ADR-024. | GST, RUN | 03 §8.1 | E2E-19 |
| R-8.8 | [§8 b5](../KONZEPT.md#k-8-b5) | No blanket insight into foreign workgroups. | Other workgroups: names only. | GST, RUN | 03 §8.1 | E2E-19 |
| R-8.9 | [§8 b6](../KONZEPT.md#k-8-b6) | Read accesses are logged too. | `read.<resource>` audit rows for API and session reads. | HUB, RUN | 02 §2.9 | E2E-15, E2E-19 |

## R-9 GitHub

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-9.1 | [§9 b1](../KONZEPT.md#k-9-b1) | Users can connect and disconnect their GitHub account. | Device flow on the execution device; disconnect hard-deletes the credential everywhere. | WEB, RUN, HUB | 03 §4.8, §4.10 | E2E-20 |
| R-9.2 | [§9 b2](../KONZEPT.md#k-9-b2) | Authorised repository access. | `github.checkout`, `github.get_pull_request`, `github.list_repositories`. | RUN | 03 §9 | E2E-21, LIVE-02 |
| R-9.3 | [§9 b2](../KONZEPT.md#k-9-b2) | Branches, commits, pushes, pull requests and comments. | Operation types of 03 §9, REST/Git Data API only. | RUN | 03 §9 | E2E-21, LIVE-02 |
| R-9.4 | [§9 b3](../KONZEPT.md#k-9-b3) | Rights limited to the connected account and released repositories. | GitHub App installation or the connected account's access + optional workgroup repository allow-list (ADR-054). | RUN | 03 §9 | E2E-22  (incl. `repo_not_accessible`) |
| R-9.5 | [§9 b3](../KONZEPT.md#k-9-b3) | All actions are audited. | | HUB, RUN | 02 §2.9 | E2E-15, E2E-21 |
| R-9.6 | [§9 b4](../KONZEPT.md#k-9-b4) (recommendation) | Check GitHub App with granular permissions. | **Adopted**: GitHub App with user-to-server tokens. ADR-015. | RUN, DBL | 05 | E2E-20, LIVE-02 |
| R-9.7 | [§9 b5](../KONZEPT.md#k-9-b5) | Claude Code must not run git commands itself; GitHub actions only as controlled platform operations. | No git in the sandbox (no Bash, Landlock exec allow-list); operations executed by RUN on the host. | SBX, RUN | 05 §4, 03 §9 | E2E-23, ATK-02 |

## R-10 Harness, hooks, strict execution ban

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-10.1 | [§10 b1](../KONZEPT.md#k-10-b1) | Every session automatically gets a standardised harness with org, workgroup, task and session context and documented platform APIs. | Fixed rules text + generated context file per session. | GST, SBX | 05 | E2E-23 |
| R-10.2 | [§10 b2](../KONZEPT.md#k-10-b2) | The harness states the prohibitions and the duty to request every action via `agent.dfx.swiss`. | | SBX | 05 | E2E-23 |
| R-10.3 | [§10 b3](../KONZEPT.md#k-10-b3) | Claude Code must not run programs, scripts, shell commands, git commands, tests or builds. | | SBX, GST | 05 §4 | E2E-23, ATK-01..ATK-12 |
| R-10.4 | [§10 b3](../KONZEPT.md#k-10-b3) | Claude Code must not start subagents. | Agent/Task tools denied. | SBX, GST | 05 §4 | E2E-23, ATK-05 |
| R-10.5 | [§10 b4](../KONZEPT.md#k-10-b4) | Manipulating or circumventing hooks and security rules is forbidden. | Managed settings only; settings/harness read-only; edits denied. | SBX, GST | 05 §4, §5 | E2E-34, ATK-13..ATK-20 |
| R-10.6 | [§10 b3 s2](../KONZEPT.md#k-10-b3) | Subagents may be requested via a controlled platform operation and started by the platform. | `subagent.request`. | GST, RUN, HUB | 03 §9 | E2E-24 |
| R-10.7 | [§10 b5](../KONZEPT.md#k-10-b5) | Hooks block forbidden actions before execution. | PreToolUse policy. | GST | 05 §5 | E2E-23, ATK-13 |
| R-10.8 | [§10 b5](../KONZEPT.md#k-10-b5) | The sandbox enforces the bans technically as well. | OS layer alone stops every attack (fault-injected hooks). | SBX | 05 §4 | ATK-21..ATK-32, E2E-34 |
| R-10.9 | [§10 b6](../KONZEPT.md#k-10-b6) | Only explicitly defined, authorised actions; no arbitrary shell execution. | Closed list of operation types; no exec-like type. ADR-012. | GST, RUN | 03 §9 | E2E-22, ATK-33 |
| R-10.10 | [§10 b7](../KONZEPT.md#k-10-b7) | Actions are checked deterministically and formally, not by content. | Schema, allow-lists, size and pattern rules only. | RUN, HUB | 03 §8.3, §9 | E2E-22 |
| R-10.11 | [§10 b7](../KONZEPT.md#k-10-b7) | Execution by controlled platform components. | Runner host executor outside the VM. | RUN | 00 §3.1 | E2E-21 |
| R-10.12 | [§10 b8](../KONZEPT.md#k-10-b8) | Blocked attempts are logged. | `session.hook` events + `hook.denied` audit rows. | GST, RUN, HUB | 02 §2.9 | E2E-23, E2E-15 |

## R-11 Operation management and duplicate protection

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-11.1 | [§11 b1](../KONZEPT.md#k-11-b1) | Every operation requested by a session gets its own database record with unique ID, task/session reference, type, parameters, timestamp, status and result. | Hub `operation` (params/result as encrypted events) + runner `operation` + guest `pending`. | HUB, RUN, GST | 02 §2.5 | E2E-21, E2E-22 |
| R-11.2 | [§11 b2](../KONZEPT.md#k-11-b2) | The platform validates operations formally. | | RUN, GST | 03 §9 | E2E-22 |
| R-11.3 | [§11 b2](../KONZEPT.md#k-11-b2) | … checks permissions. | H2–H4. | HUB | 03 §8.3 | E2E-22, E2E-18 |
| R-11.4 | [§11 b2](../KONZEPT.md#k-11-b2) | … detects obvious duplicates by deterministic rules. | HMAC dedupe tags, windows per type. ADR-012. | RUN, HUB | 04 §4.10, 03 §9 | E2E-22 |
| R-11.5 | [§11 b3](../KONZEPT.md#k-11-b3) | Example: the same comment request twice for the same PR is not published twice. | `github.comment` dedupe on (repo, number, sha256(body)), 24 h. | RUN, HUB | 03 §9 | E2E-22 |
| R-11.6 | [§11 b4](../KONZEPT.md#k-11-b4) (optional) | Idempotency key; retry rules per operation type. | **Adopted**: optional `idempotency_key` that marks the same request and never replaces the dedupe fields (KOR-28); retry and window rules per type. ADR-012. | GST, RUN, HUB | 03 §9 | E2E-22 |
| R-11.7 | [§11 b5](../KONZEPT.md#k-11-b5) | Rejected and deduplicated requests are audited too. | | HUB | 03 §8.3 | E2E-22, E2E-15 |
| R-11.8 | [§11 b6](../KONZEPT.md#k-11-b6) | PR creation: session requests `create_pull_request`; platform checks formally, creates the PR with the connected account, returns ID and URL. | Type name `github.create_pull_request`. | GST, RUN, HUB | 03 §9 | E2E-21, LIVE-02 |
| R-11.9 | [§11 b7](../KONZEPT.md#k-11-b7) | Detailed relational PR data model deliberately deferred. | **Confirmed**: PR data only in encrypted operation results. ADR-014. | HUB | 02 §2.5 | E2E-21 (asserts no PR table) |

## R-12 Whiteboards and notifications

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-12.1 | [§12 b1](../KONZEPT.md#k-12-b1) | One whiteboard each on organization, workgroup and task level. | | WEB, HUB | 03 §4.9 | E2E-25 |
| R-12.2 | [§12 b1](../KONZEPT.md#k-12-b1) | No project level. | | HUB, WEB | 02 §2.6 | E2E-25 |
| R-12.3 | [§12 b2](../KONZEPT.md#k-12-b2) | Users with write rights may edit entries. | Org level: admins; wg/task level: wg writers. | HUB, WEB | 04 §4.3 | E2E-25 |
| R-12.4 | [§12 b2](../KONZEPT.md#k-12-b2) | Users with read rights may view them. | | HUB, WEB | 04 §4.3 | E2E-25 |
| R-12.5 | [§12 b3](../KONZEPT.md#k-12-b3) | Changes are encrypted. | | WEB | 04 §4.3 | E2E-25, E2E-09 |
| R-12.6 | [§12 b3](../KONZEPT.md#k-12-b3) | … synchronised immediately. | SSE fan-out. | HUB, WEB | 03 §5 | E2E-25, PERF-03 |
| R-12.7 | [§12 b3](../KONZEPT.md#k-12-b3) | … logged with full history. | Every edit is an event; history endpoint. | HUB, WEB | 03 §4.9 | E2E-25 |
| R-12.8 | [§12 b4](../KONZEPT.md#k-12-b4) | When creating an entry the author may trigger a notification to the authorised participants of that level. | In-app notifications only. ADR-020. | WEB, HUB | 03 §4.9 | E2E-26 |
| R-12.9 | [§12 b5](../KONZEPT.md#k-12-b5) | Active cloud sessions get only a content-free ping. | `ping` hint → guest notice. | HUB, RUN, GST | 03 §6.2, §8.2, 05 §9 | E2E-26 |
| R-12.10 | [§12 b6](../KONZEPT.md#k-12-b6) | The session decides itself whether to fetch the entry via the platform API; no automatic interruption or content delivery. | Notice attached to the next hook answer; content only via `get_whiteboard`. | GST | 03 §8.2, 05 §9 | E2E-26  (asserts a working session's turn continues without a new prompt and the tmux pane receives no input) |

## R-13 Controlled subagents (idea 26)

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-13.1 | [Idee 26](../KONZEPT.md#k-idee-26) | A running session can request a subagent via the platform (e.g. PR review). | | GST | 03 §9 | E2E-24 |
| R-13.2 | [Idee 26](../KONZEPT.md#k-idee-26) | The request is logged as its own uniquely identified operation and checked deterministically for rights and admissible parameters. | | RUN, HUB | 03 §8.3 | E2E-24 |
| R-13.3 | [Idee 26](../KONZEPT.md#k-idee-26) | Only the platform may start the subagent; Claude Code never directly. | | SBX, RUN | 05 §4 | E2E-24, ATK-05 |
| R-13.4 | [Idee 26](../KONZEPT.md#k-idee-26) | Every approved subagent is a full Claude Code session in its own sandbox with own harness, hooks, real-time sync, audit logging and resource monitoring. | | RUN, GST, SBX | 05 | E2E-24, E2E-17  (asserts the child session appears in the monitoring overview) |
| R-13.5 | [Idee 26](../KONZEPT.md#k-idee-26) | The new session is linked to the parent task and the requesting session. | `parent_session_id`, same `task_id`. | HUB | 02 §2.4 | E2E-24 |
| R-13.6 | [Idee 26](../KONZEPT.md#k-idee-26) | Users can view the complete subagent session at any time in the web UI incl. read-only terminal view and history. | | WEB | 06 | E2E-24 |
| R-13.7 | [Idee 26](../KONZEPT.md#k-idee-26) | Subagents can request further subagents only via the platform. | Depth counted by the hub. | GST, HUB | 03 §9 | E2E-24 |
| R-13.8 | [Idee 26](../KONZEPT.md#k-idee-26) (open) | Limiting parallel or nested subagents is open. | **Decided**: depth ≤ 2, ≤ 3 running per task, ≤ 10 per task. ADR-017. | HUB | 03 §12 | E2E-24 |

## R-14 Open technical decisions (KONZEPT §13)

| ID | Ref | Decision | ADR | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-14.1 | [§13.1](../KONZEPT.md#k-13-1) | Passkey PRF identity bundle, recovery code, scope keys with epochs, HPKE wraps, signed membership log. | ADR-006, ADR-007, ADR-023 | WEB, RUN, HUB | 04 | E2E-30, E2E-31, CON-02 |
| R-14.2 | [§13.2](../KONZEPT.md#k-13-2) | Session isolation in one VM and full execution ban: two independent layers. | ADR-008 | SBX, GST | 05 §4 | ATK-01..ATK-40, E2E-10 |
| R-14.3 | [§13.3](../KONZEPT.md#k-13-3) | VM start < 10 s via golden image + warm pool, clone fallback. | ADR-009 | RUN | 05 | PERF-01, PERF-02 |
| R-14.4 | [§13.4](../KONZEPT.md#k-13-4) | Claude Code + OpenRouter + DeepSeek compatible; costs from the stream; quotas via usage endpoint. | ADR-010, ADR-025 | RUN, GST | 05 | LIVE-01, LIVE-03, E2E-16 |
| R-14.5 | [§13.5](../KONZEPT.md#k-13-5) | API contracts, formal rules, idempotency windows. | ADR-012 | all | 03 | CON-01, E2E-22 |
| R-14.6 | [§13.6](../KONZEPT.md#k-13-6) | Device assignment, resource planning, offline/reconnect sync. | ADR-013 | HUB, RUN | 03 §4.6.2, §6 | E2E-14, E2E-38 |
| R-14.7 | [§13.7](../KONZEPT.md#k-13-7) | PR tables deferred. | ADR-014 | HUB | 02 | E2E-21 |

## R-15 Repositories

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-15.1 | [§14](../KONZEPT.md#k-14) | `DFXswiss/agent-core` hosts hub, web, contracts and E2E. | Integration branch `ad5f2026-agent-platform`. ADR-001. | HUB, WEB, E2E | 00 §5.1, §6 | CON-04, E2E-32 |
| R-15.2 | [§14](../KONZEPT.md#k-14) | `DFXswiss/agent` hosts runner, guest, doubles, images. | Same integration branch name. ADR-001. | RUN, GST, DBL | 00 §5.2 | CON-04, E2E-32 |
| R-15.3 | [§14](../KONZEPT.md#k-14) | `DFXswiss/server` monitoring stack is reused; changes only as prepared patches. | ADR-018. | MON | 08 | E2E-17 |

## R-16 Ideas 27–47, changed sentences and §16 (status)

Built after the full concept version ([E-01](../KONZEPT.md#e-01)); status from the concept audit of 2026-11-23
(gebaut = built, teils = partly, offen = open), updated by the packages since. These rows are not yet in the E2E matrix: the package that builds a
row adds `R-` rows with their tests and replaces the row here.

| ID | Ref | Requirement | Status | As built today | Package |
|---|---|---|---|---|---|
| K-3.5 | [§3 b5](../KONZEPT.md#k-3-b5) | Under high load several VMs per workgroup may run on several devices. | teils | Separate VM per workgroup and device; the scheduler stays on the device with the running workgroup VM even when it is full. | – |
| K-10.3 | [§10 b3](../KONZEPT.md#k-10-b3) | Subagents, tests and browsers are requested only as controlled platform operations. | teils | Subagents only by operation (R-13); tests and browsers not yet; the work VM is free ([E-08](../KONZEPT.md#e-08)). | KOR-29 |
| K-27 | [Idee 27](../KONZEPT.md#k-idee-27) | Temporary test VMs: every test run gets an isolated temporary Lima VM; results, logs and exit codes stored; VM removed even on failure. | offen | Ban part built (no shell in the session); test run as operation missing ([A6](../KONZEPT.md#a-6)). | KOR-29 |
| K-28 | [Idee 28](../KONZEPT.md#k-idee-28) | Temporary browser VMs: isolated Ubuntu VM with a browser controlled through controlled interfaces, traceable and live, removed after use. | offen | – | KOR-29 |
| K-29 | [Idee 29](../KONZEPT.md#k-idee-29) | Explicit DFX support release of a workgroup (read or write), revocable, audited. | offen | Building blocks only (workgroup grant with rotation). | – |
| K-30 | [Idee 30](../KONZEPT.md#k-idee-30) | Encryption by default without UX disadvantages; passkey the only visible release; org admins read all workgroups. | teils | Client-side encryption, no recovery-code step ([E-13](../KONZEPT.md#e-13)); admin read right on all workgroups missing. | – |
| K-31 | [Idee 31](../KONZEPT.md#k-idee-31) | Scale and identity hierarchy: humans, main agents and subagents distinguishable; names and GitHub usernames for main agents. | teils | Main/subagent by depth and parent; agent names and per-agent GitHub mapping missing ([E-02](../KONZEPT.md#e-02)). | KOR-26, KOR-27 |
| K-32 | [Idee 32](../KONZEPT.md#k-idee-32) | Autonomous-first: dashboard prioritises agent status, progress, blockers and required decisions. | teils | Task per request, no questions, task overview ([E-21](../KONZEPT.md#e-21)); status-first dashboard missing. | KOR-32 |
| K-33 | [Idee 33](../KONZEPT.md#k-idee-33) | "Optimise processes" button starts an analysis session over authorised logs and metrics, no automatic changes. | offen | Raw data exist. | – |
| K-34 | [Idee 34](../KONZEPT.md#k-idee-34) | Exactly one delegated main orchestrator per user, passkey release, own key, audit, revocation. | teils | Pairing with a passkey, own key, audit and revocation built by KOR-24 ([ADR-056](../adr/056-delegated-agent-access.md)); the orchestrator running on DFX hardware is open. | KOR-24 |
| K-35 | [Idee 35](../KONZEPT.md#k-idee-35) | Own hardware exclusively for a workgroup or the whole organization; capacity, not data access. | teils | Org device per workgroup, separate VMs; assignment to the whole organization in one step missing. | – |
| K-36 | [Idee 36](../KONZEPT.md#k-idee-36) | Every workgroup member with write access can continue a session; interventions attributed. | teils | Ordered queue and author per prompt built; the session does not learn who speaks. | – |
| K-37 | [Idee 37](../KONZEPT.md#k-idee-37) | GitHub identities per PR and commit (author, committer, signature, co-authors), defaults per org/workgroup. | offen | One GitHub connection per user, operator account ([E-06](../KONZEPT.md#e-06)). | KOR-25 |
| K-38 | [Idee 38](../KONZEPT.md#k-idee-38) | Automatic and manual choice of device and VM, deterministic. | teils | Deterministic scheduler; manual choice and fallback to a second VM missing. | – |
| K-39 | [Idee 39](../KONZEPT.md#k-idee-39) | Session completion and cleanup; logs, results and context kept. | teils | Closing ends the process and frees the sandbox; context not kept resumable. | – |
| K-40 | [Idee 40](../KONZEPT.md#k-idee-40) | Full session resume of the original Claude Code session, also on another VM. | offen | Resume only of a paused session on the same VM. | – |
| K-41 | [Idee 41](../KONZEPT.md#k-idee-41) | PR conflict auto-fix button; updates the PR branch, no automatic merge. | offen | No automatic merge (met); the rest missing ([A2](../KONZEPT.md#a-2)). | – |
| K-42 | [Idee 42](../KONZEPT.md#k-idee-42) | Specialised agent roles with configurable API rights. | offen | Only member/admin/read/write ([A1](../KONZEPT.md#a-1)). | – |
| K-43 | [Idee 43](../KONZEPT.md#k-idee-43) | One main agent per task with fresh context; continuing a task uses its original session. | teils | Own Linux user and `.claude` per task; reading limited to the own workgroup and continuation after completion (K-40) missing. | – |
| K-44 | [Idee 44](../KONZEPT.md#k-idee-44) | Cloud sessions resume on another device after a device failure; temporary VMs are restarted. | offen | – | – |
| K-45 | [Idee 45](../KONZEPT.md#k-idee-45) | No DFX fee with own hardware and own AI access. | teils | Own access is budget-free; holds today only because nothing is charged. | KOR-31 |
| K-46 | [Idee 46](../KONZEPT.md#k-idee-46) | Usage-based billing: tokens and VM seconds, attributable to task, session, workgroup and org, visible. | teils | Tokens measured and attributed; VM time, prices and bookings missing ([E-15](../KONZEPT.md#e-15)–[E-17](../KONZEPT.md#e-17)). | KOR-31 |
| K-47 | [Idee 47](../KONZEPT.md#k-idee-47) | Prepaid balance; without balance no paid resources; controlled stop, resumable. | offen | Daily budget per org with refusal; balance missing ([E-14](../KONZEPT.md#e-14)). | KOR-31 |
| K-16 | [§16](../KONZEPT.md#k-16) | Final concept decisions and technical checks. | teils | No project level, fresh context, bans, idempotency built; resume across devices, runtime metering and balance stop missing. | see above |

## R-E Rules of the client (`KONZEPT.md` part B)

Younger than the concept; where they contradict it, the rule holds. Wording, date and reference in
[`KONZEPT.md`](../KONZEPT.md#teil-b--regeln-und-entscheide-des-auftraggebers); a new rule is entered there first.

| ID | Ref | Rule (short) | Status | Where |
|---|---|---|---|---|
| E-01 | [E-01](../KONZEPT.md#e-01) | Full concept version (47 ideas) is binding. | gebaut | this document, R-16 |
| E-02 | [E-02](../KONZEPT.md#e-02) | "Hundreds of agents" describes use, no number target; no artificial limit, clear message at capacity. | teils | KOR-35 |
| E-03 | [E-03](../KONZEPT.md#e-03) | The user only creates a passkey; their AI agent sets up everything else, incl. subscription token transfer. | gebaut | [ADR-056](../adr/056-delegated-agent-access.md) |
| E-04 | [E-04](../KONZEPT.md#e-04) | An agent without passkey can do everything a human with passkey can. | gebaut | [ADR-056](../adr/056-delegated-agent-access.md) |
| E-05 | [E-05](../KONZEPT.md#e-05) | A missing prerequisite is triggered, not explained. | offen | KOR-25 |
| E-06 | [E-06](../KONZEPT.md#e-06) | GitHub through the operator's account for approved users. | gebaut | [ADR-053](../adr/053-operator-github-account.md), [ADR-054](../adr/054-repository-list-optional.md) |
| E-07 | [E-07](../KONZEPT.md#e-07) | Operator subscriptions for approved organizations. | gebaut | [ADR-052](../adr/052-operator-subscriptions.md) |
| E-08 | [E-08](../KONZEPT.md#e-08) | Work VM on request in which everything is allowed. | offen | KOR-29 |
| E-09 | [E-09](../KONZEPT.md#e-09) | Nothing is monitored inside the work VM. | offen | KOR-29 |
| E-10 | [E-10](../KONZEPT.md#e-10) | Work VM egress never via the site address, fail-closed. | offen | KOR-29 |
| E-11 | [E-11](../KONZEPT.md#e-11) | No unnecessary restrictions; nothing must be released one by one. | teils | [ADR-054](../adr/054-repository-list-optional.md), KOR-29 |
| E-12 | [E-12](../KONZEPT.md#e-12) | Registration only with a PRF-capable passkey. | gebaut | [ADR-055](../adr/055-registration-without-recovery-code-step.md) |
| E-13 | [E-13](../KONZEPT.md#e-13) | No mandatory recovery-code step at registration. | gebaut | [ADR-055](../adr/055-registration-without-recovery-code-step.md) |
| E-14 | [E-14](../KONZEPT.md#e-14) | Billing only from a balance the operator sets; no payment path. | offen | KOR-31 |
| E-15 | [E-15](../KONZEPT.md#e-15) | Prices as a fair market average, in configuration. | offen | KOR-31 |
| E-16 | [E-16](../KONZEPT.md#e-16) | Everything priced in USD. | offen | KOR-31 |
| E-17 | [E-17](../KONZEPT.md#e-17) | Every booking also stores its CHF equivalent from one central daily USD/CHF rate. | offen | KOR-31 |
| E-18 | [E-18](../KONZEPT.md#e-18) | Existing accounts are admitted to an organization directly. | offen | KOR-37 |
| E-19 | [E-19](../KONZEPT.md#e-19) | No single-case fixes: exclude the class of fault. | teils | KOR-35 (first case) |
| E-20 | [E-20](../KONZEPT.md#e-20) | Community area with a group chat of all users. | offen | KOR-26 |
| E-21 | [E-21](../KONZEPT.md#e-21) | Task overview as a menu entry. | gebaut | KOR-32 |
| E-22 | [E-22](../KONZEPT.md#e-22) | One repository (`DFXswiss/agent`). | offen | INF-06 |
| E-23 | [E-23](../KONZEPT.md#e-23) | Concept and rules in one central file in the repository. | gebaut | [KONZEPT.md](../KONZEPT.md), `tests/platform/test_konzept.py` |
| E-24 | [E-24](../KONZEPT.md#e-24) | No access layer in front of the platform; it protects itself. | gebaut | operation |

## R-P Assignment guardrails (`PROJECT.md`)

| ID | Requirement | Comp | Spec | Tests |
|---|---|---|---|---|
| R-P.1 | The concept is implemented completely (all rows of this document). | all | 09 | this matrix; M3 acceptance |
| R-P.2 | New packages in the existing repos, consistent with the code base, reuse of proven building blocks, legacy code untouched. | all | 00 §5, §7 | CON-04, U-HUB-legacy-untouched, U-RUN-legacy-untouched |
| R-P.3 | 100 % line and branch coverage for all new code, technically enforced. | all | 07 §3 | coverage gates in `make platform-ci` and CI |
| R-P.4 | Every requirement has at least one E2E test. | E2E | 01 §coverage check | traceability test `e2e/platform/test_traceability.py` |
| R-P.5 | Hermetic main suite: real Claude Code against the model double, GitHub and mail doubles, passkey via virtual authenticator, real Lima VMs. | E2E, DBL | 07 §5 | E2E-01..E2E-40 (network egress check) |
| R-P.6 | A few live tests (OpenRouter/DeepSeek, GitHub test repository, subscription read-only). | E2E | 07 §6 | LIVE-01..LIVE-04 |
| R-P.7 | Topologies T1 (single device) and T2 (hub on second device, runner on runner device). | E2E | 07 §5 | E2E-32, all E2E marked "both" |
| R-P.8 | Everything built and tested on localhost / work devices; no production deployment, no tunnel changes. | E2E | 07 §7 | E2E-32 (asserts no non-LAN egress) |
| R-P.9 | Public-repo hygiene: no internal host names, IPs or secrets in code, docs, fixtures or commits. | all | 00 | U-HUB-hygiene, U-RUN-hygiene (repository scan test) |
| R-P.10 | Existing Claude subscription profiles are never modified, logged out, re-logged or refreshed; only the current access token may be read. | E2E | 07 §6 | LIVE-03 (asserts profile files unchanged) |
| R-P.11 | Work on integration branches with draft PRs; no merge into `develop`, no force push. | all | 00 §6 | M3 acceptance (branch protection check) |
| R-P.12 | Platform devices and the platform model budget only for entitled orgs (private orgs from a platform invitation, or operator-granted `org_platform_access`). | HUB, WEB | ADR-026, 03 §4.3 | E2E-01 (private org gets platform device), E2E-05 (non-private org refused with `platform_access_denied` until granted) |
| R-P.13 | Platform model budget per org and day (`PLATFORM_BUDGET_USD_PER_ORG_DAY`, `budget_exceeded`). | HUB, RUN | 03 §2, §9, 05 §10 | E2E-27 (budget step) |
| R-P.14 | Usage billing with prepaid credit (concept ideas 45–47): DFX tokens and DFX VM runtime charged by a versioned price list, credit per organization set by the operator, no new paid start and paused sessions while the credit is used up, continuation after a credit; own resources free. | HUB, RUN, WEB | ADR-062, 12-billing.md | E2E-46 |

## Coverage check

| Section | Requirements |
|---|---|
| R-0 | 5 |
| R-1 | 13 |
| R-2 | 10 |
| R-3 | 13 |
| R-4 | 8 |
| R-5 | 6 |
| R-6 | 12 |
| R-7 | 8 |
| R-8 | 9 |
| R-9 | 7 |
| R-10 | 12 |
| R-11 | 9 |
| R-12 | 10 |
| R-13 | 8 |
| R-14 | 7 |
| R-15 | 3 |
| R-P | 13 |
| **Total** | **153** (130 from Grundprinzip and §1–§12 and idea 26); R-16 and R-E are tracked separately (24 rows each) |

Every row of R-0 to R-13 names at least one `E2E-*` test; R-14, R-15 and R-P rows name the test or gate that
proves the decision. Every milestone of `09-milestones.md` lists the R-IDs that must be green at its exit; the
traceability test checks this per milestone. The file `e2e/platform/traceability.json` mirrors this matrix (ID → test IDs); the test
`test_traceability.py` fails if a requirement has no E2E test, if a referenced test ID does not exist in the
E2E suite, or if an E2E test references an unknown requirement ID.
