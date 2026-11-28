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
| R-0.3 | [GP s3](../KONZEPT.md#k-gp) | `agent-core` coordinates and synchronises. | Hub is coordinator (scheduler, operation registry, fan-out) – ADR-002. | HUB | 00 §2, 03 §4.6.2, §6 | E2E-06, E2E-38, E2E-57 |
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
| R-1.14 | [E-27](../KONZEPT.md#e-27), [E-04](../KONZEPT.md#e-04) | An AI agent uses the platform completely without a person: it creates an account through an invitation itself, signs in with its own key and can do everything a person can; it takes the account to another computer and replaces its main orchestrator without a person. | `register --own-account` → `POST /v1/agent-accounts` with named signature proof (`agent-signature-v1`) instead of WebAuthn; a recovery code at registration; `recover` (`recovery/finish` with `agent`); `agents confirm` (`POST /v1/me/agents` by the agent of an account without a passkey); the table "Without a person" of `agent-parity.md`. The person's passkey approval stays for accounts of persons (idea 34). [ADR-071](../adr/071-agent-own-account.md). | HUB, WEB, E2E | 11 §5.5 | E2E-54, `tests/platform/tool/realhub/test_alone.py`, `test_without_person.py` |

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
| R-3.3 | [§3 b2](../KONZEPT.md#k-3-b2) | Devices can be assigned to workgroups. | `device_assign` / `device_unassign` log entries + wraps. | HUB, WEB | 04 §4.7, §5.9 | E2E-38, E2E-57 |
| R-3.4 | [§3 b3](../KONZEPT.md#k-3-b3) | By default DFX provides its own execution servers. | Platform devices certified in the platform log; auto-assigned for private orgs. ADR-023. | HUB, WEB, RUN | 04 §5.9, 03 §4.6.1 | E2E-05 |
| R-3.5 | [§3 b3](../KONZEPT.md#k-3-b3) | Users can optionally attach their own hardware. | Org devices (kind `org`); an own Mac without a terminal (R-3.14). | RUN, HUB | 04 §5.8 | E2E-04, E2E-36, E2E-56 |
| R-3.6 | [§3 b4](../KONZEPT.md#k-3-b4) | Initially only macOS is supported. | Runner refuses non-darwin hosts at start (`unsupported_os`); guest is Linux inside Lima. | RUN | 05 | E2E-04 (enrollment on the macOS runner device), U-RUN-config (non-darwin refused, R-P exception: a non-macOS host is not available in the E2E topologies), `agent-runner doctor` assertion in E2E-04 |
| R-3.7 | [§3 b5](../KONZEPT.md#k-3-b5) | Execution in Lima VMs: one separate VM per workgroup on an execution device. | VM ↔ (device, wg) 1:1. | RUN, GST | 05, 02 §4 | E2E-11 |
| R-3.8 | [§3 b5](../KONZEPT.md#k-3-b5) | Several tasks of the same workgroup may share the VM. | | RUN | 05 | E2E-10 |
| R-3.9 | [§3 b5](../KONZEPT.md#k-3-b5) | … but must run in mutually isolated AI-session sandboxes. | Per-session UID, systemd unit, Landlock, managed settings. ADR-008. | SBX, GST | 05 §4 | E2E-10, ATK-01..ATK-40 |
| R-3.10 | [§3 b6](../KONZEPT.md#k-3-b6) | First task in a new org/workgroup: VM and session ready in < 10 s. | Measured as `first_working_at − task.created_at` (hub clock). Golden image + warm pool + clone fallback. ADR-009. | RUN, HUB | 03 §4.6.1, 07 §5.3 | E2E-06, PERF-01, PERF-02 ; PERF-01 includes 10 runs "new workgroup in an existing org" |
| R-3.11 | [§3 b7](../KONZEPT.md#k-3-b7) | UI shows a countdown instead of a spinner. | Countdown from `estimate_ms`. | WEB | 06 §4 | E2E-06 |
| R-3.12 | [§3 b7](../KONZEPT.md#k-3-b7) | On delays the actual status stays visible. | Status list from session transitions; real reason (`device_offline`, `capacity_exhausted`, step + elapsed). | WEB, HUB | 06 §4, 02 §2.4.1 | E2E-07, E2E-38 |
| R-3.13 | [§3 b8](../KONZEPT.md#k-3-b8) | Initial server allocation: one DFX server hosts the hub, another hosts DFX cloud sessions. | Expressed as roles "hub device" and "runner device" (no host names); proven by topology T2. Production deployment is out of scope (no deploy). | HUB, RUN, E2E | 00 §1, 07 §5 | E2E-32 |
| R-3.14 | [§3 b3](../KONZEPT.md#k-3-b3), [E-03](../KONZEPT.md#e-03) | Own hardware is connected without a terminal: the user's AI agent does it with one command and one system dialog; without an agent a download and a double click. | Signed, versioned runner package; `agent-platform device add|remove --this-computer`; download area of the hub; maintenance daemon with updates and a way back; removal leaves nothing. ADR-074. | RUN, TOOL, HUB | 05 §16, 11 §8.4, 03 §2 | E2E-56; unit suites `tests/platform/runner/install/`, `tests/platform/tool/test_ownmac.py`; manual check `manual-checks/own-device.md` (installation probe in a fresh macOS guest, steps on a real Mac) |

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
| R-5.4 | [§5 b3](../KONZEPT.md#k-5-b3) | Decryption only by authorized users and authorized execution devices. | Wraps only for entitled recipients (W1–W3); removed principals rotated out. | WEB, RUN, HUB | 04 §6.3, §5.10 | E2E-31, E2E-18, E2E-72 |
| R-5.5 | [§5 b4](../KONZEPT.md#k-5-b4) | On a DFX execution server the user trusts that server with plaintext; zero knowledge towards the hub only. | Devices hold WK/OK/CK of their assignments. ADR-007. | RUN | 00 §3, 04 §2 | E2E-05, E2E-09 |
| R-5.6 | [§5 b5](../KONZEPT.md#k-5-b5) | Passkey login does not by itself yield an encryption key; key derivation, sharing and device distribution must be defined. | PRF-based identity bundle, recovery code, scope keys with epochs, HPKE wraps. ADR-006. | WEB, RUN, HUB | 04 §2–§6 | E2E-30, E2E-31, CON-02 |

## R-6 Live synchronisation, transparency, audit

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-6.1 | [§6 b1](../KONZEPT.md#k-6-b1) | All relevant events are stored immediately locally on the execution device in its database. | Guest journal (SQLite) and runner outbox (PostgreSQL) before any network send. | GST, RUN | 02 §4, §5 | E2E-14 |
| R-6.2 | [§6 b1](../KONZEPT.md#k-6-b1) | … and synchronised encrypted to the hub with minimal delay. | Outbox push on every new item; hints over WebSocket. The model-access state of a device (observed limits, operator subscription snapshots, the per-device DFX key's spend) follows the same outbox, plaintext metadata only ([ADR-081](../adr/081-model-access-fallback-and-dfx-access-on-own-hardware.md)). | RUN, HUB | 03 §6 | E2E-14, PERF-03 |
| R-6.3 | [§6 b2](../KONZEPT.md#k-6-b2) | Covered: prompts, answers, tool events, results, status, errors, file changes. | Event kinds `prompt`, `session.output/tool/hook/file/status`, `operation.*`. | GST, RUN, HUB | 03 §7.2, §10 | E2E-13, E2E-21, E2E-40, E2E-47, E2E-55, E2E-69 |
| R-6.4 | [§6 b2](../KONZEPT.md#k-6-b2) | Covered: token usage, costs, context-window utilisation. | Gateway usage records; OpenRouter cost from the stream; context % from model catalog. ADR-025. | GST, RUN, HUB, WEB | 02 §2.8, 03 §6.1 | E2E-16 |
| R-6.5 | [§6 b2](../KONZEPT.md#k-6-b2) | Covered: Claude subscriptions and available quotas, as far as technically readable. | Usage endpoint polled per subscription credential (≥ 900 s); `quota_snapshot`. The operator's subscriptions are polled too (`operator_quota`); per organization the own and the operator's subscriptions with remaining quota and next release, or honestly "not readable" with the last observed limit time, in the settings, as a counter of the situation view and as `agent-platform quotas --org` ([ADR-081](../adr/081-model-access-fallback-and-dfx-access-on-own-hardware.md) §6). | RUN, HUB, WEB | 02 §2.8, 03 §4.3, 05 §10.6–§10.8 | E2E-16, LIVE-03 |
| R-6.6 | [§6 b3](../KONZEPT.md#k-6-b3) | `agent.dfx.swiss` shows current state and time-precise events also on smartphones. | Responsive UI, mobile emulation tests; event times shown as absolute local `HH:MM:SS.mmm` (relative only as secondary label, 06 §12.4). | WEB | 06 | E2E-29, E2E-58 |
| R-6.7 | [§6 b4](../KONZEPT.md#k-6-b4) | Every action, change and execution is logged with actor, time, task, session, workgroup, device, target, result and changes. | Hash-chained `audit` table (metadata) + reference to the encrypted event holding the change. | HUB, RUN | 02 §2.9 | E2E-15  (asserts `audit.event_id` links to the encrypted change event), E2E-47 |
| R-6.8 | [§6 b5](../KONZEPT.md#k-6-b5) (proposal) | Append-only event log with replay after connection interruptions. | **Adopted**: append-only events, gapless outbox `origin_seq`, per-scope cursors, resume. ADR-013. | HUB, RUN, WEB | 03 §5, §6, 02 §3 | E2E-14, E2E-33 |
| R-6.9 | [§6 b5](../KONZEPT.md#k-6-b5) | Absolutely delay-free network synchronisation cannot be guaranteed. | Expressed as latency targets: SSE p95 < 300 ms on LAN, terminal live < 1 s. | HUB, WEB | 03 §5 | PERF-03, PERF-04, E2E-14 |
| R-6.10 | [§6 b6](../KONZEPT.md#k-6-b6) | Default view: chat with prompts and results. | | WEB | 06 | E2E-13 |
| R-6.11 | [§6 b7](../KONZEPT.md#k-6-b7) | Additional native Claude Code session view: full live terminal output and history. | asciicast capture, encrypted `session.terminal` events, xterm.js view. ADR-016. | GST, WEB | 05, 06 | E2E-12 |
| R-6.12 | [§6 b7](../KONZEPT.md#k-6-b7) | That view is read-only; prompts are entered only via the default view. | No input path; typed text is refused by the prompt-origin hook. | WEB, GST | 05 §5 | E2E-12, E2E-35 |

## R-7 Monitoring

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-7.1 | [§7 b1](../KONZEPT.md#k-7-b1) | Full resource monitoring on the levels device → Lima VM → AI session. | | RUN, GST, HUB, MON | 08 | E2E-17 |
| R-7.2 | [§7 b2](../KONZEPT.md#k-7-b2) | CPU, RAM, storage, network, runtime, reachability, state and free capacity are visible. | | RUN, HUB, WEB | 08, 02 §2.8 | E2E-17, E2E-47 |
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
| R-8.5 | [§8 b4](../KONZEPT.md#k-8-b4) | Access rights enforced technically in the backend. | Hub checks every request; direct API calls in tests. | HUB | 03 §3 | E2E-18, U-HUB-rights, E2E-45, E2E-72 |
| R-8.6 | [§8 b4](../KONZEPT.md#k-8-b4) | … and in the user interface. | Controls hidden/disabled; readers verify author rights. | WEB | 06 | E2E-18, E2E-72 |
| R-8.7 | [§8 b5](../KONZEPT.md#k-8-b5) | Sessions may read and discover via platform APIs everything they are authorised for: members, workgroups, tasks, status, whiteboards. | MCP read tools; what both the task owner and the person whose instruction the session works on may read, across the workgroups of the organization the device serves. ADR-089 (supersedes ADR-024). | GST, RUN | 03 §8.1 | E2E-19, E2E-52, E2E-68 |
| R-8.8 | [§8 b5](../KONZEPT.md#k-8-b5) | No blanket insight into foreign workgroups. | A workgroup that the owner or the instructing person may not read is `not_found`; names stay visible as organization metadata. ADR-089. | GST, RUN | 03 §8.1 | E2E-19, E2E-68 |
| R-8.9 | [§8 b6](../KONZEPT.md#k-8-b6) | Read accesses are logged too. | `read.<resource>` audit rows for API and session reads; session reads name the workgroup read and the instructing person (ADR-089 §4). | HUB, RUN | 02 §2.9 | E2E-15, E2E-19, E2E-68, E2E-72 |

## R-9 GitHub

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-9.1 | [§9 b1](../KONZEPT.md#k-9-b1) | Users can connect and disconnect their GitHub account. | Device flow on the execution device, or an existing GitHub token (GitHub CLI login, personal access token) checked by the execution device (KOR-25, ADR-065); disconnect hard-deletes the credential everywhere. A `github.*` operation without a usable connection is held up to 15 min while the task owner is asked in the task's chat (KOR-25, ADR-066). | WEB, RUN, HUB | 03 §4.8, §4.10 | E2E-20, E2E-59, E2E-60 |
| R-9.2 | [§9 b2](../KONZEPT.md#k-9-b2) | Authorised repository access. | `github.checkout`, `github.get_pull_request`, `github.list_repositories`, `github.list_pull_requests`, `github.list_issues`, `github.get_issue` (KOR-25). | RUN | 03 §9 | E2E-21, LIVE-02, E2E-59 |
| R-9.3 | [§9 b2](../KONZEPT.md#k-9-b2) | Branches, commits, pushes, pull requests, issues and comments. | Operation types of 03 §9 (`github.comment` on issues and pull requests; `github.create_issue`, `github.update_issue`, KOR-25; `github.update_pull_request`, HOT-30), REST/Git Data API only (one GraphQL mutation for a pull request's draft state, HOT-30). | RUN | 03 §9 | E2E-21, LIVE-02 |
| R-9.4 | [§9 b3](../KONZEPT.md#k-9-b3) | Rights limited to the connected account and released repositories. | GitHub App installation or the connected account's access + optional workgroup repository allow-list (ADR-054). | RUN | 03 §9 | E2E-22  (incl. `repo_not_accessible`), E2E-59, E2E-60 |
| R-9.5 | [§9 b3](../KONZEPT.md#k-9-b3) | All actions are audited. | | HUB, RUN | 02 §2.9 | E2E-15, E2E-21 |
| R-9.6 | [§9 b4](../KONZEPT.md#k-9-b4) (recommendation) | Check GitHub App with granular permissions. | **Adopted**: GitHub App with user-to-server tokens. ADR-015. | RUN, DBL | 05 | E2E-20, LIVE-02 |
| R-9.7 | [§9 b5](../KONZEPT.md#k-9-b5) | Claude Code must not run git commands itself; GitHub actions only as controlled platform operations. | No git in the sandbox (no Bash, Landlock exec allow-list); operations executed by RUN on the host. | SBX, RUN | 05 §4, 03 §9 | E2E-23, ATK-02 |

## R-10 Harness, hooks, strict execution ban

| ID | Ref | Requirement | Decision / interpretation | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-10.1 | [§10 b1](../KONZEPT.md#k-10-b1) | Every session automatically gets a standardised harness with org, workgroup, task and session context and documented platform APIs. | Fixed rules text + generated context file per session. | GST, SBX | 05 | E2E-23 |
| R-10.2 | [§10 b2](../KONZEPT.md#k-10-b2) | The harness states the prohibitions and the duty to request every action via `agent.dfx.swiss`. | | SBX | 05 | E2E-23 |
| R-10.3 | [§10 b3](../KONZEPT.md#k-10-b3) | Claude Code must not run programs, scripts, shell commands, git commands, tests or builds. | | SBX, GST | 05 §4 | E2E-23, ATK-01..ATK-12 |
| R-10.4 | [§10 b3](../KONZEPT.md#k-10-b3) | Claude Code must not start subagents. | Agent/Task tools denied. | SBX, GST | 05 §4 | E2E-23, ATK-02, ATK-03 |
| R-10.5 | [§10 b4](../KONZEPT.md#k-10-b4) | Manipulating or circumventing hooks and security rules is forbidden. | Managed settings only; settings/harness read-only; edits denied. | SBX, GST | 05 §4, §5 | E2E-34, ATK-13..ATK-20 |
| R-10.6 | [§10 b3 s2](../KONZEPT.md#k-10-b3) | Subagents may be requested via a controlled platform operation and started by the platform. | `subagent.request`. | GST, RUN, HUB | 03 §9 | E2E-24 |
| R-10.7 | [§10 b5](../KONZEPT.md#k-10-b5) | Hooks block forbidden actions before execution. | PreToolUse policy. | GST | 05 §5 | E2E-23, ATK-13 |
| R-10.8 | [§10 b5](../KONZEPT.md#k-10-b5) | The sandbox enforces the bans technically as well. | OS layer alone stops every attack (fault-injected hooks). | SBX | 05 §4 | ATK-21..ATK-32, E2E-34 |
| R-10.9 | [§10 b6](../KONZEPT.md#k-10-b6) | Only explicitly defined, authorised actions; no arbitrary shell execution. | Closed list of operation types; no exec-like type. ADR-012. | GST, RUN | 03 §9 | E2E-22, ATK-33 |
| R-10.10 | [§10 b7](../KONZEPT.md#k-10-b7) | Actions are checked deterministically and formally, not by content. | Schema, allow-lists, size and pattern rules only. | RUN, HUB | 03 §8.3, §9 | E2E-22 |
| R-10.11 | [§10 b7](../KONZEPT.md#k-10-b7) | Execution by controlled platform components. | Runner host executor outside the VM. | RUN | 00 §3.1 | E2E-21 |
| R-10.12 | [§10 b8](../KONZEPT.md#k-10-b8) | Blocked attempts are logged. | `session.hook` events + `hook.denied` audit rows; a refusal of the kernel after an allowed call is counted per session in the monitoring (`agent_runner_session_sandbox_denials_total`, KOR-60). | GST, RUN, HUB | 02 §2.9 | E2E-23, E2E-15, E2E-51 |

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
| R-13.3 | [Idee 26](../KONZEPT.md#k-idee-26) | Only the platform may start the subagent; Claude Code never directly. | | SBX, RUN | 05 §4 | E2E-24, ATK-02, ATK-03 |
| R-13.4 | [Idee 26](../KONZEPT.md#k-idee-26) | Every approved subagent is a full Claude Code session in its own sandbox with own harness, hooks, real-time sync, audit logging and resource monitoring. | It keeps running across follow-up instructions of its main agent (`subagent.instruct`) until the main agent stops it (`subagent.stop`) or ends ([A10](../KONZEPT.md#a-10), ADR-088). | RUN, GST, SBX | 05 | E2E-24, E2E-17  (asserts the child session appears in the monitoring overview), E2E-68 |
| R-13.5 | [Idee 26](../KONZEPT.md#k-idee-26) | The new session is linked to the parent task and the requesting session. | `parent_session_id`, same `task_id`. | HUB | 02 §2.4 | E2E-24 |
| R-13.6 | [Idee 26](../KONZEPT.md#k-idee-26) | Users can view the complete subagent session at any time in the web UI incl. read-only terminal view and history. | | WEB | 06 | E2E-24 |
| R-13.7 | [Idee 26](../KONZEPT.md#k-idee-26) | Subagents can request further subagents only via the platform. | Depth counted by the hub. | GST, HUB | 03 §9 | E2E-24 |
| R-13.8 | [Idee 26](../KONZEPT.md#k-idee-26) (open) | Limiting parallel or nested subagents is open. | **Decided**: settings of the hub with generous defaults (depth 5, 50 running per task, no total and no runtime limit; 0 = no limit), a clear message `limit_exceeded` and the situation reason `limit_reached` ([A5](../KONZEPT.md#a-5), ADR-088, supersedes ADR-017). | HUB | 03 §12 | E2E-24, E2E-68 |

## R-14 Open technical decisions (KONZEPT §13)

| ID | Ref | Decision | ADR | Comp | Spec | Tests |
|---|---|---|---|---|---|---|
| R-14.1 | [§13.1](../KONZEPT.md#k-13-1) | Passkey PRF identity bundle, recovery code, scope keys with epochs, HPKE wraps, signed membership log; the closed key model over every principal (rules KM-01 … with their vectors, gaps with their packages). | ADR-006, ADR-007, ADR-023, ADR-058, ADR-060; [key model](../key-model.md) (KOR-54) | WEB, RUN, HUB | 04, [key model](../key-model.md) | E2E-30, E2E-31, CON-02, U-DOC-key-model |
| R-14.2 | [§13.2](../KONZEPT.md#k-13-2) | Session isolation in one VM and full execution ban: two independent layers. | ADR-008 | SBX, GST | 05 §4 | ATK-01..ATK-40, E2E-10 |
| R-14.3 | [§13.3](../KONZEPT.md#k-13-3) | VM start < 10 s via golden image + warm pool, clone fallback. | ADR-009 | RUN | 05 | PERF-01, PERF-02 |
| R-14.4 | [§13.4](../KONZEPT.md#k-13-4) | Claude Code + OpenRouter + DeepSeek compatible; costs from the stream; quotas via usage endpoint. | ADR-010, ADR-025 | RUN, GST | 05 | LIVE-01, LIVE-03, E2E-16 |
| R-14.5 | [§13.5](../KONZEPT.md#k-13-5) | API contracts, formal rules, idempotency windows. | ADR-012 | all | 03 | CON-01, E2E-22 |
| R-14.6 | [§13.6](../KONZEPT.md#k-13-6) | Device assignment, resource planning, offline/reconnect sync. | ADR-013, ADR-087 | HUB, RUN | 03 §4.6.2, §4.6.5, §6 | E2E-14, E2E-38, E2E-67 |
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
| K-3.5 | [§3 b5](../KONZEPT.md#k-3-b5) | Under high load several VMs per workgroup may run on several devices. | gebaut | A full VM (sessions per VM, a setting) gets a second VM on the device or a VM on another assigned device – [ADR-075](../adr/075-resource-choice.md) | KOR-41 |
| K-10.3 | [§10 b3](../KONZEPT.md#k-10-b3) | Subagents, tests and browsers are requested only as controlled platform operations. | teils | Subagents only by operation (R-13); tests and browsers not yet; the work VM is free ([E-08](../KONZEPT.md#e-08)). | KOR-43, KOR-44 |
| K-27 | [Idee 27](../KONZEPT.md#k-idee-27) | Temporary test VMs: every test run gets an isolated temporary Lima VM; results, logs and exit codes stored; VM removed even on failure. | gebaut | Operation `test.run`: a fresh VM per run, result with exit code and summary, the complete log kept encrypted outside the VM, the VM deleted on every path; tests go through it, everything else stays free in the work VM ([ADR-080](../adr/080-test-runs-as-operations.md), [A6](../KONZEPT.md#a-6), [A8](../KONZEPT.md#a-8), [A9](../KONZEPT.md#a-9)). | KOR-43 |
| K-28 | [Idee 28](../KONZEPT.md#k-idee-28) | Temporary browser VMs: isolated Ubuntu VM with a browser controlled through controlled interfaces, traceable and live, removed after use. | teils | Operations `browser.open`/`browser.close` with a temporary browser VM (torn down on close, idle, session end, task close, credit, restart; a failed VM is replaced on the next action), eleven browser tools for the session, every action an encrypted record entry with the picture after it, the live picture end to end encrypted and sent only while someone watches; the free browser of the work VM stays unwatched ([ADR-085](../adr/085-browser-as-platform-operation.md), [A6](../KONZEPT.md#a-6), [E-09](../KONZEPT.md#e-09)). | KOR-44 |
| K-29 | [Idee 29](../KONZEPT.md#k-idee-29) | Explicit DFX support release of a workgroup (read or write), revocable, audited. | gebaut | Root-signed platform role `support` bound to the person's certification, release per workgroup in the membership log by an admin or a writer, wraps per support person, revocation with rotation, support reads and writes audited and shown to the organization ([ADR-096](../adr/096-support-release.md), [A12](../KONZEPT.md#a-12)); the support side through an agent is a decision of the owner (ADR-096 §6). | KOR-56 |
| K-30 | [Idee 30](../KONZEPT.md#k-idee-30) | Encryption by default without UX disadvantages; passkey the only visible release; org admins read all workgroups. | gebaut | Client-side encryption, no recovery-code step ([E-13](../KONZEPT.md#e-13)); admins read every workgroup ([ADR-058](../adr/058-admins-read-every-workgroup.md)); inheritance, distribution and revocation described in the [key model](../key-model.md) (KOR-54), revocation of the agent (KOR-57) and of the support release ([ADR-096](../adr/096-support-release.md), KOR-56) built. | KOR-34, KOR-54 |
| K-31 | [Idee 31](../KONZEPT.md#k-idee-31) | Scale and identity hierarchy: humans, main agents and subagents distinguishable; names and GitHub usernames for main agents. | gebaut | Main agents with handle `#<number>` and a sealed, renamable name (automatic from the title), subagents `#<number>.<path>`; the session knows its name and its children's; audit actors `main_agent`/`subagent` on behalf of a person; responsibility view by person with GitHub identity, paged and windowed; agent tool `agents …` ([ADR-095](../adr/095-main-agents-with-names.md), KOR-52); per-session GitHub identity (KOR-47, [ADR-078](../adr/078-github-identities.md)); the role (rights profile) of every session in the session tree (KOR-51, [ADR-094](../adr/094-rights-profiles-and-agent-roles.md)) ([E-02](../KONZEPT.md#e-02)). | KOR-26, KOR-27, KOR-47, KOR-51, KOR-52 |
| K-32 | [Idee 32](../KONZEPT.md#k-idee-32) | Autonomous-first: dashboard prioritises agent status, progress, blockers and required decisions. | gebaut | Task per request, task overview ([E-21](../KONZEPT.md#e-21)); the home of an organization is the situation view: needs a decision, stuck, running, finished, across all readable workgroups, decisions taken in place, the same through `agent-platform` ([ADR-076](../adr/076-situation-view-and-attention-categories.md)). | KOR-32, KOR-39 |
| K-33 | [Idee 33](../KONZEPT.md#k-idee-33) | "Optimise processes" button starts an analysis session over authorised logs and metrics, no automatic changes. | gebaut | The button (situation view, workgroup view; agent tool `optimize`) creates an analysis task with the read-only profile `analysis`; every session reads usage, audit, operations and metrics of the workgroups its people may read (`get_usage`, `list_audit`, `list_workgroup_operations`, `get_metrics`, read-logged); the report has six sections with evidence; a person adopts it onto a whiteboard ([ADR-097](../adr/097-process-analysis.md)); E2E-73. | KOR-53 |
| K-34 | [Idee 34](../KONZEPT.md#k-idee-34) | Exactly one delegated main orchestrator per user, passkey release, own key, audit, revocation. | gebaut | Pairing with a passkey and audit (KOR-24, [ADR-056](../adr/056-delegated-agent-access.md)); own identity as a sub-principal of its user with real revocation by rotation, and the orchestrator as a platform session on DFX hardware (KOR-57, [ADR-082](../adr/082-agent-own-identity.md)). | KOR-24, KOR-57 |
| K-35 | [Idee 35](../KONZEPT.md#k-idee-35) | Own hardware exclusively for a workgroup or the whole organization; capacity, not data access. | gebaut | Per workgroup or for the whole organization in one step (`device_assign_org`); the device holds the keys of the workgroups it may run (said in the dialog); an own Mac is connected without a terminal (`device add --this-computer`, ADR-074, KOR-59) – [ADR-075](../adr/075-resource-choice.md) | KOR-41 |
| K-36 | [Idee 36](../KONZEPT.md#k-idee-36) | Every workgroup member with write access can continue a session; interventions attributed. | gebaut | The session learns the verified sender of every prompt (hook line, never text), operations, sessions and consumption carry "triggered by" ([A15](../KONZEPT.md#a-15)), the queue is visible and a waiting prompt can be withdrawn ([ADR-083](../adr/083-who-speaks-in-a-shared-session.md), E2E-64). | KOR-46 |
| K-37 | [Idee 37](../KONZEPT.md#k-idee-37) | GitHub identities per PR and commit (author, committer, signature, co-authors), defaults per org/workgroup. | gebaut | Several accounts per user, company accounts of organizations (the operator's account among them, [E-06](../KONZEPT.md#e-06)), choice per operation, session and task, defaults per workgroup and organization, author/committer/co-authors from registered identities only, own SSH signing key; GitHub signs no API commits (pre-study of [ADR-078](../adr/078-github-identities.md)). | KOR-47 |
| K-38 | [Idee 38](../KONZEPT.md#k-idee-38) | Automatic and manual choice of device and VM, deterministic. | gebaut | Deterministic device choice (hub) and VM choice (runner), several VMs per workgroup, choice by user and agent, waiting states instead of refusals – [ADR-075](../adr/075-resource-choice.md) | KOR-41 |
| K-39 | [Idee 39](../KONZEPT.md#k-idee-39) | Session completion and cleanup; logs, results and context kept. | gebaut | Closing ends the process and frees the sandbox; the context is kept as encrypted checkpoints at the hub (deleted with the task) and stays resumable ([ADR-072](../adr/072-session-context-resume.md)). | KOR-33 |
| K-40 | [Idee 40](../KONZEPT.md#k-idee-40) | Full session resume of the original Claude Code session, also on another VM. | gebaut | Every follow-up continues the same conversation (`claude --resume` with the same ID) from the task's checkpoint, with its workspace, after close, stop, failure, VM deletion, the switch of its paused VM to a newer golden ([ADR-093](../adr/093-workgroup-vms-follow-the-golden.md)) and on another VM or device; a new AI only as the reported emergency path. PR → task relation as opaque tag; the button on the PR is idea 41 ([ADR-072](../adr/072-session-context-resume.md)). | KOR-33, KOR-70 |
| K-41 | [Idee 41](../KONZEPT.md#k-idee-41) | PR conflict auto-fix button; updates the PR branch, no automatic merge. | gebaut | Pull requests per task and workgroup with a fresh conflict state ("being checked" while GitHub computes); "Fix conflicts" sends a fixed instruction to the original session (resume, ADR-072); `github.merge_base` merges the base into the workspace with markers, the merge commit has two parents and is verified against the preparation, fast-forward push, never a merge of the pull request ([A2](../KONZEPT.md#a-2), [ADR-084](../adr/084-fix-conflicts-by-merge-commit.md)); the instruction asks for the tests with `test.run` before the merge commit (K-27). | KOR-48 |
| K-42 | [Idee 42](../KONZEPT.md#k-idee-42) | Specialised agent roles with configurable API rights. | gebaut | Rights profiles per task or child as a voluntary restriction (types and read reach), enforced by hub, runner and guest, never more than the parent; built-in roles development, security, review, merge, read only; review verdicts, security reports, reviews and checks, `github.merge` with formal merge criteria and separate duties as workgroup settings that default to none ([A1](../KONZEPT.md#a-1), [ADR-094](../adr/094-rights-profiles-and-agent-roles.md)). | KOR-51 |
| K-43 | [Idee 43](../KONZEPT.md#k-idee-43) | One main agent per task with fresh context; continuing a task uses its original session. | gebaut | Own Linux user and `.claude` per task; continuation after completion built (K-40, KOR-33); reading beyond the own workgroup within owner ∩ instructing person ([A11](../KONZEPT.md#a-11), [ADR-089](../adr/089-sessions-read-like-their-human.md)). | KOR-45 |
| K-44 | [Idee 44](../KONZEPT.md#k-idee-44) | Cloud sessions resume on another device after a device failure; temporary VMs are restarted. | gebaut | Interruption after `AGENT_HUB_DEVICE_FAILOVER_SECONDS`, deterministic move with the pinned checkpoint, the interrupted instruction repeated, the lost session fenced; test runs cancelled and repeated by the session, their log kept where the VM survived; with one device: continuation after a restart ([ADR-087](../adr/087-device-failure.md), KOR-58) | E2E-67 |
| K-45 | [Idee 45](../KONZEPT.md#k-idee-45) | No DFX fee with own hardware and own AI access. | gebaut | Own resources are never charged ([ADR-062](../adr/062-prepaid-credit-and-usage-billing.md)). | KOR-31 |
| K-46 | [Idee 46](../KONZEPT.md#k-idee-46) | Usage-based billing: tokens and VM seconds, attributable to task, session, workgroup and org, visible. | teils | Tokens, session VM runtime and the runtime of work and test VMs charged by a price list ([ADR-062](../adr/062-prepaid-credit-and-usage-billing.md), [ADR-080](../adr/080-test-runs-as-operations.md), [E-15](../KONZEPT.md#e-15)–[E-17](../KONZEPT.md#e-17)); own hardware + DFX model access (case 3) through a limited provider key per org device, booked as tokens from the provider's figures ([ADR-081](../adr/081-model-access-fallback-and-dfx-access-on-own-hardware.md) §7); browser VMs with a live picture not yet. | KOR-31, KOR-43, KOR-44, KOR-50 |
| K-47 | [Idee 47](../KONZEPT.md#k-idee-47) | Prepaid balance; without balance no paid resources; controlled stop, resumable. | gebaut | Prepaid credit per org, controlled stop and continuation after a credit ([ADR-062](../adr/062-prepaid-credit-and-usage-billing.md), [E-14](../KONZEPT.md#e-14)). | KOR-31 |
| K-16 | [§16](../KONZEPT.md#k-16) | Final concept decisions and technical checks. | teils | No project level, fresh context, bans, idempotency, runtime metering and balance stop ([ADR-062](../adr/062-prepaid-credit-and-usage-billing.md)) and session resume across VMs and devices (KOR-33, [ADR-072](../adr/072-session-context-resume.md)) built, the latter also after a device failure (points 5 and 7, KOR-58, [ADR-087](../adr/087-device-failure.md)); the key model of point 7 is checked in the [key model](../key-model.md) (KOR-54) with its gaps (KOR-55, KOR-56, KOR-57, KOR-45, KOR-47). | see above |

## R-E Rules of the client (`KONZEPT.md` part B)

Younger than the concept; where they contradict it, the rule holds. Wording, date and reference in
[`KONZEPT.md`](../KONZEPT.md#teil-b--regeln-und-entscheide-des-auftraggebers); a new rule is entered there first. The
interpretations A1–A21 (part C) have no rows of their own; the rows above and below cite them where they apply.

| ID | Ref | Rule (short) | Status | Where |
|---|---|---|---|---|
| E-01 | [E-01](../KONZEPT.md#e-01) | Full concept version (47 ideas) is binding. | gebaut | this document, R-16 |
| E-02 | [E-02](../KONZEPT.md#e-02) | "Hundreds of agents" describes use, no number target; no artificial limit, clear message at capacity. | teils | KOR-35, KOR-45 ([A14](../KONZEPT.md#a-14)); model accesses: a used-up subscription is passed over within its cost class, a wait names its time and continues by itself, quotas are visible (KOR-50, [ADR-081](../adr/081-model-access-fallback-and-dfx-access-on-own-hardware.md)) |
| E-03 | [E-03](../KONZEPT.md#e-03) | The user only creates a passkey; their AI agent sets up everything else, incl. subscription token transfer. | gebaut | [ADR-056](../adr/056-delegated-agent-access.md) |
| E-04 | [E-04](../KONZEPT.md#e-04) | An agent without passkey can do everything a human with passkey can. | gebaut | [ADR-056](../adr/056-delegated-agent-access.md) |
| E-05 | [E-05](../KONZEPT.md#e-05) | A missing prerequisite is triggered, not explained. | gebaut | [ADR-066](../adr/066-held-github-operations.md) (GitHub), [ADR-079](../adr/079-no-approval-as-precondition.md) (model access, credit, device, platform access; register `15-prerequisites.md`) |
| E-06 | [E-06](../KONZEPT.md#e-06) | GitHub through the operator's account for approved users. | gebaut | [ADR-053](../adr/053-operator-github-account.md), [ADR-054](../adr/054-repository-list-optional.md), [ADR-065](../adr/065-github-existing-token.md) |
| E-07 | [E-07](../KONZEPT.md#e-07) | Operator subscriptions for approved organizations. | gebaut | [ADR-052](../adr/052-operator-subscriptions.md) |
| E-08 | [E-08](../KONZEPT.md#e-08) | Work VM on request in which everything is allowed. | gebaut | [ADR-059](../adr/059-work-vm-on-request.md) |
| E-09 | [E-09](../KONZEPT.md#e-09) | Nothing is monitored inside the work VM. | gebaut | [ADR-059](../adr/059-work-vm-on-request.md) |
| E-10 | [E-10](../KONZEPT.md#e-10) | Work VM egress never via the site address, fail-closed; Cloudflare only on its free plans, no dedicated egress address. | gebaut | [ADR-059](../adr/059-work-vm-on-request.md) |
| E-11 | [E-11](../KONZEPT.md#e-11) | No unnecessary restrictions; nothing must be released one by one. | teils | [ADR-054](../adr/054-repository-list-optional.md), [ADR-059](../adr/059-work-vm-on-request.md), [ADR-079](../adr/079-no-approval-as-precondition.md) (register with test, root-signed switches built; flipped after the owner's decision) |
| E-12 | [E-12](../KONZEPT.md#e-12) | Registration only with a PRF-capable passkey. | gebaut | [ADR-055](../adr/055-registration-without-recovery-code-step.md) |
| E-13 | [E-13](../KONZEPT.md#e-13) | No mandatory recovery-code step at registration. | gebaut | [ADR-055](../adr/055-registration-without-recovery-code-step.md) |
| E-14 | [E-14](../KONZEPT.md#e-14) | Billing only from a balance the operator sets; no payment path; credit is only bought or distributed by the operator by hand (directly on the hub device, no public endpoint), never an automatic start credit. | gebaut | [ADR-062](../adr/062-prepaid-credit-and-usage-billing.md) |
| E-15 | [E-15](../KONZEPT.md#e-15) | Prices as a fair market average, in configuration. | gebaut | [ADR-062](../adr/062-prepaid-credit-and-usage-billing.md) |
| E-16 | [E-16](../KONZEPT.md#e-16) | Everything priced in USD. | gebaut | [ADR-062](../adr/062-prepaid-credit-and-usage-billing.md) |
| E-17 | [E-17](../KONZEPT.md#e-17) | Every booking also stores its CHF equivalent from one central daily USD/CHF rate. | gebaut | [ADR-062](../adr/062-prepaid-credit-and-usage-billing.md) |
| E-18 | [E-18](../KONZEPT.md#e-18) | Existing accounts are admitted to an organization directly. | gebaut | [ADR-060](../adr/060-direct-admission-of-existing-accounts.md) |
| E-19 | [E-19](../KONZEPT.md#e-19) | No single-case fixes: exclude the class of fault. | teils | KOR-35 (first case), [ADR-079](../adr/079-no-approval-as-precondition.md) (register of prerequisites with a completeness test) |
| E-20 | [E-20](../KONZEPT.md#e-20) | Community area with a group chat of all users. | offen | KOR-26 |
| E-21 | [E-21](../KONZEPT.md#e-21) | Task overview as a menu entry. | gebaut | KOR-32 |
| E-22 | [E-22](../KONZEPT.md#e-22) | One repository (`DFXswiss/agent`). | gebaut | [ADR-061](../adr/061-one-repository.md) |
| E-23 | [E-23](../KONZEPT.md#e-23) | Concept and rules in one central file in the repository. | gebaut | [KONZEPT.md](../KONZEPT.md), `tests/platform/test_konzept.py` |
| E-24 | [E-24](../KONZEPT.md#e-24) | No access layer in front of the platform; it protects itself. | gebaut | operation |
| E-25 | [E-25](../KONZEPT.md#e-25) | No unordered protection layers: usability, speed and performance first. | gebaut | every package |
| E-26 | [E-26](../KONZEPT.md#e-26) | AI agents are users like humans: operating the platform is simple and efficient for them. | offen | KOR-63 |
| E-27 | [E-27](../KONZEPT.md#e-27) | An AI agent can do everything, also without a human (own account by invitation, own key). | gebaut | [ADR-071](../adr/071-agent-own-account.md) |
| E-28 | [E-28](../KONZEPT.md#e-28) | Personal message of the requester wherever someone grants access. | offen | KOR-64 |
| E-29 | [E-29](../KONZEPT.md#e-29) | GitHub works for every customer with every repository their GitHub account can access (own account, own or third-party organization, fork). | gebaut | [ADR-077](../adr/077-github-every-repository.md) |
| E-30 | [E-30](../KONZEPT.md#e-30) | A user sees on the home page, without choosing an organization, the remaining quota of every own model subscription (per provider a collapsible line with reset, bar and share; per subscription the source and time of its reading; nothing twice) and a page with all of them; measured centrally, visible to the owner only. | gebaut | [ADR-091](../adr/091-subscriptions-at-a-glance.md) on the readings of [ADR-090](../adr/090-every-subscription-logged-in-centrally.md) |
| E-31 | [E-31](../KONZEPT.md#e-31) | Every Claude subscription has its own login on a platform device, is measured and renewed there, never copied to a computer of the user, and serves only the sessions its owner switches on (default: measure only). | gebaut | [ADR-090](../adr/090-every-subscription-logged-in-centrally.md) |
| E-32 | [E-32](../KONZEPT.md#e-32) | Nothing runs on the user's computer except at most a Claude session that steers agent.dfx.swiss; everything local today (the `agent` tool with ledger, coordinator, A38, AI accounts, `knock` and dashboard, the legacy hub, the tooling that builds, checks, merges and releases the platform) becomes a working mode of the platform that is chosen per session or task – by a sentence in the prompt, in the web client or with the agent tool – never forced and never on by default, and is then carried out completely; the accesses these need (Apple signing, server access, mailboxes, subscriptions) move along without asking, services on the computer that do not come from this repository stay out. | offen | [ADR-098](../adr/098-working-modes.md) (KOR-72; build packages in its §9) |
| E-33 | [E-33](../KONZEPT.md#e-33) | Independent work the platform does for a user runs at the same time, each in its own isolated environment – sessions, test runs and browsers as well as platform operations such as logging in several subscriptions; nothing forces them into a queue: what an operation needs belongs to it (every subscription has its own mailbox). | offen | KOR-73 |
| E-34 | [E-34](../KONZEPT.md#e-34) | All self-hosted GitHub Actions runs of the DFX organisations run through agent.dfx.swiss. | offen | concept [16-ci-runners.md](16-ci-runners.md), decisions open (§13 there); note: one VM per job is interpretation [A20](../KONZEPT.md#a-20); host-bound jobs are not covered by it and are an open deviation (decision D-1 there, [KONZEPT part D](../KONZEPT.md#d-offen)) |
| E-35 | [E-35](../KONZEPT.md#e-35) | One shared capacity: no own machines for self-hosted runners next to the platform; the operator's whole server capacity belongs to agent.dfx.swiss, and a runner is obtained through the platform. | offen | concept [16-ci-runners.md](16-ci-runners.md) (§6.6 there); note: that the runner runs on the same devices as the work VMs is decided by [E-36](../KONZEPT.md#e-36); that it is a VM stays interpretation [A20](../KONZEPT.md#a-20) (summed up in [A21](../KONZEPT.md#a-21)); host-bound jobs: decision D-1 there, [KONZEPT part D](../KONZEPT.md#d-offen) |
| E-36 | [E-36](../KONZEPT.md#e-36) | CI jobs, also with foreign PR code, run on the same devices as the work VMs of the workgroups; there is no own CI device, not even inside the platform. | offen | concept [16-ci-runners.md](16-ci-runners.md) (§6.6, §9 there); gate U stays a recommendation (decisions D-7, D-20 there) |
| E-37 | [E-37](../KONZEPT.md#e-37) | What the platform can do itself is a platform tool and needs no work or browser VM (reading a mailbox, for example); a session names the access, never sees its secret, and a VM is given only where it is really needed (operating a web page, building and testing software). | offen | KOR-74 |
| E-38 | [E-38](../KONZEPT.md#e-38) | agent.dfx.swiss gets its own vault for the accesses of agents, separate from the operator's internal staff vault; agents use its secrets without seeing them – a concept only, built when the owner commissions it. | offen | [ADR-101](../adr/101-agent-vault.md) (Proposed, not commissioned) |
| E-39 | [E-39](../KONZEPT.md#e-39) | A check for humans (picture puzzle, Cloudflare, captcha) is never circumvented and does not just end the work: the platform pauses and opens the owner a remote control of the sandbox (live picture with click and input relay, like VNC) on agent.dfx.swiss; only the human's own identity may click, never an agent. | offen | KOR-73 (subscription logins); browser sandboxes of sessions to follow |
| E-40 | [E-40](../KONZEPT.md#e-40) | A signed-in browser stays signed in and unlocked across reloads, closed and reopened tabs and browser restarts until the user signs out; the sign-in renews itself while the browser is used; selectable, on by default. | offen | task on agent (browser device key, [ADR-006](../adr/006-passkey-prf-key-management.md)) |
| E-41 | [E-41](../KONZEPT.md#e-41) | A task can be recorded like a Jira ticket without an agent starting: title and description, a person, a topic, a GitHub organisation and a repository, an urgency, status todo / inProgress / completed; the task overview filters by them; an agent works only once the task is started or assigned to an agent, a request to an agent still creates a task with its agent at once. | offen | interpretation [A22](../KONZEPT.md#a-22); task on agent |
| E-42 | [E-42](../KONZEPT.md#e-42) | A public landing page presents agent.dfx.swiss and looks roughly like dfx.swiss (login button top right, footer); it is built by an agent on Fable 5.2 that decides how to market and present the platform. | offen | interpretation [A23](../KONZEPT.md#a-23); task on agent |

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
| R-P.12 | Platform devices and the platform model budget only for entitled orgs (private orgs from a platform invitation, operator-granted `org_platform_access`, or – while the root-signed switch `open_platform_access` is on – every org without an explicit entry, ADR-079). | HUB, WEB | ADR-026, ADR-079, 03 §4.3 | E2E-01 (private org gets platform device), E2E-05 (non-private org refused with `platform_access_denied` until granted) |
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
| **Total** | **153** (130 from Grundprinzip and §1–§12 and idea 26); R-16 (24 rows) and R-E (36 rows) are tracked separately |

Every row of R-0 to R-13 names at least one `E2E-*` test; R-14, R-15 and R-P rows name the test or gate that
proves the decision. Every milestone of `09-milestones.md` lists the R-IDs that must be green at its exit; the
traceability test checks this per milestone. The file `e2e/platform/traceability.json` mirrors this matrix (ID → test IDs); the test
`test_traceability.py` fails if a requirement has no E2E test, if a referenced test ID does not exist in the
E2E suite, or if an E2E test references an unknown requirement ID.
