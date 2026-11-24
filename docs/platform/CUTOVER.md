# Cutover plan: platform to production (agent.dfx.swiss)

Status: **plan only** ([09 M4](spec/09-milestones.md#m4--cutover-plan-document-only-not-executed)). Nothing in this
document has been executed by the platform work: no deployment, no DNS, tunnel or access-policy change, no push to
the infrastructure repository. Each step below is carried out later, by the people named in its role column, after
the owner approvals of §4. Authored in agent-core (DOC-02); since ADR-061 there is one repository `DFXswiss/agent`,
and every "agent-core" below means a commit of `DFXswiss/agent` (the hub image is built from it).

Devices and people are named by role only. Concrete hosts, addresses, accounts and secret locations belong to the
infrastructure repository and its operator notes, never to this public document.

## Contents

| § | Topic | 09 M4 item |
|---|---|---|
| [1](#1-principles) | Principles | no data migration, no deployment in this work |
| [2](#2-roles) | Roles | – |
| [3](#3-target-deployment) | Target deployment: hub device, runner device, monitoring | deployment roles |
| [4](#4-prerequisites-that-need-owner-approval) | Prerequisites that need owner approval | DNS, tunnel ingress, access policy, infrastructure PR process |
| [5](#5-fixing-the-webauthn-rp-id) | Fixing the WebAuthn RP ID | RP ID before the first real passkey |
| [6](#6-cutover-steps) | Cutover steps, each with check and rollback | keys, build, bootstrap, certification, monitoring, smoke tests |
| [7](#7-retiring-the-legacy-hub) | Retiring the legacy hub | retirement of the legacy hub |
| [8](#8-rollback) | Rollback overview and point of no return | rollback |
| [9](#9-operation-without-data-migration) | Operation without data migration | no data migration |
| [10](#10-risks-and-open-owner-decisions) | Risks and open owner decisions | risk M4 |
| [11](#11-day-of-cutover-checklist) | Day-of-cutover checklist | – |

## 1. Principles

- **No data migration.** The platform starts with an empty database; the legacy Team Record stays in its own
  database and remains readable there ([00 §7](spec/00-overview.md#7-coexistence-with-the-legacy-code), §9).
- **Owner first.** Every change outside the two application repositories (DNS, tunnel ingress, access policy,
  stack definition, runner-device role, monitoring) goes through the infrastructure repository's pull-request process
  and needs the owner's written approval where its policies require it (§4). No break-glass change is planned.
- **The integration branches reach `develop` only through a human** (§6 step S0). Agents never merge or push to
  `develop` ([00 §6](spec/00-overview.md#6-integration-branches-and-work-package-branches), ADR-027 §1).
- **One step at a time.** Every step in §6 names who does it, what must hold before, how it is checked and how it is
  rolled back. A failed check stops the cutover at that step; the rollback of that step is applied, not improvised.
- **Zero knowledge holds in production as in the tests.** The hub device never holds the platform root key, user
  content or credentials of users ([ADR-003](adr/003-zero-knowledge-hub.md)); the runner device processes plaintext of
  the workgroups it serves and is trusted with it ([00 §3](spec/00-overview.md#3-trust-boundaries)).
- **Legacy stays untouched**: no legacy module, configuration or legacy runner installation is changed by the
  cutover ([ADR-001](adr/001-platform-packages-and-coexistence.md)).

## 2. Roles

| Role | Who / what | Holds |
|---|---|---|
| **Owner** | the person who decides for DFX | approvals of §4, the go/no-go of each phase |
| **Operator** | one or two named staff members | the platform root key (offline), the operator record `<key file>.state`; runs `agent-hub platform …` |
| **Infrastructure operators** | maintainers of the infrastructure repository | review and apply of the stack, DNS, tunnel, access policy and monitoring changes |
| **Hub device** | a DFX server of the fleet | one Lima VM with the hub stack (hub, PostgreSQL, tunnel connector) |
| **Runner device(s)** | DFX execution servers (macOS, Apple silicon) | `agent-runner` LaunchDaemon, its own PostgreSQL cluster, workgroup Lima VMs |
| **Monitoring hub** | the existing monitoring stack of the fleet | Prometheus/Grafana receiving the runner's textfile metrics through the host agents |
| **First users** | owner and operators | the first passkeys, recovery codes and the `inviter` role |
| **Legacy users** | developers with the legacy runner (`swiss.dfx.agent`) | their legacy installations, which stay as they are |

## 3. Target deployment

### 3.1 Hub device: hub and PostgreSQL in one Lima VM

The hub follows the **stack pattern of the infrastructure repository** (the pattern of its other product stacks):

| Aspect | Rule for the hub stack |
|---|---|
| VM | one Lima VM for this stack only, `vmType: vz`, `mounts: []` (also set on creation, the template would add a home mount), `hostLoopback: false`, every port forward ignored, a separate data disk for the container root and the database |
| Autostart | the host's existing Lima autostart LaunchDaemon starts the VM; the VM name is added to its list by the stack PR |
| Services (Compose, `restart: unless-stopped`) | `hub` (`agent-hub serve`, one uvicorn worker, port 8788, **no host port**), `hub-db` (PostgreSQL ≥ 16, image pinned by version **and** digest), `hub-db-init` (one-shot, superuser: creates the database `agent_hub` owned by the owner role and the writer role with login; idempotent), `hub-migrate` (one-shot `agent-hub migrate` with the owner DSN, run before every hub start of a new release), `connector` (the tunnel connector, pinned by digest, its run token only in a root-owned 0600 file of the stack directory) |
| Hub image | built **inside the VM from the agent-core source at the release commit** (§6 S4), tag = commit, never pulled from a registry (`pull_policy: never`). Recipe: a build stage with the Node version of `.nvmrc`, `git` and the checkout **including `.git`** at the release commit (the release build reads `HEAD`, the commit time and a clean `git status`) runs `npm ci && npm run build -- --release` in `web/`, which writes the release assets into `src/agent_hub/static/`; the runtime stage is Python 3.12 with the `hub` extra and `pip install --no-deps .` of `src/agent_hub` and `src/agent_core` only (ADR-061 §2; the assets are packaged with the hub). The build stage prints the build hash; it must equal the hash published in S4. The image definition lives in the stack PR of the infrastructure repository (DOC-02 decision 2) |
| Resource limits | every service carries the memory, swap, CPU, pid and file limits and the non-blocking log driver of the infrastructure repository's isolation policy; the VM size is entered where that policy's guard expects it |
| Secrets | root-owned 0600 files in the stack directory inside the VM, passed as environment: `AGENT_HUB_SIGNING_KEY`, the database passwords (superuser for `hub-db-init` only, owner for `hub-migrate` only, writer for `hub`). Storage and copies follow the secret decision of §4 (P6) |
| Logs | container logs through the fleet's in-VM log agent; the hub never logs secrets or content ([03 §1](spec/03-api.md#1-conventions)) |
| Backup | classified **important**: daily `pg_dump` of the `agent_hub` database plus one `agent-hub export` per organization ([runbooks/export-restore.md](runbooks/export-restore.md)), shipped to the fleet's backup targets; `agent-hub audit verify` daily ([runbooks/hub-operation.md](runbooks/hub-operation.md#5-audit-and-integrity)) |

Hub configuration (`AGENT_HUB_*`, fail-closed, [03 §2](spec/03-api.md#2-configuration-of-the-hub-agent_hub_-fail-closed-like-agent_coreconfig),
[runbooks/hub-operation.md](runbooks/hub-operation.md#2-configuration-agent_hub_-fail-closed)):

| Setting | Production value |
|---|---|
| `PUBLIC_URL` | `https://agent.dfx.swiss` – fixes the RP ID (§5) |
| `DATABASE` / `OWNER_DATABASE` | writer DSN for `hub`; owner DSN for `hub-migrate` only (refused in the hub process) |
| `SIGNING_KEY`, `HUB_KEY_ID` | hub key of §6 S3 |
| `PLATFORM_ROOT_KEY` | public root key of §6 S2 (must equal the key compiled into the web build) |
| `HOST`, `PORT` | `0.0.0.0` inside the Compose network, `8788` |
| `TRUSTED_PROXIES`, `FORWARDED_HEADER` | the connector's fixed address in the Compose network (a single address, not the whole subnet, so no other container can set the header), and the header the connector forwards (`X-Forwarded-For` unless it writes `Forwarded`); without them every user shares one anonymous rate-limit bucket ([runbooks/hub-operation.md](runbooks/hub-operation.md#4-rate-limits-and-many-users-behind-one-address)) |
| `MODEL_CATALOG` | the release's model catalog (same file as the runners) |
| `PLATFORM_BUDGET_USD_PER_ORG_DAY` | owner decision (P9); empty = unlimited |
| `AGENT_HUB_TEST_*` | never set |

Exactly **one hub process per database** ([runbooks/hub-operation.md](runbooks/hub-operation.md#1-operating-rules)); no
replicas, no second worker.

### 3.2 Runner device: `agent-runner` as LaunchDaemon

As in [runbooks/runner-installation.md](runbooks/runner-installation.md): a dedicated unprivileged runner account,
Lima ≥ 2.3 with `vz`, PostgreSQL binaries, Python 3.12, **Claude Code on the host at the golden's pin**
(`claude_code` in `images/versions.json`; the subscription login runs `claude setup-token` on the host, not in a VM),
the agent checkout at the release commit with `pip install -e ".[platform]"` in a virtual environment with pip,
`agent-runner service install --profile platform --user <runner account>` from an administrator account writing
`/Library/LaunchDaemons/swiss.dfx.agent-runner.plist` (`UserName` = runner account). The runner only connects
**outbound** to `https://agent.dfx.swiss` (signed requests and the device WebSocket); it needs no inbound port, no
tunnel hostname of its own and no address on the hub device's network.

| Setting (`AGENT_RUNNER_*`) | Production value |
|---|---|
| `HUB_URL` | `https://agent.dfx.swiss` |
| `PLATFORM_ROOT_KEY` | same public root key as hub and web build |
| `GITHUB_CLIENT_ID` | public client ID of the production GitHub App (P8) |
| `OPENROUTER_KEY_FILE` | 0600 file with the platform OpenRouter key, with the provider-side spend guardrail (P9) |
| `MODEL_CATALOG` | the same catalog file as the hub (empty = the packaged catalog of the release, which the hub must then also use) |
| `TEXTFILE_DIR` | a directory owned by the runner account; root links it into node_exporter's textfile directory (§6 S10) |
| `VM_*`, `POOL_SIZE`, `MEMORY_RESERVE_MIB` | sized for the device ([05 §3](spec/05-runner.md#3-vm-management-adr-009)); defaults are a safe start |
| `TEST_MODE` | `0` (doctor warns otherwise) |

The runner device's role in the infrastructure repository changes from "host baseline only" to "execution device";
that role change is part of the infrastructure PRs (P4). The legacy runner (`swiss.dfx.agent`,
`~/.local/share/agent`) may stay on the same device; it uses another label, home and database cluster
([00 §7](spec/00-overview.md#7-coexistence-with-the-legacy-code)).

### 3.3 Monitoring

The prepared patch of the agent repository (`deploy/monitoring/patch/`, [08 §5](spec/08-monitoring.md#5-grafana-assets-agent-deploymonitoring),
[ADR-018](adr/018-monitoring-reuse.md)) is rendered against a fresh checkout of the infrastructure repository and
handed over as its pull request ([runbooks/monitoring-patch-handover.md](runbooks/monitoring-patch-handover.md)):
three dashboards, the seven alert rules, the textfile symlink section per runner device, the absence-rule and
inventory entries where missing, and the regression test. Device-level host metrics come from the existing host
agents; no exporter, port or push password enters any VM ([08 §3](spec/08-monitoring.md#3-sink-a--prometheus-via-the-existing-stack)).

### 3.4 Network path

```
browser / runner ──HTTPS──► edge (production name) ──tunnel──► connector (in the hub VM) ──HTTP──► hub:8788
```

| Path class | Paths | Authentication by the hub |
|---|---|---|
| Web client | `/`, `/assets/…`, `/security` and the other client routes | none needed (static, SRI and CSP, [ADR-019](adr/019-web-stack-and-csp.md)) |
| Anonymous API | `/v1/health`, `/v1/config` (also read by runners and the operator CLI before every signed call), `/v1/invites/lookup`, `/v1/passkeys/{register,login}/{begin,finish}`, `/v1/recovery/{begin,finish}` | rate limits, WebAuthn ceremonies, invitation proof (ADR-043) |
| Browser API | every other `/v1/…` path and `/v1/stream`, including `/v1/platform/invites` (users with the `inviter` role) and reading the platform log | passkey-certified session key, signed requests |
| Device API | `/v1/device/…` including enrollment and the WebSocket `/v1/device/ws` | Ed25519 device signatures (enrollment: proof of possession and invitation proof) |
| Operator API | `POST /v1/platform/log`, `GET /v1/platform/enrollments` (used by `agent-hub platform`) | requests signed by the platform root key |

**Compression is the edge's job.** The hub never compresses a response, and it refuses a compressed request body
(`Content-Encoding` → `400 request_invalid`: the signed body hash covers the bytes as sent). The web budget of
[06 §12.2](spec/06-web.md#122-performance-budget) (initial JS ≤ 300 KiB gzip, time to interactive ≤ 3 s) assumes gzip or
brotli at the edge: enable response compression there for the web client paths (`/`, `/assets/…`); request bodies
pass unchanged.

Legacy paths (`/api/…`, `/sync/…`, `/auth/…`, `/pair…`, `/health`) do not exist in the new hub and answer 404 when they reach it; while an access layer covers them (P2 option a), the access layer answers instead.

## 4. Prerequisites that need owner approval

None of these is done by the platform work. Each is a pull request in the infrastructure repository (reviewed and
applied by its operators) or an explicit owner decision, recorded where the infrastructure repository's policies
require it.

| # | Prerequisite | Approval / process | Needed before |
|---|---|---|---|
| P1 | **Go decision** for the cutover and a date; the release commit (§6 S0) | owner | S0 |
| P2 | **Access policy for invitation-based external users** (risk M4, §10). Today the production name sits behind an access layer that admits staff identities only; invited customers with passkeys cannot pass it. Options: (a) keep the staff-only access layer for a first, internal phase and open the client and API paths later (staged, §6 S13); (b) remove the access layer for the production name, with the infrastructure repository's exposure checklist completed (alerts on account creation and admin sign-in, real client address in the origin logs, WAF rules adjusted, guard tests updated); (c) keep it permanently – the concept's external users are then impossible. Recommendation: (a) then (b). With (a) the device paths, the operator paths **and `/v1/config` and `/v1/health`** of §3.4 still need narrow bypasses (enrollment and every operator command read `/v1/config` first), as the legacy machine paths have today, because runners and the operator CLI cannot complete an interactive access login | owner, written; infrastructure PR | S6 (a), S13 (b) |
| P3 | **DNS**: the production name stays a proxied record on the hub stack's tunnel (unchanged if the legacy tunnel is reused, see P5) | infrastructure PR | S6 |
| P4 | **Stack and role PRs**: hub stack (Lima template, Compose, README with data contract and secret table, autostart entry, isolation-policy entries, log agent) on the hub device; runner-device role change, Lima installation and runner LaunchDaemon documentation on the runner device | infrastructure PRs, owner approval of the role change | S5, S9 |
| P5 | **Tunnel ingress**: the production name's ingress points to `http://hub:8788` of the new stack (either the existing tunnel moves to the new stack, or a new tunnel and a DNS change); the previous ingress is kept documented for the rollback of S6 | infrastructure PR, owner | S6 |
| P6 | **Secret storage** of the hub signing key, database passwords, tunnel run token, platform OpenRouter key and GitHub App secrets: the infrastructure repository's credential policy (secret store, or a listed exception for files inside the VM). The hub signing key is **not replaceable** (§6 S3) and needs an offline copy. No value of the legacy deployment is reused; every secret is generated new | owner, credential-policy entry | S3 |
| P7 | **WebAuthn RP ID** = the production name (§5) | owner, written | S5 |
| P8 | **GitHub App** for production (device flow on, permissions of [ADR-015](adr/015-github-app-device-flow-rest-only.md)), owned by the DFX organization | owner | S9 |
| P9 | **Model access and budget**: platform OpenRouter key with provider-side spend limit, default model, per-org daily budget (`PLATFORM_BUDGET_USD_PER_ORG_DAY`) | owner | S9 |
| P10 | **Monitoring PR** from the prepared patch (§3.3) | infrastructure PR | S10 |
| P11 | **Rehearsal** of S2–S11 under a non-production name on throw-away devices, including the root-key loss/re-keying procedure ([runbooks/platform-root-key.md](runbooks/platform-root-key.md#5-loss-of-the-key)) and a restore ([runbooks/export-restore.md](runbooks/export-restore.md)); passkeys of the rehearsal belong to its RP ID and are discarded with it | owner approves the rehearsal name (same process as P3/P5) | S5 |
| P12 | **Notice to legacy users** about the date and the end of the legacy hub (§7) | owner | S12 |

## 5. Fixing the WebAuthn RP ID

- The hub derives the RP ID from the host of `AGENT_HUB_PUBLIC_URL`; there is no separate setting
  ([03 §2](spec/03-api.md#2-configuration-of-the-hub-agent_hub_-fail-closed-like-agent_coreconfig)). `GET /v1/config`
  publishes `rp_id` and `origin`, and the web client refuses to run when they differ from `location.origin`
  ([06 §10.4](spec/06-web.md#104-compiled-configuration)).
- **Decision for production: RP ID `agent.dfx.swiss`, origin `https://agent.dfx.swiss`** (KONZEPT §1 names this host;
  [ADR-004](adr/004-invitation-identity-without-keys-file.md)). A parent domain as RP ID is not used: the hub has no
  setting for it, and it would let every other host under that domain request assertions for platform passkeys.
- It is fixed **before the first real passkey** (step S8) and never changed afterwards. A passkey is bound to its RP
  ID, and the PRF output that unlocks a user's identity bundle exists only for that credential
  ([04 §2–§3](spec/04-crypto.md#2-key-inventory), ADR-006). A later domain change makes every passkey unusable: users
  could return only through the recovery flow ([04 §5.5](spec/04-crypto.md#55-recovery)) with their recovery code;
  users without it lose their identity, and a single-member private organization is lost with it.
- Rehearsals and test hubs use other names; their passkeys are throw-away and never shown to real users.
- Check before S8: `GET https://agent.dfx.swiss/v1/config` returns `"rp_id":"agent.dfx.swiss"` and
  `"origin":"https://agent.dfx.swiss"`, and a registration on a real device shows the production name in the
  platform's passkey dialog.

## 6. Cutover steps

Each step: **who**, **before** (preconditions), **do**, **check**, **rollback**. Placeholders in angle brackets are
filled from the operators' private notes.

### S0 – Integration branches to `develop` (human only)

- **Who:** a human maintainer of each repository (owner approval P1).
- **Before:** the milestone acceptance packages are done; their draft PRs from `ad5f2026-agent-platform` to `develop`
  are reviewed (agent: including the repository's guard and review rules).
- **Do:** merge both draft PRs into `develop` (agent-core and agent). Tag the release commit of each repository; these
  commits are the only inputs of S4, S5 and S9.
- **Check:** legacy CI and legacy deployments are unchanged (the platform packages are separate,
  ADR-001); `make platform-ci` green on the tagged commits.
- **Rollback:** revert the merge commit in `develop`. Nothing is deployed yet.

### S1 – Release verification

- **Who:** operator.
- **Before:** S0.
- **Do:** on the tagged commits, the full suites of [07-testing.md](spec/07-testing.md): `make platform-ci` (both
  repositories, agent also `make platform-ci-linux`), E2E in topologies T1 and T2, the attack suite, PERF-01, the PRF
  device matrix ([manual-checks/](manual-checks/README.md)) with the manual rows done by a person.
- **Check:** all green; results recorded with the release.
- **Rollback:** none needed; a failure stops the cutover before anything is created.

### S2 – Platform root key

- **Who:** operator, on an operator machine that is **not** the hub device.
- **Before:** S1; P6 decided.
- **Do:** `agent-hub platform init --key-file <dir>/platform-root.json` (prints only the public key); two encrypted
  offline copies in separate places, holders recorded ([runbooks/platform-root-key.md](runbooks/platform-root-key.md#1-create-the-key-once-offline)).
  Record the public key and its SHA-256 fingerprint.
- **Check:** the key file is 0600; the public key is noted in the release record.
- **Rollback:** before S4 is published, discard the key and create a new one. After the build is published, a new
  root key means a new build and a new, empty platform ("re-keying", runbook §5).

### S3 – Hub key and stack secrets

- **Who:** operator with an infrastructure operator.
- **Before:** P6.
- **Do:** generate the hub signing seed (32 random bytes, hex) and choose `AGENT_HUB_HUB_KEY_ID` (`[a-z0-9-]`, e.g. a
  release-neutral `hub-1`); generate the database passwords (owner, writer). Store them as P6 decided; keep an offline
  copy of the signing seed with the root-key copies. Never print them into terminals that are recorded or logs.
- **Check:** the values exist only in the agreed places.
- **Rollback:** regenerate freely **until the platform-log genesis** (S7), where the operator record pins the hub key
  (a later change needs a new operator record and its pinning anew). After the first device enrolls (S9) the hub key
  is pinned by every runner, by the operator record and by invitation packages; a changed hub key makes all of them refuse the hub
  (`hub_key_changed`, unsigned or unverifiable responses) and every device must be uninstalled and enrolled again.
  Loss of the seed has the same effect.

### S4 – Release build with the root key and published build hash

- **Who:** operator.
- **Before:** S2; clean checkout of the agent-core release commit.
- **Do:** in `web/`: `npm ci`, then `VITE_PLATFORM_ROOT_KEY=<public root key> npm run build -- --release`. The release
  build refuses the test root key, a missing key and uncommitted changes, and prints `build hash <sha256>` (SHA-256 of
  `build-manifest.json`, [06 §10.3](spec/06-web.md#103-reproducible-build)). Repeat the build on a second machine.
- **Check:** both builds are byte-identical (same build hash); `npm run build:check` passes (SRI and CSP).
  **Publish** the build hash, the commit and the root-key fingerprint with the release (release notes of the
  agent-core release tag), so that users can compare them with the `/security` page.
- **Rollback:** rebuild; a published hash is superseded by publishing the corrected one with a note.

### S5 – Hub stack on the hub device (not yet routed)

- **Who:** infrastructure operators.
- **Before:** P4, P6, P7 merged/decided; P11 rehearsal passed; S3, S4.
- **Do:** create the VM and the stack as in §3.1; build the hub image in the VM from the release commit (the build of
  S4 inside the image build, same root key, same build hash); start `hub-db`; run `hub-migrate`; start `hub` and
  `connector`. The tunnel ingress of the production name does **not** point to it yet.
- **Check:** inside the VM: `GET /v1/health` ok; `GET /v1/config` shows the production `rp_id`/`origin`, the root
  key of S2 and the hub key ID of S3; `agent-hub audit verify` exit 0; `agent-hub migrate` prints `unchanged` on a
  second run; the static `build-manifest.json` hashes to the published build hash.
- **Rollback:** stop and delete the VM and its data disk; nothing is public.

### S6 – Route the production name to the new hub

- **Who:** infrastructure operators.
- **Before:** S5; P2 (option a or b), P3, P5 merged; P12 sent; legacy hub data backed up (§7).
- **Do:** switch the tunnel ingress of the production name to `http://hub:8788`; apply the access policy of P2.
  From this moment the legacy hub is no longer reachable under the production name (§7).
- **Check:** from outside: `https://agent.dfx.swiss/v1/health` ok; `/v1/config` as in S5; `/security` shows the
  published build hash and root fingerprint; a legacy path (`/sync/pull`) answers 404 (or the access layer's refusal under P2 option a); device and operator paths pass
  the access layer, the client paths behave as P2 decided; the forwarded address is the rate-limit key: a burst of
  more than 30 anonymous requests per minute from one test address is answered `429` while a second address is still
  served (the hub's access log shows the connector, because uvicorn does not apply proxy headers; real client
  addresses for the exposure checklist come from the edge's logs); the ingress reuses idle upstream connections for
  less than the hub's 120 s (cloudflared `keepAliveTimeout`, default 90 s; [runbooks/hub-operation.md](runbooks/hub-operation.md), KOR-18).
- **Rollback:** restore the previous ingress (documented in P5) and access policy; the legacy hub serves again if it
  was kept startable (§7). If the legacy hub was already offline before the cutover, the rollback is only to unroute
  the production name (no service, as before S6). The new hub keeps running unrouted; its database is kept.

### S7 – Genesis of the platform log

- **Who:** operator.
- **Do:** `agent-hub platform init --key-file <key> --hub https://agent.dfx.swiss` (appends `root` at seq 0, pins the
  hub key in `<key file>.state`).
- **Check:** the command prints `root entry appended (seq 0)`; the operator record pins the hub key ID of S3; a
  second run prints `root entry present`.
- **Rollback:** before S8 the database can be dropped and S5 repeated (the platform is still empty).

### S8 – First users (bootstrap invitation)

- **Who:** operator and the first users (owner, operators).
- **Before:** S7; §5 check done.
- **Do:** `agent-hub platform invite-user --key-file <key> --hub https://agent.dfx.swiss --expires-hours 24` per first
  user; hand each code over through a confidential channel ([runbooks/platform-root-key.md](runbooks/platform-root-key.md#bootstrap-user-invitations)).
  Each user redeems it on a real device: passkey with PRF, recovery code written down and confirmed, private
  organization created. Then `agent-hub platform grant-role inviter --user <user id> --key-file <key> --hub https://agent.dfx.swiss` for the
  people who invite customers (the `user_id` of `GET /v1/me` in that user's signed-in
  session; the user passes it on). For a shared organization (e.g. the staff organization) that is to use platform devices:
  `agent-hub platform grant-org-access --org <org id> --key-file <key> --hub https://agent.dfx.swiss`
  ([ADR-026](adr/026-platform-device-entitlement.md)); private organizations have platform access from their genesis.
- **Check:** sign-in on a second device of the same user works (passkey sync or added passkey); unlocking with the
  recovery code works; `/security` matches the published values.
- **Rollback:** until other users join, the operator may still restart from an empty database (S5); the first users
  then delete their now useless passkeys and register again with new invitations. After external users joined: no
  wipe (§8).

### S9 – Runner device: install, enroll, certify

- **Who:** runner-device operator (runner account) and operator (root key).
- **Before:** P4 (runner role), P8, P9; S7.
- **Do:** following [runbooks/runner-installation.md](runbooks/runner-installation.md) and
  [runbooks/device-certification.md](runbooks/device-certification.md#1-platform-device):
  1. install the agent release commit (`.[platform]`) and Claude Code at the golden's pin on the host
     ([runner-installation.md §1](runbooks/runner-installation.md#1-prerequisites)), configure §3.2;
  2. `agent-runner image build` (production golden, never a TEST_MODE golden; [runbooks/golden-image-release.md](runbooks/golden-image-release.md));
  3. operator: `agent-hub platform invite-device …`; runner account: `agent-runner enroll --platform --hub
     https://agent.dfx.swiss --code <code>` (state `pending_certification`, reports the `device_id`);
  4. from an administrator account: `sudo env -i PATH=… AGENT_RUNNER_…=… <venv>/bin/agent-runner service install
     --profile platform --user <runner account>` ([runner-installation.md §3](runbooks/runner-installation.md#3-install));
  5. operator compares the `device_id` and runs `agent-hub platform certify-pending --device <id>`.
- **Check:** `agent-runner doctor` all `ok` (`os`, `lima`, `vz`, `postgresql`, `golden`, `landlock`, `login_cli`,
  `hub`, `clock`, `textfile`, `test_mode`, `sync`); the `hub` line reads `active`; `agent-runner status` shows the hub connection and a filled pool.
- **Rollback:** `agent-hub platform decertify --device <id>`, then `agent-runner uninstall` on the device (removes the
  service, its VMs, the store and the identity). The legacy runner on that device is not affected.

### S10 – Monitoring patch

- **Who:** operator (renders), infrastructure operators (review, apply), root on the runner device (symlink).
- **Before:** P10; S9.
- **Do:** render the patch with the runner's textfile directory and the runner device's fleet name, open the PR,
  attach the hand edits; after merge and apply, create the textfile symlink of the host README section
  ([runbooks/monitoring-patch-handover.md](runbooks/monitoring-patch-handover.md)).
- **Check:** Prometheus has `agent_runner_*` series with the runner device's `server` label; the three dashboards
  show data; the seven rules evaluate without error; `agent-runner doctor` `textfile ok`.
- **Rollback:** revert the PR in the infrastructure repository and remove the symlink; the runner keeps working
  (the textfile sink is independent of the hub sink; `TEXTFILE_DIR` empty turns it off).

### S11 – Smoke tests in production

- **Who:** operator with a first user.
- **Do / check (all must pass):**
  1. first task in the user's private organization with the platform model: the countdown reaches
     `session.working` within 10 s (target of [ADR-009](adr/009-vm-start-under-ten-seconds.md)), the answer streams
     into the chat, the read-only terminal view replays;
  2. a whiteboard entry with notification; the session receives the content-free ping;
  3. GitHub: connect the account by device flow on the runner device, one operation on a test repository of the
     user, its duplicate refused;
  4. in a shared organization with platform access (S8): assign the platform device to a workgroup and run a task
     there;
  5. invite a second user into the organization (org invitation) and a customer by platform invitation (only if P2
     already admits external users); both sign in, see only what their rights allow;
  6. monitoring in the web UI (device → VM → session) and in Grafana;
  7. `agent-hub audit verify` exit 0, `agent-hub export --org <id>` of the test organization succeeds and the export
     restores into a scratch database (`rebuild-projections --check` without differences);
  8. a smartphone sign-in (passkey) shows the live task state.
- **Rollback:** a failing smoke test stops the cutover: either fix and repeat, or roll back S6 (unroute; data kept).

### S12 – Retire the legacy hub

See §7. **Who:** infrastructure operators, owner approval. **Check:** no legacy stack runs; legacy data backed up and
readable. **Rollback:** start the legacy stack again (§7).

### S13 – Open the platform to external users (if P2 was staged)

- **Who:** infrastructure operators, owner (written approval of P2 option b).
- **Do:** replace the staff-only access layer for the client and anonymous API paths as approved (exposure checklist
  complete, alerts active); keep the hub's own limits as the protection of the anonymous paths.
- **Check:** an invited external test user without a staff identity registers and works; alerts fire on account
  creation; rate limits answer `429` under a scripted burst from one address.
- **Rollback:** restore the staff-only access layer; registered external users keep their accounts but cannot reach
  the client until it is reopened.

## 7. Retiring the legacy hub

- **If the legacy hub is not running** at the cutover (offline or never brought up on the hub device), §7 reduces to
  keeping its last existing backups and data volumes as they are; the S6 rollback is then only unrouting.
- **Before S6:** take a final backup of the legacy hub's data (its SQLite file and a dump of the Team Record
  database) to the fleet's backup targets, following its existing backup classification.
- **At S6:** the legacy hub loses the production name. Its machine-path bypasses in the access policy are removed
  in the same PR as the new device and operator bypasses (P2).
- **After S11 (S12):** stop the legacy stack. Keep its VM or data volumes stopped and its backups for the retention
  the owner sets; the legacy Team Record stays readable from its own database (start the record database alone, or
  restore a dump into a scratch database) – it is not imported into the platform.
- **Legacy runners on developer machines stay untouched**: nobody uninstalls or reconfigures them. Their local
  store and local commands keep working; their sync to the legacy hub fails with retries from S6 on (the new hub
  answers 404 on the legacy paths). Developers who want to stop the retries unpair or stop their legacy daemon
  themselves. Platform use is by invitation (S8) and needs no legacy installation.
- **Rollback of the retirement:** start the legacy stack again from its kept VM/data; routing it under the
  production name again is the S6 rollback, routing it under another name needs the owner and an infrastructure PR.

## 8. Rollback

| Phase | Rollback | Data effect |
|---|---|---|
| S0–S4 | revert merges, discard keys and builds | none (nothing deployed) |
| S5 | delete the unrouted VM | none |
| S6 | previous ingress and access policy; legacy hub serves again | new hub keeps its (still empty or test) database |
| S7–S8 before other users | drop the database, repeat from S5 | first users re-register |
| S9 | decertify, `agent-runner uninstall` | the device's local data are deleted |
| S10 | revert the monitoring PR | none |
| S11 | stop and roll back S6 (unroute) or fix and repeat | test data kept |
| S12 | start the legacy stack again from its kept VM/data | none |
| S13 | restore the staff-only access layer | accounts kept |
| **After external users joined** | **no wipe and no return to the legacy hub as the system of record**: unroute the new hub (S6 rollback) while a fault is fixed, then route it again; restore from backups only per [runbooks/export-restore.md](runbooks/export-restore.md) | users keep passkeys and data |

**Point of no return:** the first passkey of a user outside the operator group. From then on the RP ID (§5), the
platform root key (§6 S2) and the hub key (§6 S3) cannot change without a new, empty platform; the rollback is to take
the platform offline temporarily, never to discard it.

## 9. Operation without data migration

- The platform starts empty: organizations, workgroups, tasks, whiteboards and credentials are created anew by the
  users after the cutover. Nothing from the legacy hub, the Team Record or legacy runner stores is copied, converted or
  imported; the two data models are incompatible by design (plaintext replica vs. end-to-end encrypted logs,
  [ADR-003](adr/003-zero-knowledge-hub.md)).
- Legacy history stays readable where it is (§7); a person who needs an old record reads it there.
- From the cutover on, the platform's own backups (§3.1) are the only copy of its hub data; runners keep local data
  that the hub does not have (plaintext operation parameters, journals) and are not backed up – they are rebuilt by
  a store wipe ([runbooks/export-restore.md](runbooks/export-restore.md#5-runner-store-wipe)).

## 10. Risks and open owner decisions

| Risk | Effect | Mitigation |
|---|---|---|
| Access policy of the production host blocks external users (risk M4) | invited customers cannot register | owner decision P2 is a precondition; staged opening S13 |
| RP ID changed after real passkeys exist | all passkeys unusable; identities without recovery code lost | §5 fixed before S8, written approval P7 |
| Loss or compromise of the platform root key | no new devices/inviters, or rogue certifications | two offline copies, [runbooks/platform-root-key.md](runbooks/platform-root-key.md#4-compromise-of-the-key); re-keying after a loss (runbook §5: new build, new empty platform) is described but **was never rehearsed** – no test or E2E runs it; it stays an unrehearsed residual risk until the rehearsal P11 |
| Restore from an export older than the runners' outbox retention (one hour; the daily export always is) | runner events pushed after the export and already pruned on the device are lost; each runner that lost items reports `outbox_items_lost` and keeps syncing | restore from the newest export; record the alerts in the incident, [runbooks/export-restore.md](runbooks/export-restore.md#3-restore) step 7 |
| Loss of the hub signing key | every device must re-enroll | offline copy (S3), important-class backup |
| PRF not available on users' platforms (ADR-006) | users depend on the recovery code | recovery code mandatory; manual PRF matrix rows done in S1 |
| Second hub process started by mistake | limits split, duplicate reminders | one `hub` service without replicas (§3.1) |
| Monitoring PR or runner-device role change delayed | runner running without fleet alerts | S10 may follow S9; hub-side monitoring in the web UI works without it |
| Legacy users surprised by the end of sync | support load | notice P12; legacy local use unaffected |

## 11. Day-of-cutover checklist

- [ ] P1–P12 approved/merged; rehearsal (P11) passed
- [ ] S0 release commits tagged; S1 suites green
- [ ] S2 root key created, two offline copies, fingerprint recorded
- [ ] S3 hub key and secrets stored as decided, offline copy of the signing seed
- [ ] S4 build hash reproduced and published with commit and root fingerprint
- [ ] S5 hub stack healthy, unrouted
- [ ] legacy hub backed up
- [ ] S6 production name routed; `/v1/config` and `/security` checked
- [ ] S7 platform log genesis
- [ ] S8 first users registered with confirmed recovery codes; inviters granted
- [ ] S9 runner device certified; doctor all `ok`
- [ ] S10 monitoring PR applied; series and rules visible
- [ ] S11 smoke tests 1–8 passed
- [ ] S12 legacy hub stopped, data kept
- [ ] S13 (when approved) external access opened
