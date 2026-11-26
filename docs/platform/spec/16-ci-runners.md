# 16 – CI runners: GitHub Actions jobs in throw-away VMs

Rules [E-34](../KONZEPT.md#e-34), [E-35](../KONZEPT.md#e-35), [E-36](../KONZEPT.md#e-36). Also touched:
[E-32](../KONZEPT.md#e-32) (nothing of agent runs on a user's computer except at most one Claude session; it excludes
work devices as pool devices, §10.2, D-25). Status: concept – not built; open decisions in §13.

This document is the complete concept for running the self-hosted GitHub Actions jobs of the DFX organisations
through agent.dfx.swiss, on the same devices as its work VMs ([E-35](../KONZEPT.md#e-35) and
[E-36](../KONZEPT.md#e-36); before E-36 the interpretation [A21](../KONZEPT.md#a-21)). Beyond these three rules and
the interpretation [A20](../KONZEPT.md#a-20) (one VM per job) it decides nothing: recommendations (such as the
trust classes of §5) are marked as such, and every choice is listed in §13 with its options and the factual tendency
of the research; every number that is not yet known is listed in §14. Sources are given in square brackets and
resolved in §16; repository paths are relative to `DFXswiss/agent`. "[OPS]" stands for the operator's infrastructure
documentation (an internal repository; the concrete assignment of devices, labels and repositories is kept there and
not repeated here).

## 1. Purpose and scope

### 1.1 What "all self-hosted runs" means
- Every job of a repository in a DFX organisation whose `runs-on` today names a self-hosted label runs, after the
  migration (§12), on a runner that agent.dfx.swiss starts for exactly one job (by the assumption of §3.2, any
  matching job GitHub assigns to it) in a fresh VM and deletes afterwards ([E-34](../KONZEPT.md#e-34), VM per job:
  [A20](../KONZEPT.md#a-20)). The host-bound jobs of §1.3 are not covered by A20: open deviation, decision D-1 ([KONZEPT part D](../KONZEPT.md#d-offen)).
- Organisations in scope: the DFX organisations on GitHub, each with its own installation of the CI app (§4). The
  exact list of organisations, repositories and labels is the inventory of stage 0 (§12) and is kept in [OPS].
- Public repositories documented with self-hosted CI in [OPS] include `DFXswiss/backend` and `DFXswiss/realunit-app`
  (where the golden tests of the latter run today is open, §8.5, D-17); their current runners and fork-PR settings are
  part of the stage 0 inventory ([OPS]). `DFXswiss/agent` itself uses GitHub-hosted runners only
  (`.github/workflows/platform-runner.yml:17,32-33,68`).
- GitHub-hosted jobs stay as they are; moving them is not part of E-34.

### 1.2 Job classes

| Class | Examples (by kind) | Runs in | Reason |
|---|---|---|---|
| Linux, portable | lint, unit and integration tests, PR hygiene checks, guards, Dockerfile lint | Linux CI VM (§7) | Needs nothing of the host |
| Linux, build | container image builds and publishing | Linux CI VM, trust class T (§5) | Needs Docker/Buildx and short-lived registry credentials only |
| macOS | iOS/macOS app builds, iOS simulator, golden (pixel) tests, script tests that need real macOS tools | macOS CI VM (§8), at most 2 per device | Needs Xcode or macOS/BSD tools |
| Privileged | infrastructure plans and applies, tunnel token handling | Linux CI VM, class P, last (§12) | Not host-bound in principle: state and provider credentials need a place and a credential concept first (§12 stage 7; current state: [OPS]) |
| Host-bound | deploy jobs in which a device sets itself up, drift checks of a device, reconcile/ensure jobs against one device, key generation and connector installation on a device | **No VM** – open deviation, decision D-1 ([KONZEPT part D](../KONZEPT.md#d-offen)); options: minimal deploy runner per device, signed pull deployment or another way (§13 D-1) | See 1.3 |

### 1.3 Why host-bound jobs cannot run in a VM
- A deploy job of this kind installs operator keys and user-session settings on the device itself [OPS]. A CI VM has
  no mounts and no forwards (`images/workvm.yaml:20,36-43`); it sees neither the home directory nor the launchd jobs
  of the host.
- A way back from the VM to the host (SSH) would break boundary B4 ("only the host initiates connections",
  [00 §3](00-overview.md#3-trust-boundaries)).
- The operator's model "every device deploys itself" is documented with reasons; an SSH fan-out from a central host
  was rejected [OPS].
- Consequence: these jobs cannot move into a CI VM. Today they run on a deploy runner outside the platform, which
  contradicts [E-34](../KONZEPT.md#e-34) and [E-35](../KONZEPT.md#e-35); [A20](../KONZEPT.md#a-20) does not cover
  them. Open deviation, decision D-1 ([KONZEPT part D](../KONZEPT.md#d-offen)): the options include a minimal deploy
  runner per device (class H, under the requirements of §5) as an exception and a signed pull deployment without an
  Actions runner (§13 D-1).

### 1.4 Linux and macOS share
- Most jobs are Linux jobs. The macOS share is probably small: most script tests inspected use stubs; only a few need
  real macOS tools (§8.5). **Assumption** (research 2): after moving the portable parts to Linux, a small macOS core
  suite remains.
- **To be measured:** the share, by running the whole script-test list once under Ubuntu (M-14, §14).

### 1.5 Known gaps of today's state
- Every label that a workflow requests needs an active host, and GitHub Actions must be enabled in the repository;
  stage 0 (§12) compares both with the current state, which is kept in [OPS].
- `--ephemeral` alone gives a fresh registration, not a fresh environment; a fresh environment per job needs the VM of
  [A20](../KONZEPT.md#a-20).

## 2. Terms

| Term | Meaning |
|---|---|
| Job | One job of a GitHub Actions workflow run; the unit GitHub assigns to one runner. |
| JIT runner | A runner registered with a just-in-time configuration (`generate-jitconfig`, §3); it needs no registration token and takes one job. |
| Ephemeral runner | A runner that de-registers automatically after one job (`--ephemeral`); a JIT runner behaves the same. |
| Scale set | A named set of runners with labels in one runner group, served by a listener over a long poll (`actions/scaleset`, ARC). |
| Runner group | GitHub's access unit for self-hosted runners in an organisation or enterprise: which repositories and workflows may use the runners. |
| Trust class | U, B, T, P or H (§5): what a job may see and reach. One class = one runner group = one pool. |
| CI profile | A golden image plus VM size for CI: `ci-linux-arm64` (§7) or the macOS CI image (§8). It differs from the session golden and the work image. |
| Pool device | An execution device of the operator (macOS, Apple silicon) in the one shared pool ([E-35](../KONZEPT.md#e-35), [E-36](../KONZEPT.md#e-36)): it runs session VMs, work VMs and CI VMs. A role, like "runner device" ([00](00-overview.md)). A device a customer adds is not a pool device. |
| Workload kind | What a VM is for: **interactive** (session VMs), **work** (work, test run and browser VMs, `13-work-vm.md`) or **CI** (CI VMs). "Agent VMs" are the first two. |
| CI controller | The process on a pool device that listens for jobs, starts VMs, hands over JIT configurations and deletes VMs (§6). |
| Markers | **Assumption**, **Unsure** and **To be measured** carry over the markers `Annahme:`, `Unsicher:` and `Messung nötig:` of the two research reports of 2026-11-25 unchanged in meaning; a marker without a research source is this document's own (e.g. the placement order of §6.6). |

## 3. GitHub mechanics

### 3.1 Documented

| Topic | Finding | Source |
|---|---|---|
| JIT | `POST /orgs/{org}/actions/runners/generate-jitconfig` or `/repos/{owner}/{repo}/…`; required: `name`, `runner_group_id`, 1–100 labels; answer carries `encoded_jit_config` | [GH-REST] |
| `--ephemeral` | De-registers after one job; GitHub recommends autoscaling with ephemeral runners | [GH-RUNNER-REF] |
| Registration token | Expires after one hour | [GH-REST] |
| Job acceptance | A job assigned to a runner that does not take it within 60 s is queued again | [GH-RUNNER-REF] |
| Queue | A job waits at most 24 h | [GH-LIMITS] |
| Job duration | Up to 5 days | [GH-LIMITS] |
| Runners per group | 10 000 | [GH-LIMITS] |
| Registrations | 1 500 per 5 min per repository/organisation/enterprise | [GH-LIMITS] |
| Orphaned runners | Removed after 14 days; ephemeral ones after 1 day without connection | [GH-REMOVE] |
| Logs | Runner logs of ephemeral runners must be saved externally | [GH-RUNNER-REF] |
| `workflow_job` webhook | `queued`, `waiting`, `in_progress`, `completed`; an app needs Actions: read | [GH-WEBHOOK-EVENTS] |
| Failed webhooks | No automatic redelivery | [GH-WEBHOOK-FAILED] |
| Webhook autoscaling | Possible, depends on timely delivery; GitHub points to ARC or the scale-set client | [GH-RUNNER-REF] |
| REST limit of an app installation | 5 000/h, scaling up to 12 500/h; Enterprise Cloud 15 000/h | [GH-RATE] |
| Secondary limits | 100 concurrent requests, 900 points/min; content-creating requests usually 80/min and 500/h | [GH-RATE] |
| Job-completed hook | Runs before the job ends; unsuitable for VM teardown | [GH-HOOKS-SCRIPTS] |

### 3.2 Not documented (found in none of the primary sources read)
- The lifetime of a JIT configuration.
- How long a registered JIT runner without an assigned job may wait; the 60 s apply only from assignment.
- An upper bound per organisation.
- Whether `generate-jitconfig` counts against the 500/h content-creation limit.
- **Assumption:** a JIT runner is not bound to a job ID (the request knows only name, group, labels, work folder), so
  GitHub gives it any matching job. Trust classes must therefore be separated by runner groups (and labels within a
  group), never by "the job for which the VM was started".
- **To be measured:** M-1 and M-2 (§14).

### 3.3 Runner versions
- Registration needs at least v2.329.0, jobs need a higher version; full enforcement on GitHub Enterprise Cloud from
  2026-09-29 [GH-VERSION-1].
- Every new release must be installed within 30 days; security updates can pause assignments at once [GH-VERSION-2].
- Contradiction: the runner reference named 63 days at retrieval [GH-RUNNER-REF]. Planning uses the stricter 30 days.
- **Unsure:** both changelogs say "GitHub Enterprise Cloud"; whether the same holds for Free and Team organisations on
  github.com is not stated explicitly.
- Check endpoint: `GET /orgs/{org}/actions/runners/deprecations/{version}` [GH-REST].
- Listed as latest: v2.338.2 of 26 October (year not shown), with a `linux-arm64` package [GH-RELEASES].
- Consequence (recommendation): pin the runner version in the image instead of auto-updating in each VM (auto-update
  costs start time), watch the deprecation endpoint, rebuild at the latest every two to three weeks (§7.4, §11.5).

### 3.4 Scale sets without Kubernetes
- ARC: the listener holds an HTTPS long poll, receives "Job Available", checks the target count and acknowledges;
  then JIT configuration and ephemeral runner are created; no webhook [GH-ARC].
- `actions/scaleset` [SCALESET]: Go client, **Public Preview** ("API is stable", interfaces and examples may change;
  release section empty at retrieval; Go ≥ 1.25). Scope repository, organisation or enterprise; app authentication
  preferred; own JWT provider possible (KMS/HSM). Poll held about 50 s, client timeout 2 min; unacknowledged messages
  are redelivered. Scaling follows `statistics.TotalAssignedJobs`, not single messages (at most 50 per answer). Jobs not
  taken are reassigned up to three times. Example outside Kubernetes: `examples/dockerscaleset`.
- GitHub names VMs and macOS as target platforms and several labels per set [GH-CHANGELOG-FEB].
- **Unsure:** no stable specification of the wire protocol was found; a Python reimplementation would couple to
  preview internals. Community ports (Rust, TypeScript) are not official.

### 3.5 Trigger options

| Option | Advantage | Drawback |
|---|---|---|
| **A** scale-set client as a Go sidecar | Official, outbound only, no webhook | Preview; second language next to the Python runner |
| **B** webhook + JIT | Public REST API | Inbound endpoint, lost deliveries (no redelivery), reconciliation still needed |
| **C** polling + JIT | Close to the operator's current feeder | API budget, latency, assignment races |
| **D** fixed JIT stock per pool | Simple | Holds RAM; waiting limit of an unassigned JIT runner unknown (§3.2) |

Factual tendency (research 1, not decided): A deviates least from the trust boundaries (outbound only, nothing at the
hub). Decision D-2 (§13).

## 4. Organisations, permissions, plans, cost

### 4.1 One app, one installation per organisation
- A GitHub App with one installation per organisation; per organisation its own installation token, runner groups,
  scale sets and API budget. The operator's current feeder already serves two organisations this way [OPS].
- Minimal permission: organisation level **Self-hosted runners: read/write**. Repository level would need
  Administration: read/write plus Metadata: read, which reaches much further; a classic PAT would need `admin:org` or
  `repo` [GH-ARC-AUTH], [GH-APP-PERMS]. Actions: read is added only for webhooks or job metrics.
- The CI app is **separate** from the existing user app of [ADR-015](../adr/015-github-app-device-flow-rest-only.md)
  (Contents, Pull requests, Issues; user-to-server tokens). Owner of the CI app and its key: decision D-4.
- **Unsure:** whether one of the operator identities on GitHub is an organisation or a user account [OPS]; a user
  account offers only repository-level runners (**Assumption**).

### 4.2 Runner groups and plan
- Additional organisation runner groups are documented from the Team plan; default access is private repositories
  only; policies restrict repositories and workflows [GH-ACCESS], [GH-GROUPS].
- The runner groups of §5 need the plan check of D-9 (the plans in use: [OPS]). Without additional groups only the
  default group exists, and public cannot be separated cleanly from private; the alternative is repository-level
  runners with the wider app permission, which cannot separate classes within one repository (D-9).
- Enterprise: groups can be shared across organisations and the scale-set client supports the scope. Whether DFX has
  an enterprise account is not documented; technically it is not needed.

### 4.3 Cost

| Date | Finding | Source |
|---|---|---|
| December 2025 | Announcement of USD 0.002/min for self-hosted from 2026-03-01; later postponed. The page names 15 December for the postponement and 16 December for the announcement (inconsistent) | [GH-PRICING-2026] |
| same | Public repositories were to stay free under that plan | [GH-PRICING-2026] |
| 2026-11-25 | Billing documentation: self-hosted "Free"; no new date. Postponed, not visibly cancelled | [GH-BILLING] |

- Still billable: artifact and package storage USD 0.25/GB-month; cache above 10 GB per repository USD 0.07/GB-month
  [GH-BILLING]. The billing page mentions different rates for Copilot code review on ARC runners; to be checked.
- Comparison: GitHub-hosted macOS USD 0.062/min [GH-RUNNER-PRICING]; GitHub-hosted runners are forbidden in the main
  infrastructure repository [OPS].
- Platform billing: CI VM runtime is a DFX resource ([Idee 46](../KONZEPT.md#k-idee-46),
  [Idee 47](../KONZEPT.md#k-idee-47)); internal exemption or an internal cost centre is decision D-13.
- **To be measured:** M-3 (minutes per organisation and month, the cost risk if the fee returns).

## 5. Trust classes

**Recommendation, not decided** (research 1 §4.2; class model: decision D-26, separation per class: D-20). Labels are
chosen by the workflow author and are therefore **no security boundary**. Every class has its own runner
group (with repository and workflow policies), its own scale set and its own VM pool; classes share no labels and no
writable cache. "VM pool" means separate warm VMs and slot accounts per class; the devices below them are the one
shared pool of [E-35](../KONZEPT.md#e-35) and [E-36](../KONZEPT.md#e-36) (§6.6).

| Class | Examples | Runner group | Secrets | Egress | Cache | Pool |
|---|---|---|---|---|---|---|
| **U** untrusted | every job that executes code not yet on a protected branch, public or private: fork PRs, same-repository PR tests, pushes to unprotected branches | own group, only the repositories that need it | none – a requirement on the repository, not something runner group, scale set or VM pool can enforce; without the preconditions of §9.6, U jobs from the same repository are **not** secret-free | strictest (decision D-8) | read-only (image pre-warm, read-only proxy); never writes a shared cache | own; recommended only on a pool device that has passed gate U (§9.3; open, D-7, D-20); never a slot account of another class or workload kind; whether U may run at the same time as T/P on one device: D-24 |
| **B** bot-trusted | the A38 guard, PR base hygiene checks | own group, base-branch workflows only | only the token the guard needs | runner minimum set, PyPI and setup-python downloads (or both pre-warmed in the image) | none | own |
| **T** trusted build | image builds on protected branches; tests on pushes to and scheduled runs of a protected branch | own group, protected triggers only | builds: short-lived registry credentials, environment protection where the plan offers it; tests: none | registries, package mirrors | writable layer cache per class and repository | own |
| **P** privileged | infrastructure plans and applies from protected branches, tunnel tokens | own group, protected workflow refs only; migrated last (§12 stage 7) | provider credentials after a credential concept (§12 stage 7) | runner minimum set plus provider APIs only | none | own |
| **H** host | deploy, drift, reconcile on a device | required: own group, only the one repository and its protected workflow refs, no PR trigger | only what the device's own jobs need | only what the device's own jobs need | – | **no VM** (§1.3) |

- Egress is in addition to the runner minimum set of §9.4; all egress values are subject to D-8. Class B: the guard
  installs Python with `actions/setup-python` and PyYAML with pip (`.github/actions/a38-guard/action.yml:51,64`).
- Class P: recommendation: until stage 7 these jobs run manually (the way back of stage 7), never on the host of a
  pool device; current state: [OPS].
- Class H on a pool device: the deploy runner is persistent and runs on the host, and neither a label nor a changed
  `runs-on` in a PR may reach it; hence the own group. If D-9 gives no groups, the recommendation for pool devices is
  the signed pull deployment (D-1 (c)) instead of a deploy runner. Both stay open (D-1, D-9).
- U jobs of private repositories are treated as hostile by default, like those of public ones; no class relies on
  fork-PR approval. Where a repository has no protected branch, every job that executes its code is class U.
- GitHub warns that environment reviews do not replace missing isolation [GH-SECURE].

## 6. Architecture

### 6.1 Components

| Component | Runs on | Task |
|---|---|---|
| CI controller (listener) | Pool device, own macOS account (**recommendation**, decision D-3) | One scale-set session per organisation and class; long poll to GitHub, outbound only; asks the VM manager for a VM per assigned job; requests the JIT configuration |
| Key signer | Pool device or a separate signer | Holds the CI app key; signs app JWTs for the controller; recommendation: the key never enters a VM or the hub (open, D-4) |
| VM manager | Pool device (runner area of `agent-runner`, reusing the work VM machinery) | Pool, APFS clone, slot account, pf anchor, start, handover, teardown, sweep (`13-work-vm.md` §6, §7) |
| Admission | Pool device, one per device for all workload kinds | CPU, RAM, disk and macOS VM budget of the device (§6.6); no VM of any kind starts without it |
| CI VM | Pool device | Linux (`ci-linux-arm64`) or macOS CI image; GitHub `run.sh` with the JIT configuration; one job |
| Hub | hub device | Optional: device status and content-free metrics only (§6.3) |
| Monitoring | Pool device textfile collector | Metrics of §11.2 |

### 6.2 Data flow and VM lifecycle

```
 GitHub (api.github.com, *.actions.githubusercontent.com)
    ▲  long poll / acknowledge / JIT request (HTTPS, outbound only)          ▲ job traffic of the runner
    │                                                                        │ (HTTPS, outbound, via egress path)
 ┌──┴──────────────────────────── pool device (macOS) ─────────────────────┐ │
 │ CI controller ──JWT──► key signer (CI app key)                          │ │
 │      │ 1 job assigned (class X)                                        │ │
 │      ▼                                                                 │ │
 │ admission: capacity was reserved before the job was taken (§6.6)        │ │
 │ VM manager: 2 take pool VM / APFS clone of the CI image                 │ │
 │             3 start in slot account, pf anchor fail-closed              │ │
 │             4 wait for boot, SSH over vsock                             │ │
 │             5 JIT config on stdin → run.sh (never argv, never cloud-init)│ │
 │      ┌──────────────────────────────┐                                   │ │
 │      │ CI VM: run.sh → one job ─────┼───────────────────────────────────┼─┘
 │      └──────────────────────────────┘                                   │
 │             6 run.sh exits (or wall-clock limit) → read _diag (bounded) │
 │             7 delete VM + disk + aux storage; remove GitHub runner      │
 │             8 sweep every minute: leftover VMs, rows `deleting`         │
 │ textfile metrics ──► monitoring      status (no content) ──► hub (opt.) │
 └─────────────────────────────────────────────────────────────────────────┘
```

- Pool: at most **one** warm, unassigned clone of a current CI image per device in total, not one per class, like
  the work VM pool (`13-work-vm.md` §7); it is taken for the class it was cloned for and refilled for the class the
  controller expects next. Its RAM counts in the device admission (§6.6) and it gives way first. A VM never serves
  two jobs.
- Handover: the JIT configuration goes after boot over vsock SSH on stdin, never in argv or cloud-init; the work
  image already forbids Lima parameters (`13-work-vm.md` §4, `images/workvm.yaml:3-5`). The client README demands:
  "Treat JIT configs as secrets until consumed" [SCALESET].
- Teardown: after `run.sh` ends, plus a wall-clock limit; the job-completed hook is unsuitable because it runs before
  the job ends [GH-HOOKS-SCRIPTS]. Deletion in a `finally` shielded from cancellation, as for test runs
  (`13-work-vm.md` §12.2); a failed deletion stays `deleting` and is retried by the sweep every minute
  (`13-work-vm.md` §7). CI VMs use their own name prefix, and the sweep also removes the GitHub runner registration.
- Reusable: APFS clone, warm pool, FIFO, deletion in `finally` (`13-work-vm.md` §7, §12.2). Slot accounts and pf
  (`13-work-vm.md` §6) are reusable only with the changes of §9.3 (accounts and their pf/wrapper/sudoers entries per
  job instead of a fixed installed set). Not reusable as is: the `runner.workvm` row requires `task_id`, `org_id` and `wg_id` (and `session_id` for the
  scope `session`) (`13-work-vm.md` §8); the session golden forbids Bash and git (`05-runner.md` §4); output through `workvm-job` is
  not needed for jobs (GitHub carries the logs) but useful for diagnosis.

### 6.3 Role of the hub
- Default (recommendation): the CI loop runs without the hub. The hub sees at most device status and content-free
  metrics (counts, durations, error classes), no repository names, no job payloads, no JIT configuration, no app key.
  This keeps [§5](../KONZEPT.md#k-5) and the rule that outside services are reached only from the execution device
  ([00 §1](00-overview.md#1-architecture-in-one-picture)).
- Alternative "hub": a webhook receiver or controller at the hub. The hub is publicly reachable
  ([E-24](../KONZEPT.md#e-24)), but would see repository and job metadata and need a webhook secret; this needs a
  KONZEPT change (metadata rule) before it may be built. Decision D-3.
- If the hub is down, sessions keep running and operations wait ([ADR-002](../adr/002-hub-as-coordinator.md)); the CI
  loop must not depend on it (§11.3).

### 6.4 Where the KONZEPT model needs a decision
A CI job is no user request and belongs to no workgroup ([Grundprinzip](../KONZEPT.md#k-gp),
[§2 Punkt 5](../KONZEPT.md#k-2-b5), [§16 Punkt 2](../KONZEPT.md#k-16-b2)); a VM per job ([A20](../KONZEPT.md#a-20)) differs from a VM per
workgroup ([§3 Punkt 5](../KONZEPT.md#k-3-b5)). Options: an infrastructure workload without task, or a technical
organisation/workgroup – decision D-12. An own product next to the platform is excluded by
[E-35](../KONZEPT.md#e-35). Job audit: which record a job leaves
([§6 Punkt 4](../KONZEPT.md#k-6-b4): every action with actor, time, task, session, workgroup, device, target, result
and changes – a CI job has an actor (the GitHub trigger), a time, a device, a target and a result, but no task, no
session and no workgroup; [§11](../KONZEPT.md#k-11); blocked attempts: [§10 Punkt 8](../KONZEPT.md#k-10-b8)); [E-09](../KONZEPT.md#e-09) (no monitoring inside
the work VM) is not carried over unchecked.

### 6.5 Trust boundaries

| Boundary ([00 §3](00-overview.md#3-trust-boundaries), [PLATFORM-DESIGN §3](../PLATFORM-DESIGN.md#3-trust-boundaries)) | Effect of CI |
|---|---|
| B1 browser ↔ hub, B7 agent tool ↔ hub | None |
| B2 runner ↔ hub, B3 hub ↔ internet | The hub sees neither app key, nor JIT configuration, nor job payload (default of §6.3) |
| B4 runner host ↔ VM | Holds: the host starts every connection; the VM holds no device key. New: the VM receives a JIT configuration, `GITHUB_TOKEN` and workflow secrets of its class – a new data class (§9.6) |
| B5 guest agent ↔ session | Not present in a CI VM (no guest agent, no session) |
| B6 execution device ↔ outside services | Holds with option A: GitHub is reached from the device |
| **New: GitHub / workflow author ↔ CI VM** | Workflow code is untrusted input from the author of the commit or PR. The platform's prompt and signature checks do not cover it; isolation (§9.3), classes (§5) and egress (§9.4) are the boundary |
| **New: CI VM ↔ agent VM on one pool device** | Both kinds run on one host ([E-35](../KONZEPT.md#e-35), [E-36](../KONZEPT.md#e-36): also CI jobs with foreign PR code); the host is the only thing between foreign workflow code and workgroup plaintext – a risk the client carries knowingly by E-36. The recommended gate U (open, D-7, D-20) and the separation rules of §9.3 |

### 6.6 Shared capacity
There is one pool ([E-35](../KONZEPT.md#e-35) and [E-36](../KONZEPT.md#e-36)): every pool device offers its capacity
to agent VMs and CI VMs, and none is kept for CI; CI jobs, also with foreign PR code, run on the same devices as the
work VMs, and an own CI device, even inside the platform, is excluded by E-36. Parameter names below are
placeholders where the runner does not have them yet; a value is named only where the specification already sets one.
Every other value is **to be measured** and then set (D-10, D-22).

**One admission per device.** Exactly one admission decides on a device for every kind of VM: workgroup VMs with
their sessions, work, browser and test VMs, and later CI VMs ([05 §3.8](05-runner.md#38-device-admission),
`agent_runner/admission.py`). It counts free RAM after `MEMORY_RESERVE_MIB`, the CPU load and the disk in one
transaction. CI plugs into this admission as one more kind; there is no second mechanism. No kind has a fixed number
or a fixed share, CI included: what is physically free decides ([ADR-075](../adr/075-resource-choice.md) addendum 2).
The client (2026-11-26): "wenn es nur ci runnerbedarf gibt, dann darf es auch 100% sein, ich glaube wir haben das
beste ergebnis wenn wir denen den vorrang geben die noch nicht viele plätze haben".

| Resource | Budget of a device | Counted per VM |
|---|---|---|
| RAM | Physical RAM − `MEMORY_RESERVE_MIB` (as today) | Configured memory, from `starting` until the VM is stopped or deleted; warm pool entries count |
| CPU | Host load per core below `CPU_LOAD_MAX_PERCENT` (as today, every kind) | Configured vCPUs |
| Disk | Volume quota per workload kind − reserve for image generations | Allocated disk or profile size, plus the pool entry reserve (`13-work-vm.md` §7) |
| macOS VMs | 2 (§8.4) | 1 per macOS VM of any purpose; Linux VMs of any kind 0 [APPLE-FORUM] |
| Slot accounts | Accounts installed per workload kind and class | 1 per VM that uses one |

- Today's RAM slots are no CPU budget: their vCPUs together may exceed the cores of the device (budgets per device:
  [OPS]). A CI VM with a constant full load changes the assumption behind that. The ratio of agent VMs next to CI load is
  **to be measured** (M-19, M-20).
- All checks and the reservation are one transaction, so that two requests never take the same capacity.

**No fixed shares.** `MIN_SHARE`, `MAX_SHARE`, `HEADROOM_INTERACTIVE`, `CI_ORG_SHARE` and `CI_CLASS_SHARE` do not
exist. With only CI demand, CI may use a device up to 100 %; so may every other kind alone. A CI VM is not
interrupted, so capacity it holds returns only at a job's end; that time is bounded by the wall-clock limit of the job
(§6.2). A macOS VM reduces the Linux capacity of its device by its vCPUs, RAM and disk like any other VM.

**Who goes first: the kind with the fewest active instances.** When room becomes free or several requests come at the
same time, the waiting request of the kind with the fewest active instances **on this device** goes first; on a tie
the older request. Kinds: sessions (workgroup VMs), work, browser and test VMs, later CI VMs. Example of the client:
80 % of a device run CI, then a new Claude session and a new CI runner arrive - the session goes first. A new request
may start only with the room that the waiting requests ahead of it leave (05 §3.8, reason `turn`); with room for all,
all start.
1. A running session, work VM job, test run or CI job is **never stopped silently** to make room. The existing stops
   (idle stop, credit, boundary failure, closed task) keep their reasons and stay visible. Only what is physically free
   is admitted: RAM after the host reserve, CPU, disk, at most 2 macOS VMs per Mac.
2. A session refused for lack of capacity keeps its place (its age) while the hub places its next attempt, at most a
   short hold; if it fits on another device, the hub may place it there.
3. What may give way, in this order: the warm pool entry of CI, other warm pool entries, idle agent VMs under the
   existing idle rule ([05 §3.7](05-runner.md#37-work-vm-slots-adr-059-addendum-4)): stopped, disk kept, started
   again on demand. Whether CI demand may trigger the last step: D-22.
4. A request that does not fit **waits; the budget is not exceeded**. CI jobs wait at GitHub (at most 24 h, §3.1): the
   controller takes a job only for capacity it has reserved, because an untaken job is queued again after 60 s (§3.1).
   How the scale-set client limits the offered capacity is **to be measured** (M-8). Work VMs keep their queue
   (`13-work-vm.md` §7); sessions keep `capacity_exhausted` and the new placement by the hub, with the clear message
   of [E-02](../KONZEPT.md#e-02).
5. Freed capacity goes to the waiting request of the kind with the fewest active instances, then the older one;
   within a kind its own queue keeps the arrival order.

**Fairness.**
- Between the GitHub organisations and between classes the same rule holds: the waiting job of the organisation or
  class with the fewest active CI VMs on the device goes first, on a tie the older; the oldest waiting age is the
  alarm (§11.2). GitHub's own assignment order inside a scale set is not controlled by the platform (§3.2).
- Between organizations of the platform: CI of the operator cannot crowd out the capacity customers pay for, because
  a kind with many active instances always goes after one with few.
- One wide workflow (matrix) counts as many jobs and as many active instances.

**Pool.** One warm VM per device in total (§6.2), not one per kind or class.

**Visible.** `agent-runner status` (`runner.status.admission kind= active= waiting=`) and the metrics
`agent_runner_admission_active{kind}` and `agent_runner_admission_waiting{kind}` show the active instances per kind
and the queue.

**Placement.** Deterministic, like [Idee 38](../KONZEPT.md#k-idee-38):
1. Filter: device online and not draining; image of the profile present; class allowed (for class U as decided in D-7/D-20, §9.3);
   D-24 respected; macOS profile only with a macOS counter below 2 and a fitting host version (§10.2).
2. Filter: CPU **and** RAM **and** disk of the profile fit, after the room the waiting requests ahead of it need
   (fewest active first).
3. Order: for Linux jobs, devices without use for a macOS profile first, so that macOS-capable capacity stays free;
   then the most free capacity of the scarcest resource; then the smallest device ID. **Assumption:** this order is
   good enough; packing large profiles (fragmentation) is **to be measured** (M-20).
4. Reserve, then start. If the device loses the reservation before the job is taken, the job returns to the queue.

The CI loop must work without the hub (§6.3), while agent VMs are placed by the hub from the capacity report. Both see
the same device admission, which has the last word; where the choice between devices is made for CI is D-23. The
capacity report must therefore show CI use, so that the hub does not offer capacity that CI holds
(`05-runner.md` §3.4, areas report through `USAGE`).

**Effect on interactive sessions.** CI load may slow sessions through CPU, memory bandwidth, disk, and the shared
egress path. Criteria, each measured without and with full CI share (M-19): assignment and cold start of a session VM
(target [§3 Punkt 6](../KONZEPT.md#k-3-b6)), time from prompt to first output without the model's share, hook and tool
call latency, `workvm_exec` start latency, CPU wait in session VMs, disk and egress throughput. The permitted deviation
is a parameter (`LATENCY_BUDGET`, D-22); the 20 % invariant of §11.1 concerns job runtimes and is not carried over
unchecked. If the budget is exceeded in operation, admission stops **new** CI VMs on the device; running jobs finish.

## 7. Linux CI profile

### 7.1 Image `ci-linux-arm64`
- Base: the pinned Ubuntu 24.04 arm64 cloud image of the work image (`images/workvm.yaml:12-15`), `vmType: vz`.
- Contents (recommendation): GitHub runner, version pinned, `linux-arm64` package [GH-RELEASES]; Docker engine and
  Compose (as in `images/workvm/provision.sh:65-68`) plus Buildx (not in today's package list, `13-work-vm.md` §4);
  a pre-warmed toolcache for the languages in use; git; sshd on vsock.
- Not included: Claude Code, the guest agent, `workvm-browse`, Playwright (unless a class needs it), any credential.
- Template settings as the work image: `mounts: []`, no port forwards, `ssh.overVsock: true`, no SSH agent, no
  `param`, host resolver off (`images/workvm.yaml`, `13-work-vm.md` §6.5).
- amd64: vz cannot run Intel guests on ARM [LIMA-VZ]; Rosetta is configurable [LIMA-TPL-2.3]. The need for amd64
  images is unknown (inventory, stage 0).

### 7.2 Sizes

| Size | CPU / RAM / disk | Source |
|---|---|---|
| standard | 4 / 8 GiB / 50 GiB | `13-work-vm.md` §2 |
| large | 8 / 16 GiB / 100 GiB | same |
| xlarge | 16 / 32 GiB / 200 GiB | same |

- The heaviest documented test run fits no current profile (maximum 32 GiB; its budget: [OPS]). Larger profile
  or splitting the job: decision D-11; keeping the current host is excluded by [E-35](../KONZEPT.md#e-35).
- The current large script-test job uses 16 workers and has its host to itself [OPS]; a 16-CPU VM does not reproduce
  that 1:1. **To be measured:** M-5.

### 7.3 Differences to the work VM and the session golden

| Topic | Session golden | Work VM / test run VM | CI VM |
|---|---|---|---|
| Purpose | Claude Code sessions | session commands, test runs ([E-08](../KONZEPT.md#e-08), [A9](../KONZEPT.md#a-9)) | one GitHub job |
| Shell and git | forbidden: no Bash, Landlock exec allow-list, no git (`05-runner.md` §4) | free as root | free (workflow steps are shell and git by nature) |
| Guest agent, Claude | yes | no guest agent | no |
| Credentials | model credential in the memory of the guest gateway (`05-runner.md` §10.2) and a control token handed over at boot (`05-runner.md` §3.2); none in the session sandbox | platform puts none in ([ADR-059](../adr/059-work-vm-on-request.md)) | JIT configuration, `GITHUB_TOKEN`, class secrets (§9.6) |
| Lifetime | per workgroup | per task/session or per run | per job |

Reason for no Landlock ban in the CI guest: [§9 Punkt 5](../KONZEPT.md#k-9-b5), [§10 Punkt 3](../KONZEPT.md#k-10-b3)
and [§10 Punkt 6](../KONZEPT.md#k-10-b6) bind Claude Code sessions; a CI job runs no Claude Code and is workflow code
by definition. [E-08](../KONZEPT.md#e-08) exempts only the work VM of sessions, so the CI guest needs its own
clarification in KONZEPT (decision D-12).

### 7.4 Build, pinning, provenance
- Build like the work image (create, provision, smoke, stop, rename with date and hash; `13-work-vm.md` §4). Smoke for
  CI: runner `--version`, `docker run hello-world` (online), toolcache present.
- Pinned today: Ubuntu image by digest (`images/workvm.yaml:13-15`), Node and Claude tarballs by SHA (`05-runner.md`
  §3.1). Gap: apt packages without versions (`13-work-vm.md` §4); no signature or reproducibility documented for the
  work image.
- Options for trust in the golden image: digest + SBOM; additionally signed; reproducible build – decision D-15.
- Rebuild at least every two to three weeks because of the 30-day version rule (§3.3), on every runner security
  release at once, with the previous generation kept as fallback (recommendation).

### 7.5 Start time
- Measured for the slim session golden (Lima 2.3.0, vz, 4 vCPU/8 GiB): pool assignment 0.10 s, clone + start
  4.3–6.0 s, cold start 8.1–11.5 s, `limactl clone` 0.04 s ([ADR-009](../adr/009-vm-start-under-ten-seconds.md)).
  Not valid for the CI image and the runner registration. **To be measured:** M-4.
- vz snapshots are not implemented in Lima v2.3.0 [LIMA-VZ-DRIVER]; about 1 GB host RAM per idle VM, returned only at
  stop ([ADR-009](../adr/009-vm-start-under-ten-seconds.md)).

## 8. macOS CI profile

### 8.1 Licence (note, **no legal advice**; review on the original text required)
Source: macOS 27 software licence agreement (EA2005, 07/11/2026) [APPLE-SLA], read through a text extraction;
section 2B(iii) is also quoted in [APPLE-FORUM].

| Passage | Wording (excerpt) | Meaning |
|---|---|---|
| 2B(iii) | "to install, use and run up to two (2) additional copies or instances of the Apple Software … within virtual operating system environments on each Apple-branded computer you own or control that is already running the Apple Software" | Two VMs in addition to the host, per Mac owned or controlled |
| 2B(iii) | "(a) software development; (b) testing during software development; (c) using macOS Server; or (d) personal, non-commercial use" | Closed list of purposes |
| 2B(iii), start | "except as otherwise provided in writing, signed, or issued by an authorized representative of Apple" | More only in writing; Apple DTS named no application route in the forum |
| after 2B(iii) | "does not permit you to use the virtualized copies … in connection with service bureau, time-sharing, terminal sharing, relay service or other similar types of services" (except section 3) | Services for third parties excluded |
| 2B(ii) | "If you are a commercial enterprise or educational institution … one (1) copy" | Own rule for enterprises |
| 3A | "Permitted Developer Services means continuous integration services, including but not limited to software development …" | CI is the leasing purpose of section 3 |
| 3 | "each lease period must be for a minimum period of twenty-four (24) consecutive hours" | 24 h minimum lease, sole use by the lessee, prior notice to Apple |
| 3D | "either a Lessor or a Lessee (but not both)" | Only one side may virtualise |

- Section 3 does not fit: per-minute jobs against a 24 h minimum lease with sole use.
- Enforcement: according to Apple DTS the framework limits macOS VMs (`VZMacPlatformConfiguration`) to two; Linux VMs
  have no fixed limit [APPLE-FORUM]. The error at the third VM is documented in [ECLECTIC-2022].
- Xcode licence: §2.2A allows "a reasonable number of copies … on Apple-branded computers that are owned or
  controlled by You", internal use only; §2.7 forbids among others hosting and renting [XCODE-SLA]. VMs are not
  mentioned: **Unsure** whether Xcode in a guest is covered. Images with Xcode are never distributed publicly.

| Case | Assessment (not legal advice) |
|---|---|
| DFX repositories on DFX-owned Macs, development and test | Plausibly covered by 2B(iii)(a)/(b) |
| Production deployments, continuous services in macOS VMs | Not covered by the purposes; excluded |
| RealUnit, 21gifts | **Open:** group relationship, payment and "own or control" decide whether this is own use or a service to third parties (D-16) |
| Fork PRs of external contributors to DFX repositories | **Assumption:** DFX tests its own software, so close to the licence; clearly more critical as a security question (§9.2) |
| Customers of agent.dfx.swiss on DFX Macs | High risk "service bureau"; section 3 does not fit per-minute jobs → **excluded until the legal review** (D-16, D-21) |
| A customer on its own Mac | The customer would be the licensee; to be reviewed separately |

Open legal questions (D-21): does 2B(iii) also apply to preinstalled macOS or to macOS installed by DFU/IPSW
(section 2A separates the cases with "unless you obtained the Apple Software from the Mac App Store, through an
automatic download"; the installation type of each device: [OPS])? The licence chooses Californian law
according to research 2; no Swiss clause was found – how this relates to DFX's own requirements is to be reviewed.
Is internal charging between group companies already a service?

### 8.2 Tools

| Tool | Licence | Cost 2 Macs | Cost 10 Macs | Notes |
|---|---|---|---|---|
| **Lima** | Apache-2.0 [LIMA-LICENSE] | 0 | 0 | macOS guests experimental since v2.1, ARM only, `vz` the only VM type, guest not newer than host [LIMA-MACOS]; already used by DFX for installation probes (`docs/platform/manual-checks/own-device.md` §1) |
| **Tart ≥ 2.33.0** | FSL-1.1-ALv2 © OpenAI since 2.33.0 (2026-07-17), each version Apache-2.0 after two years [TART-LICENSE], [TART-RELEASES] | 0 (**Assumption**) | 0 (**Assumption**) | "A Permitted Purpose is any purpose other than a Competing Use"; internal use allowed; "Competing Use" = commercial offer that "offers the same or substantially similar functionality as the Software" – relevant if the platform offered Tart VMs to customers (it sells usage by credit, [ADR-062](../adr/062-prepaid-credit-and-usage-billing.md)) |
| **Anka Build** | annual subscription per Apple-silicon host [ANKA] | 2 host licences, price on request | 10, on request | Anka Develop is free but limited to laptops and one VM, unsuitable for CI servers |
| **Orka / Orka Engine** | key required, price through sales [ORKA-PRICING] | on request | on request | Engine on-prem from macOS 15, headless VMs [ORKA-ENGINE] |
| **UTM** | Apache-2.0 [UTM-LICENSE] | 0 | 0 | Secondary for unattended CI; no advantage over Lima found |

- Tart cost: the former price page `tart.run/licensing/` answers 404; Cirrus Labs writes "We have also stopped
  charging licensing fees for them" [CIRRUS]. The old licence (Fair Source 0.9, 100 CPU cores) applies according to a
  secondary source [CLODPOD-88] still to 2.32.1.

### 8.3 Technique
- Image from Apple's restore image (IPSW): load IPSW, create `VZMacPlatformConfiguration` and auxiliary storage, run
  `VZMacOSInstaller` [APPLE-INSTALL]. Lima's template uses `UniversalMac_27.2_26B123_Restore.ipsw` with its SHA-256
  [LIMA-MACOS-TPL]; the digest is pinned (recommendation).
- From macOS 27, `VZMacGuestProvisioningOptions` sets user, password, auto-login and SSH, only at the first start
  after the restore [APPLE-PROVISIONING]. Tart offers it since 2.33.0 (`--provisioning-opts`); in Lima the switch is
  open [LIMA-5516]. Lima 2.3 (2026-11-06) can suppress the first-login assistant [LIMA-RELEASES-API].
- Lima limits for macOS guests: the video display cannot be switched off [LIMA-4614] (open), no automatic port
  forwarding, no own CA certificates, no passwordless `sudo`; the password lies in the guest at `~/password`
  [LIMA-MACOS].
- DFX adjustment: the own-device package removes Lima's macOS guest agent ([HOT-20](../decisions/HOT-20.md)); a
  pool device with macOS VMs needs a Lima build with it. Work and golden templates are Ubuntu only (`13-work-vm.md`
  §4).
- Homebrew: template `homebrew-macos` [LIMA-BREW]. Ready Tart images exist as vanilla, base, xcode, runner
  [TART-IMAGES].
- Size: Xcode about 30 GB, DerivedData 20–30 GB, simulators 50+ GB [OPS]; a Tart base image is about 25 GB download
  [TART-README]. Tart has stacked disks with a shared base from macOS 27 [TART-FAQ].
- Clone per job: APFS clone, as for Linux. The Ubuntu figures (clone 0.044–0.046 s, boot 5.77–5.80 s,
  [M0](../measurements/M0.md)) do **not** apply to macOS. **To be measured:** M-10.

| Limit in the macOS guest | Finding |
|---|---|
| Nested virtualization | Only for Linux guests [TART-FAQ]; GitHub names the same Apple limit for arm64 runners [GH-HOSTED]; confirmed by DFX ([KOR-59](../decisions/KOR-59.md) point 14) |
| Docker | Consequence: no local Linux VM, so no usual Docker service in a macOS guest; GitHub requires Linux for container actions anyway [GH-RUNNER-REF] |
| GPU/Metal | Paravirtualised and limited [ECLECTIC-2023]; behaviour of the iOS simulator: **To be measured** (M-11) |
| iCloud / App Store | iCloud from host and guest macOS 15 on a fresh install; a second concurrent clone gets a new identity [APPLE-ICLOUD]; App Store apps do not run according to a secondary source [SCALENINJA]. No Apple ID for CI |
| Network | NAT by default; bridging needs the entitlement `com.apple.vm.networking` [APPLE-BRIDGE] |
| USB | Pass-through in Apple's macOS 27 sample [APPLE-RUN-MACOS]; off for CI |
| Start without login | Tart: since macOS 15 an unlocked login keychain is needed [TART-FAQ]. Whether this also affects Lima as a LaunchDaemon: **To be measured** (M-12) – open point |

Care: update the host before the guest; rebuild the image on every macOS, Xcode and runner security release, and at
the latest every two to three weeks (§3.3), with digest and fallback version (recommendation, research 2 §3).

### 8.4 Enforcement of the licence limit
- Hard counter: at most **2 macOS VMs per pool device**, counting **every** macOS VM – CI jobs, image builds and
  installation probes (recommendation). The framework enforces the same limit (§8.1); the counter avoids a third VM
  failing at start and blocking jobs.
- The limit holds per device of the shared pool. Linux VMs, so also all agent VMs, do not count: the framework limits
  macOS VMs only, and Linux VMs have no fixed limit [APPLE-FORUM]. They still use CPU, RAM and disk (§6.6).
- Placement must know the counter (§6.6): the counter is part of the device's admission, not of the CI controller,
  so that an image build or probe cannot pass it by.
- A slot reserved for image builds lowers job parallelism temporarily to 1.
- Register of all macOS VMs per Mac (licence way 2B(iii), research 2 §7).
- Parallelism: 2 per pool device, i.e. 4 with two and 20 with ten devices, if the device's shares (§6.6) allow it.

### 8.5 Which jobs really need macOS

| Kind | Finding |
|---|---|
| Script tests of the macOS fleet | Only partly: the current job is on its large label for its size (16 workers), not for macOS [OPS]. Real macOS/BSD needs (`/usr/bin/plutil`, `stat -f` without GNU alternative) in a sample of five tests; several tests are portable or stub `launchctl`, `diskutil`, `plutil`, `sysctl`, `vm_stat`; one test needs Docker and aborts in CI without it [OPS]. The list is **not complete** (BSD `sed`/`date`, bash 3.2) |
| Deploy, drift, connector installation | Change or inspect the concrete host (`launchctl`/`plutil`) → host-bound (§1.3), not in a VM |
| A main-suite alarm job | Uses `/usr/bin/lockf` [OPS]; replaceable, no real macOS need |
| `DFXswiss/agent` `platform-runner.yml` | Matrix with `macos-latest` (`.github/workflows/platform-runner.yml:32`), only for one branch (`:6-10`); GitHub-hosted, public repository (`.github/a38.json:92`) |
| `DFXswiss/realunit-app` golden tests | Current location to be clarified ([OPS], D-17) |
| RealUnit app tier 3 (iOS simulator, Maestro) | Needs Xcode and simulator runtime [OPS]; **Unsure:** today's workflow, repository not checked out |
| iOS simulator, `flutter build macos` in general | Need full Xcode |

- Goldens: their pixel equality depends on fixed hardware [OPS]; in VMs the baselines must be regenerated and checked
  for stability.
- The script-test job does not run unchanged in a macOS VM (it needs Docker, which a macOS guest cannot run); the job
  must be split.
- Recommendation: move the Linux-capable parts to Linux first (the four `stat` places are small changes); keep a small
  macOS core suite, because a Linux-only run would hide differences to the macOS fleet. **To be measured:** M-14.

### 8.6 Recommendation (not decided, D-19)

| Point | Recommendation |
|---|---|
| Tool | **Lima**, matching the existing runner and licence model; version and IPSW digest pinned |
| Alternative 1 | **Tart**: more mature for macOS CI, Softnet, stacked disks; price: second tool, FSL, keychain question |
| Alternative 2 | Anka or Orka Engine if support is needed; request an offer |
| Licence way | 2B(iii), DFX repositories only, purpose development and test, register of all VMs per Mac |
| Allocation | Hard counter 2 per device, all macOS VMs count |
| Hardware | Pilot on one pool device with two VMs; then a second device for failure and maintenance (§10.2) |

Order: (1) Linux run of the script tests, (2) split the job, (3) legal review, (4) Lima spike, (5) pilot with one
trusted repository, (6) decision on Xcode and RealUnit.

The recommendation flips (research 2 §7):
- to **Tart**, if spike criterion 1 or 2 (M-12, M-16) fails with Lima, Tart passes it, and FSL is cleared;
- to **GitHub-hosted**, if only the public `DFXswiss/agent` repository needs macOS;
- to **Anka/Orka**, if support or central management matters more than cost;
- to a **dedicated Mac without VM**, if goldens, simulator or real devices are not stable in VMs – no longer available
  without a new rule of the client: it contradicts [E-35](../KONZEPT.md#e-35) (no own machine) and deviates from the
  interpretation [A20](../KONZEPT.md#a-20) (VM per job); such jobs would be reported as not movable;
- to **no macOS VM at all**, if almost nothing remains after the split;
- to **stop**, if the legal review denies 2B(iii) for the installation type.

## 9. Security

### 9.1 GitHub guidance [GH-SECURE]
- Self-hosted runners should "almost never" be used for public repositories; private repositories are affected too
  when someone can fork and open PRs.
- JIT reduces the risk but replaces no clean environment; reused hardware can leak data.
- `pull_request_target` must not check out foreign code.
- Runner groups limit the damage.
- Fork approvals that wait more than 30 days expire [GH-FORK-APPROVE].
- Public fork PRs: not self-hosted, only after A38 approval in class U, or all in class U – decision D-6. Whether the
  former way (fork PRs of the backend on GitHub-hosted runners [OPS]) is still allowed under DFX's "no hosted" rule is
  part of D-6.

### 9.2 Fork PRs and the A38 guard

| Required today (`docs/a38-guard.md`) | Consequence for the service |
|---|---|
| The guard never checks out PR code (`:3`) | Own pool B, separate from U |
| Approval only for the first held `pull_request` runs at the current head/merge commit, after the report and the review rule (`:154-159,178`) | The service **never approves** runs itself |
| A held run is `completed/action_required` (`:178`) | Only actually assigned jobs start; never a start from run lists |
| Approval is no test result (`:156,182`) | Monitoring separates "approved" from "green" |
| Head/base are checked again before the POST; a residual race remains (`:184`) | VM isolation must hold even after a wrong approval |
| Same-repository PRs get no fork approval (`:159`) | Approval is no general protection |
| Workflow changes are policy migrations (`:124,130`) | Label changes need maintainer approval, except a guard-docs change set that touches only `.github/workflows/a38-guard.yml` and/or markdown (`docs/a38-guard.md:124`); the pool B switch therefore needs its own review |

### 9.3 Guest → host isolation
- Present: slot account per VM, pf anchor fail-closed, no mounts and no forwards (`13-work-vm.md` §6.1, §6.2, §6.5).
- Known limit: the per-account pf filter does not attribute every packet; a program in a VM can reach, with single
  connections, the device's loopback services, the site network and – while the Cloudflare client is disconnected –
  the internet with the site address ([13 §6.6](13-work-vm.md#66-known-limit-of-the-per-account-filter)). The
  operator accepted this for work VMs on 2026-11-24.
- Consequence for class U: foreign PR code runs on the devices that hold workgroup plaintext
  ([E-35](../KONZEPT.md#e-35), [E-36](../KONZEPT.md#e-36)), as far as it runs self-hosted
  ([E-34](../KONZEPT.md#e-34); public fork PRs: D-6). The research's way out, an own CI device separated from
  workgroup plaintext, is excluded by E-35 and E-36 (D-5), also inside the platform: this co-residency is the client's
  decision and a risk carried knowingly (§15). What that separation would have given can only come from the boundary
  of the VM. **Gate U** (recommendation, not decided: D-7,
  D-20) – class U should run on a pool device only after all of:
  1. a boundary by **interface** instead of the per-account filter with its known limit, built for the CI VMs of the
     device;
  2. `networkOpts.hostLoopback: false` in the CI templates (Lima 2.3, vz only, not with `user-v2`; it does not protect
     the site network, hence point 1 [LIMA-TPL-2.3]). `images/workvm.yaml` does not set it and requires only Lima 2.2.0
     (`:9`);
  3. the attack tests M-6 and M-21 passed on this device (and M-18 for macOS guests). These tests themselves run with
     the platform's own test code and agent VMs of a test workgroup only.
- Gate U goes beyond the boundary of today's work VMs; whether it is ordered is the client's decision
  ([E-25](../KONZEPT.md#e-25), D-20), and accepting the known limit stays an open option for every class (D-7 (a)).
  **Proposed** default: class U does not start before D-20 is decided. The recommendation does not cover classes B, T
  and P; whether the boundary also applies to them is D-7.
- How the interface boundary is built (per-VM interface, VLAN, separate physical network) is D-7. Tart's Softnet filters
  privately addressed traffic but allows the vmnet gateway and needs root at start [SOFTNET].
- Escape from the VM (hypervisor bug) is out of scope of the platform; damage is limited to the slot account
  (`docs/platform/security/work-vm-threat-model.md`). On a shared device that account lies next to agent VMs and the
  runner; this residual risk follows from E-35 and E-36 and gate U does not remove it.
- The same threat model (`docs/platform/security/work-vm-threat-model.md:35`) adds: since HOT-18 the Lima `_config`
  belongs to the slot account, so such an escape can also leave an `override.yaml` for that slot's later VMs. Slot
  accounts are reused between jobs within a class; an escaped class U job could thus influence later U jobs in the same
  slot, also of other repositories. `_config` is not the only carrier: an escaped job controls the whole slot account –
  its home `/var/agent-workvm/<account>` (0700, `13-work-vm.md` §6.1) and **every** state a macOS account can hold
  outside it. Examples, expressly not exhaustive: its crontab (`/usr/lib/cron/tabs/<account>`) and `at` jobs, jobs in
  its launchd user domain (`user/<uid>`, via `launchctl submit` or `bootstrap`), its folders under
  `/private/var/folders/…`, files it owns in `/tmp`. Such state can restart processes after the account's processes
  were killed and survives recreating the home; state bound to the account name also survives recreating an account
  of the same name. The state of an account cannot be enumerated conclusively, so no list of items to clean is
  complete. The work VM boundary stops slot processes only when the pf hook is lost (`13-work-vm.md` §6.3,
  `:192-193`), not between jobs. A root-owned or read-only `_config` is no remedy: `limactl start` then fails with
  `_config/networks.yaml: permission denied`, which is why HOT-18 replaced the root-owned `_config`
  (`docs/platform/decisions/HOT-18.md` point 5, `src/agent_runner/workvm/accounts.py:21-24`). **Recommendation**
  (part of D-20, not decided): the CI accounts have two mandatory properties; how they are achieved is left to the
  build.
  - **(a) Nothing of a CI job carries over** into a later job or onto the device: neither processes nor files nor any
    other state of the account the job ran under (examples, expressly not exhaustive: the home with Lima `_config` and
    `override.yaml`, crontab and `at` jobs, the launchd user domain, `/private/var/folders/…`, `/tmp`). Because the
    state of a macOS account cannot be enumerated conclusively, the obvious implementation is an own account per job
    that is torn down completely afterwards; a root-owned `_config` is excluded by HOT-18 (see above).
  - **(b) Every account is covered by the work VM boundary before its first process**: pf rules per UID, wrapper and
    sudoers allow-list (`13-work-vm.md` §6), fail-closed; at teardown these entries are removed.
  - How this is built (creating and deleting accounts, port assignment, parallelism, teardown without blocking work VM
    allocations) is open and part of D-20. The recommendation sets no ordering that restricts parallel CI VMs or work VM
    allocations.
- Separation between agent VMs and CI VMs on one device:
  - Slot accounts, Lima homes and pf rules are separate per workload kind and, within CI, per class; an account is
    never reused across them.
  - Nothing writable is shared: no cache, volume, package proxy store, Docker layer store or warm VM (§9.5). CI images
    and the work image are separate images; a clone shares blocks only with its own read-only image.
  - Side channels (shared CPU caches, memory bandwidth, timing of disk and network) remain between VMs of one host. No
    countermeasure was researched; the effect is **unsure** and the risk is listed in §15. D-24 covers co-residency
    of classes, not of workload kinds, which E-35 and E-36 set.
  - Exhaustion: CI has quotas for CPU, RAM, disk and slot accounts (§6.6), its own APFS volume with quota, a wall-clock
    limit per job, and a limit on egress. Work VMs have no bandwidth limit today (`13-work-vm.md` §6.2), so one is
    needed for CI (`CI_EGRESS_LIMIT`, to be measured, M-7).
- macOS guests: no shared folders, no SSH agent, no clipboard, no USB, no Docker socket; SSH over vsock for macOS
  guests: **To be measured** (M-13).

### 9.4 Egress
- Present for work VMs: only through the device's Cloudflare client, private ranges and port 25 blocked
  (`13-work-vm.md` §6.2, §6.3); no destination list, no bandwidth limit (`13-work-vm.md` §6.2).
- Minimum set for runners: GitHub API, `*.actions.githubusercontent.com`, `codeload`, results and blob storage,
  release and package domains [GH-RUNNER-REF]; GitHub requires at least 70 kbit/s and port 443 outbound
  [GH-RUNNER-REF]. Pure IP lists do not fit these wildcards (conclusion of research 1).
- Only free Cloudflare plans come into question: [E-10](../KONZEPT.md#e-10) excludes paid plans also for other
  purposes, so CI VMs too. Open is only whether E-10's rule on outgoing addresses (shared provider addresses only)
  applies to CI VMs, or an own CI rule, or a domain allow-list through a proxy, and the destination list: decision
  D-8. Volume and IP allow-lists of target services
  are to be checked; the tunnel MTU is 1300 (`13-work-vm.md` §6.3). **To be measured:** M-7.

### 9.5 Caches

| Variant | Assessment |
|---|---|
| GitHub cache | Scope rules exist (PR caches limited to the merge ref), content unsigned and unchecked; low-trust triggers can poison [GH-CACHE]; egress volume |
| Pre-warmed in the image | Safest local way: read-only per release |
| Read-only pull-through proxy (npm, apt, registry) | Fast; needs a network exception at the outside boundary |
| Writable layer cache per class and repository | Only T; never U → T |
| Shared writable volume | Rejected |
| Any cache shared between agent VMs and CI VMs | Rejected, also read-only from the other side (§9.3) |

Today there is no dependency cache across runs (`13-work-vm.md` §12.2). Comparison from the former host: `npm ci`
1.0 min hosted, 0.6 min in steady state [OPS].

### 9.6 Secrets per class
- The platform puts no credential into work VMs ([ADR-059](../adr/059-work-vm-on-request.md)); CI hands over the JIT
  configuration, `GITHUB_TOKEN` and workflow secrets – a new data class.
- U: never secrets – as a **requirement on the repository**, not as a property of the runner. GitHub withholds
  secrets only from fork PRs; workflows of PRs from the same repository and of pushes to unprotected branches receive
  repository and organisation secrets and a `GITHUB_TOKEN` with the default permissions [GH-SECRETS], [GH-SECURE].
  Runner group, scale set and VM pool (§5) cannot prevent that; the VM receives `GITHUB_TOKEN` in any case (B4,
  §6.5). Preconditions per repository that runs U jobs:
  - secrets only in environments that are bound to protected branches;
  - organisation secrets only for the repositories that need them;
  - `GITHUB_TOKEN` read-only by default ("Workflow permissions"), write rights only per job where needed.

  Without these preconditions, U jobs from the same repository are **not** secret-free. Whether the plan offers
  environment protection for the repositories concerned is part of D-9; ordering and checking the preconditions is
  part of D-20; the current settings per repository are part of the stage 0 inventory (§12.2).
- B: only what the guard needs. T/P: only with environment protection where the plan offers it;
  short-lived credentials. Signing keys never in PR VMs; signed releases on a separate way.
- The token that creates JIT runners needs organisation admin for runners [GH-REST]; it stays with the controller /
  key signer; only `encoded_jit_config` enters the VM.
- Recommendation (open, D-4): the app key never lies in a VM or at the hub; a separate account, better an external
  signer (§6.1).

### 9.7 Supply chain
- Manipulated runner, apt or image packages: SHA pins, SBOM, signature, staged rollout (D-15).
- `actions/scaleset` is a preview: pin the version, keep an adapter and a JIT fallback (§15).

### 9.8 Logs without personal data
- [OPS] rule: personal data of DFX customers stays on the designated production system; no log line, metric, trace,
  alert or test data outside it.
- Actions logs go to GitHub; fixtures must be free of personal data; masking is only a safety net under the same
  rule.
- Read `_diag` before deletion, bounded, encrypted and kept briefly. Default telemetry: job ID, duration, exit,
  error class, no step content. No repository, branch or user names as unchecked metric labels.

### 9.9 Hardening needs an order ([E-25](../KONZEPT.md#e-25))
Protection layers beyond what was ordered are built only after the client decided them. The measures of this
section that go beyond today's work VM (interface boundary, `hostLoopback: false`, separate network, egress
allow-list, signed images, separate key signer) must be ordered explicitly by the client: decision D-20. The first two
are the build parts of the recommended gate U (§9.3); nothing is built before the client orders it. The fork-PR
protection protects third parties and is justified as such under [E-11](../KONZEPT.md#e-11).
[A16](../KONZEPT.md#a-16) (workflows run with the rights of the connected account) needs a sentence on the boundary to
host and CI.

## 10. Hardware requirements

This section describes what a **pool device** must bring for both workloads, agent VMs and CI VMs (§6.6). Devices are
named by role. The concrete assignment to the fleet is kept in the operator's internal infrastructure repository.

### 10.1 Every pool device

| Resource | Requirement |
|---|---|
| CPU | Budget for both workloads (§6.6). CI: no overcommit (the operator's CI policy forbids it [OPS]) or a measured quota per class (D-10). Arithmetic only: without overcommit a device carries at most one standard CI VM (4 vCPU) per four cores, fewer after the host reserve and the agent share; the sustainable mix is **to be measured** (M-5, M-9, M-19, M-20) |
| RAM | Host reserve (default 32 GiB, [ADR-009](../adr/009-vm-start-under-ten-seconds.md)) plus the agent VMs plus the CI profile sizes of §7.2; RAM figures are no CI capacity: the RAM slots are for both workloads together (budgets per device: [OPS]) |
| Disk | Golden, work and CI images with two generations each, warm-pool clones of both workloads, Docker layers per running VM; 50 GiB reserve per pool entry and disk admission against a volume quota (`13-work-vm.md` §7); one APFS volume with quota per workload kind (`13-work-vm.md` §6.1) |
| Isolation | Work boundary installed (`13-work-vm.md` §6), CI slot accounts apart from the work slots; for class U the interface boundary of gate U is recommended (§9.3; open, D-7, D-20) |
| Enrollment | Enrolled and certified as execution device ([05 §2.1](05-runner.md#21-enrollment), [device certification](../runbooks/device-certification.md)); no other runner service on it except, if D-1 allows one, the deploy runner of §1.3, and that only in the group of class H (§5); without groups pull deployment is recommended (D-1 (c), D-9) |
| Network | Egress path of §9.4; parallel image pulls through the tunnel (M-7) |
| Continuous operation | Restart without login (LaunchDaemon), no sleep (as for runner devices) |
| Nested virtualization | Not needed: Docker runs natively in the Linux guest. If ever needed: vz on M3 or newer with macOS 15 or newer, off by default [LIMA-TPL-2.3]; **Assumption:** the current Apple-silicon generation of the fleet meets this |

### 10.2 Pool device with macOS VMs

In addition to §10.1; a pool device without these properties carries Linux VMs only.

| Resource | Requirement |
|---|---|
| CPU / RAM per VM | **To be measured**; comparison GitHub M1 runner 3 cores, 7 GB [GH-HOSTED]. Pilot value (**Assumption**): 4–5 vCPU, 16 GiB, 100 GiB logical |
| Parallelism | At most 2 macOS VMs per pool device; Linux VMs do not count (§8.4) |
| Disk | IPSW, two image generations, clones; with Xcode and simulator about 100–110 GB (from the DFX figures of §8.3) |
| Network | Same rule as Linux aimed for; at least 70 kbit/s and port 443 outbound [GH-RUNNER-REF] |
| macOS version | Host ≥ guest [LIMA-MACOS]; Lima's current guest image is 27.2 [LIMA-MACOS-TPL]. A device below that version is updated first (versions per device: [OPS]) |
| Continuous operation | Restart without a person, no sleep; settings per device: [OPS]. With FileVault, auto-login is not available [APPLE-FV-AUTOLOGIN]; unlocking over SSH from macOS 26 is possible [APPLE-FV-DEP] but an intervention. Start without GUI login: M-12 |
| Power | **To be measured** at the mains under two VMs (M-15) |

Requirements by device type. Data, versions and budgets of the concrete devices are kept in [OPS]; which of the
operator's machines become pool devices at which time is D-25:

| Device type | Requirement |
|---|---|
| Pool device for Linux VMs | §10.1; RAM above the host reserve for at least one standard profile (arithmetic: 32 + 8 = 40 GiB), otherwise it has no VM capacity |
| Pool device with macOS VMs | In addition this section; RAM for the pilot value of two macOS VMs above the host reserve (arithmetic: 32 + 2 × 16 = 64 GiB) plus the agent share; the Linux capacity they cost is a question of shares (D-22) |
| Pool device that already runs sessions | CI only after M-19 and within the shares (§6.6) |
| CI machine that changes into the pool | Host version ≥ guest (macOS VMs), runner services removed, set up anew before it holds workgroup plaintext (§12.3); heavy jobs: D-11 |
| Machine with another role (production services, hub) | Not assessed here; D-25 |
| Work device (a user's computer) | Excluded by [E-32](../KONZEPT.md#e-32): nothing of agent runs on it except at most one Claude session; no pool device |

### 10.3 The pool as a whole
- A profile that only one pool device can run (macOS, large sizes, class U if gate U is ordered) has a
  single point of failure; a second such device for failure and maintenance (recommendation, §8.6).
- A failing device takes agent VMs and CI VMs together; how much free capacity the rest of the pool keeps for that is
  a parameter (`POOL_FAILURE_RESERVE`, D-22).
- The research's argument for an own CI device (separation from workgroup plaintext, research 1) is rejected by
  [E-35](../KONZEPT.md#e-35) and [E-36](../KONZEPT.md#e-36) (D-5); E-36 also excludes an own CI device inside the
  platform. The recommendation in its place is gate U (§9.3; open, D-7, D-20).

## 11. Operations

### 11.1 Capacity model
- The model for both workloads is §6.6. Today the runner capacity counts RAM only: `vm_slots_total =
  floor(budget_mib / VM_MEMORY_MIB)` ([05 §3.4](05-runner.md#34-capacity)); the capacity report may carry `cpus`
  (optional, same section). The report needs CPU, disk, the macOS counter and the use per workload kind.
- Admission needs a CPU budget: vCPU per workload kind, class and device, no overcommit or a measured quota (D-10).
- The operator's acceptance invariant "at most 20 % runtime deviation under load" [OPS] is the acceptance criterion;
  whether it applies unchanged to VM pools is part of D-10 (M-9).
- Per class: pool size, parallel jobs, oldest waiting age; macOS: the counter of §8.4.

### 11.2 Monitoring

| Metric | Purpose |
|---|---|
| Assigned/running jobs, oldest waiting age per organisation and class | Queue |
| Pickup p50/p90/p95 | Replaces the operator's current pickup metric [OPS] |
| Duration, conclusion, infrastructure errors separately | Error rate |
| Clone, boot, registration and deletion duration | Platform |
| VM without listener, age above the job limit, age in `deleting` | Hangs |
| CPU wait, RAM, disk, swap, egress, each per workload kind | Capacity |
| Share held, borrowed and waiting per workload kind; refused or waiting session starts while CI holds capacity | Displacement (§6.6) |
| Session latencies of §6.6 with the CI share held at that time | Effect on sessions |
| Runner version, deprecation date, app API budget | GitHub duties |
| macOS VM count per device (0–2) | Licence counter |

Present: 15 s sampling and textfile (`05-runner.md` §14, sinks in `08-monitoring.md`) and the work VM series
(`13-work-vm.md` §10). No repository, branch or user names as unchecked labels.

### 11.3 Failures

| Case | Today / derived | Required |
|---|---|---|
| Hub down | Sessions keep running, operations wait ([ADR-002](../adr/002-hub-as-coordinator.md)) | CI loop independent of the hub; status delivered later |
| Runner process restarts | Every work VM is deleted (`13-work-vm.md` §7, §12.2) = job lost | Drain (no new jobs, wait up to a deadline) or takeover of running VMs (D-14) |
| Pool device down | Running job lost; agent VMs of the device too (device failure, [Idee 44](../KONZEPT.md#k-idee-44)) | Never repeat silently; other devices serve new jobs of the same labels within their shares; moved sessions compete with CI for the rest (D-22) |
| Whole pool down | Both workloads stop; a job waits up to 24 h [GH-LIMITS] | Alarm much earlier; no CI job may be needed to repair the pool (host-bound way, §1.3) |
| Pool full | – | Queue (§6.6), alarm on the oldest waiting age per workload kind |
| GitHub poll down | Registered jobs run on (**Assumption**) | Backoff, no mass start |
| Deletion fails | Row stays `deleting`, sweep every minute (`13-work-vm.md` §7) | Own prefix; remove the GitHub runner as well |

- Double serving: waiting jobs go to any matching online runner; old and new runners may share labels only on
  purpose (§12).
- Active-active: whether two listeners can share one scale-set session is not documented; otherwise a leader lease or
  one set per device. **To be measured:** M-8.

### 11.4 Upgrades
- Host before guest (macOS); rolling, image last.
- CI controller upgrade with drain or VM takeover (D-14).
- Image rollout staged: one class first, previous generation kept.

### 11.5 Runner version alarm
- Daily check of the pinned version against the deprecation endpoint (§3.3); alarm well before 30 days; automatic
  image rebuild proposal; emergency build path for a security release that pauses assignments.

### 11.6 Logs
- Job logs live at GitHub. Locally: `_diag` bounded and briefly encrypted (§9.8); controller log lines with job ID,
  class, durations and error class only.
- The operator keeps container stdout out of the central log store on purpose [OPS]; CI keeps that line.

## 12. Migration

### 12.1 Necessary changes
1. New labels, **no** reuse of today's labels during the trial. Proposal: `agent-ci-u`, `agent-ci-b`, `agent-ci-t`
   plus sizes (the research proposed a DFX prefix; the name is open).
2. Workflows: `runs-on`, Homebrew paths, home-directory paths of the old host, Colima starters and `DOCKER_HOST`
   removed.
3. The runner policy, its exception markers and its exact-match contract test in the infrastructure repository
   [OPS].
4. The canonical preflight of the image builds replaced [OPS].
5. A38 inventory and policy approval where A38 is enforced; a guard-docs change set (only
   `.github/workflows/a38-guard.yml` and/or markdown) needs none, so the pool B switch needs its own review (§9.2).
6. Runtime tables of the CI runner policy measured again [OPS].

### 12.2 Stages (research 1 §8)

| Stage | Content | Exit |
|---|---|---|
| 0 | Inventory of real runners, plans, organisations, labels; secret, environment and `GITHUB_TOKEN` settings per repository against the U preconditions of §9.6; clear the label gap of §1.5; amd64 need | – |
| 1 | Test organisation/repository, option A, one VM class, attack and failure tests on a pool device to which no workgroup is assigned yet; admission of §6.6 built | Uninstall the app |
| 2 | Shadow operation: copy of a light check (e.g. Dockerfile lint) with a new label, not required, trusted triggers only; M-19 and M-20 on a device with agent VMs of a test workgroup | Switch the workflow off |
| Gate U (recommendation) | D-7 and D-20 put to the client; if ordered: interface boundary, `hostLoopback: false`, M-6 and M-21 passed, M-18 for macOS guests (§9.3) | No class U job on the device |
| 3 | Light PR checks (class U; proposed default: not before D-20 is decided, §9.3); at least 5 runs per load class [OPS] | Label back |
| 4 | Pool B (A38 guard), then `DFXswiss/backend` medium jobs | Label back |
| 5 | Script tests after separating the macOS parts; heavy backend jobs after the size decision (D-11) | Label back, while the old runner service still exists |
| 6 | Image builds with short-lived registry credentials | Label back, while the old runner service still exists |
| 7 | Infrastructure plans/applies and tunnel tokens only after remote state and a credential concept | Manual way |
| Never | Host-bound jobs (§1.3) | – |

- Shadow operation: side effects (push, comment, deploy) switched off; duplicate runs do not share `concurrency`.
  Without a comparison host for a label, historical values are only a plausibility check.
- Infrastructure plans on PRs execute PR code (class U, §5) and get no provider credentials.

### 12.3 Today's CI machines into the pool ([E-35](../KONZEPT.md#e-35), [E-36](../KONZEPT.md#e-36))
One machine at a time, each only after the jobs it serves have a working place in the pool or a stage above is done
for them:

| Step | Content | Way back |
|---|---|---|
| a | Inventory: which labels and host-bound jobs the machine serves; data on it (state, caches, credentials, §1.2) moved or declared dispensable | – |
| b | Reduce its runner slots while stages 3 to 6 move its jobs; no label served by old and new runners except on purpose (§11.3) | Slots back |
| c | Remove the runner services, their accounts, caches and credentials; only the deploy runner of §1.3 stays until D-1 is decided (open deviation). Check for class H (§5): its group admits only the one repository and the protected workflow refs, no workflow with a PR trigger, and a test PR with a changed `runs-on` does not reach it | Reinstall the services |
| d | Operating system to the required version (§10.2); the device is set up anew before it holds workgroup plaintext (recommendation); then work boundary and CI slot accounts installed | – |
| e | Enroll and certify as execution device only after D-1 is decided; if D-1 does not allow a deploy runner on an execution device, it is removed first (pull deployment, D-1 (c)); first without workgroups, with class B/T load; then M-19 and M-20 with agent VMs of a test workgroup | Decertify |
| f | Both workloads within the shares; class U as decided in D-7/D-20 (recommendation: only after gate U on this device) | Share of the kind to 0 |

The last machine outside the pool is gone when every label of the inventory is served by the pool or listed as
host-bound. Machines with other roles: D-25.

### 12.4 macOS order (research 2 §7)
(1) Linux run of the script tests (M-14), (2) split the job, (3) legal review (D-21), (4) Lima spike (M-12, M-16 to
M-18, M-9), (5) pilot with one trusted DFX repository, (6) decision on Xcode and RealUnit (D-16, D-17).

### 12.5 Host-bound jobs
Open deviation, decision D-1 ([KONZEPT part D](../KONZEPT.md#d-offen)): today they run on a deploy runner outside the
platform, against [E-34](../KONZEPT.md#e-34) and [E-35](../KONZEPT.md#e-35) (§1.3). The options – among them a minimal
deploy runner per device (class H, under the requirements of §5) as an exception and a signed pull deployment without
an Actions runner – are listed in D-1.

## 13. Open decisions

**Decided is only D-5, by [E-35](../KONZEPT.md#e-35) and [E-36](../KONZEPT.md#e-36);** where E-35 or E-36 excludes a
single option, the row says so. Everything else is open. The tendency is the factual assessment of the research reports, not a choice.

| ID | Decision | Options | Tendency (not decided) |
|---|---|---|---|
| D-1 | Scope, and host-bound jobs (§1.3; not covered by [A20](../KONZEPT.md#a-20); open deviation, [KONZEPT part D](../KONZEPT.md#d-offen)) | (a) Linux, not host-bound only; (b) plus macOS VMs. Host-bound jobs: (c) signed pull deployment without an Actions runner; (d) minimal deploy runner per device outside the platform as an exception to [E-34](../KONZEPT.md#e-34) and [E-35](../KONZEPT.md#e-35), class H (§5); (e) another way the client names. Whether such jobs may run outside the platform at all is part of this decision | (a) first, (b) after the spike; host-bound jobs: no choice made, all of (c)–(e) open; on pool devices without runner groups (D-9) §5 recommends (c) rather than (d) |
| D-2 | Trigger | (a) `actions/scaleset`; (b) webhook + JIT; (c) polling + JIT; (d) fixed JIT stock | (a) |
| D-3 | Location of the controller | (a) inside `agent-runner`; (b) sidecar with its own account; (c) separate host (not as a machine outside the platform (E-35), nor as an own CI device inside it (E-36)); (d) hub with KONZEPT change | (b) |
| D-4 | Owner of the CI app and of the key that creates JIT runners, and where that key lies | operator; separate service account; external signer | separate from the user app ([ADR-015](../adr/015-github-app-device-flow-rest-only.md)); key never in VM or hub |
| D-5 | Hardware | Were: (a) share the productive runner device; (b) idle runner device as own CI host; (c) repurpose the current macOS CI hosts; (d) mixed | **Decided by [E-35](../KONZEPT.md#e-35) (2026-11-25) and [E-36](../KONZEPT.md#e-36) (2026-11-26): one shared pool; CI jobs, also with foreign PR code, run on the same devices as the work VMs.** The research's tendency to an own CI host (separation from workgroup plaintext) is rejected, also as an own CI device inside the platform; reason: E-35 and E-36. What follows for class U is open (D-7, D-20; recommendation: gate U, §9.3). Shares: D-22 |
| D-6 | Public fork PRs (Linux and macOS) and who approves | (a) not self-hosted; (b) only after A38 approval in U; (c) all in U | open; the service never approves itself |
| D-7 | pf residual risk | (a) accept for CI, per class (U, B, T, P); (b) `hostLoopback: false` + interface/VLAN boundary before start; (c) own physical network in addition. Includes: how the interface boundary is built | (b); for class U recommended as gate U (§9.3) |
| D-8 | Egress: rule on outgoing addresses and destination list (only free Cloudflare plans, [E-10](../KONZEPT.md#e-10); §9.4) | (a) adopt E-10's rule on outgoing addresses; (b) own CI rule – must work without a paid plan; (c) domain allow-list through a proxy – must work without a paid plan | open |
| D-9 | GitHub plan | (a) Team for runner groups; (b) Free with repository runners – cannot meet §5 within one repository: then a repository may carry only one class, otherwise (b) is not viable; (c) Enterprise; in each case: does the plan offer environments bound to protected branches for the repositories with U jobs (§9.6)? | open |
| D-10 | CPU and runtime invariant | (a) no overcommit; (b) measured quota per class; does the 20 % invariant apply to VM pools? Which CPU ratio holds for agent VMs on the same device? | open, after M-5/M-9/M-19 |
| D-11 | Heavy jobs | (a) larger profile; (b) split jobs; (c) keep the current large host – excluded as a machine outside the platform (E-35), and as an own CI device inside it (E-36) | open between (a) and (b) |
| D-12 | Model in KONZEPT | (a) infrastructure workload without task; (b) technical organisation/workgroup; (c) own product next to the platform – **excluded by [E-35](../KONZEPT.md#e-35)**; plus the CI-guest clarification of §7.3 and the job audit of §6.4 | open |
| D-13 | Billing | (a) internal exemption; (b) internal cost centre | open |
| D-14 | Upgrade | (a) drain; (b) VM takeover | open |
| D-15 | Trust in the golden image | (a) digest + SBOM; (b) also signed; (c) reproducible build | open |
| D-16 | Who counts as own use for macOS | RealUnit and 21gifts own use or third party; customers of agent.dfx.swiss excluded until legal review | customers excluded |
| D-17 | RealUnit goldens | where they run today; whether they move into VMs | clarify first (§8.5) |
| D-18 | Experimental Lima feature in production | yes, pinned with spike; no | depends on M-12, M-16 |
| D-19 | macOS tool | Lima; Tart; Anka/Orka; GitHub-hosted; dedicated Mac without VM (excluded by E-35 as an own machine; deviates from A20, §8.6) | Lima, Tart as fallback (§8.6) |
| D-20 | Hardening order ([E-25](../KONZEPT.md#e-25)) | order the hardening expressly: which of §9.3–§9.7 and of the separation per class of §5 (own runner group, scale set, VM pool and slot accounts per class) the client orders before which stage; includes the recommended gate U (§9.3) with its two build parts: order it, order parts of it, or not; includes the recommended properties (a) and (b) of the CI accounts (§9.3: nothing of a job carries over; every account covered by the work VM boundary before its first process) and how they are built; includes the repository preconditions for class U (§9.6: secrets only in environments bound to protected branches, organisation secrets only where needed, `GITHUB_TOKEN` read-only by default) | order gate U before stage 3; proposed default: class U does not start before this decision, and not in a repository that misses the §9.6 preconditions |
| D-21 | Legal review of the Apple licences | 2B(iii) for the installation type (DFU/IPSW), governing law, intra-group charging, Xcode in a guest | review on the original text before the macOS pilot |
| D-22 | Shares of the pool (§6.6) | values of `MIN_SHARE`, `MAX_SHARE`, `HEADROOM_INTERACTIVE`, organisation and class shares, `LATENCY_BUDGET`, `POOL_FAILURE_RESERVE`; order between workload kinds; may CI demand stop idle agent VMs? how much Linux capacity may the pilot device give up for macOS VMs? | open, after M-19/M-20 |
| D-23 | Where CI placement is decided | (a) each device offers only its own free capacity to GitHub; (b) one leading controller for all devices; (c) hub (metadata rule, D-3) | open, after M-8 |
| D-24 | Class U at the same time as T/P on one device | (a) allowed with separate slot accounts; (b) never at the same time; (c) U only on some pool devices, which still carry agent VMs (a device for CI alone is excluded by [E-36](../KONZEPT.md#e-36)) | open |
| D-25 | Which operator machines become pool devices, and when | machines with production services, the hub device, small machines: (a) all, in an order; (b) machines with another role stay outside as no runner machines (for machines with production services this contradicts [E-35](../KONZEPT.md#e-35) and needs a new rule of the client; keeping the hub device out of the CI pool does not, its capacity already belongs to agent.dfx.swiss); work devices are excluded by [E-32](../KONZEPT.md#e-32) and are no option | open; E-35 names the whole server capacity |
| D-26 | Trust class model (§5) | (a) the five classes U, B, T, P, H of §5; (b) fewer classes; (c) another split | (a) (research 1 §4.2) |

## 14. Measurements before building

| ID | Measurement | Procedure | Acceptance |
|---|---|---|---|
| M-1 | JIT lifetime and waiting | In a test organisation create JIT configurations; delay the start by 1, 10, 60 and 180 min; record the behaviour | The usable delay is known and documented; D-2 option (d) assessable |
| M-2 | Rate limits of `generate-jitconfig` | Increase requests; read the rate headers | Budget per organisation and hour known; class limits set below it |
| M-3 | Usage per organisation | Usage report and minutes of one month per organisation | Cost risk at USD 0.002/min known |
| M-4 | Linux start latency | 50 clones each cold/warm: request → boot → Docker ready → runner online → first step; p50, p95, maximum at 1, 5, 10, 20 parallel VMs | Values documented; pickup target set from them |
| M-5 | Job sizes | Coverage, E2E and script tests in 4/8, 8/16 and 16/32; CPU wait, RSS, disk throughput | Each job has a size; D-10/D-11 decidable |
| M-6 | Loopback and boundary | `hostLoopback: false` with vsock SSH, Docker DNS and egress; then the work VM attack tests including the site network | All work VM attack probes pass; site network unreachable; part of gate U (§9.3) |
| M-7 | Egress throughput | Cloudflare throughput with parallel image pulls (tunnel MTU 1300) | Throughput and failure behaviour documented; D-8 decidable |
| M-8 | Two listeners and offered capacity | Start two listeners; kill one hard; record the behaviour; let a listener offer less capacity than jobs wait, down to zero | Active-active, leader lease or one set per device decided on facts; no job is taken without a reservation (§6.6); D-23 decidable |
| M-9 | Runtime deviation under load (spike criterion 5) | Repeated runs of a reference job idle and under full pool load | Deviation at most 20 % [OPS] |
| M-10 | macOS start time and disk | 10 cold and 10 cloned starts: time to SSH and to "runner online" (median, p95); `du` of base and clone after one job | Values documented |
| M-11 | GPU/simulator in the macOS guest | Run simulator and golden tests in the guest | Stable or documented as unstable (flips §8.6) |
| M-12 | Start without GUI login (spike criterion 1) | Lima macOS guest as LaunchDaemon after a host restart without GUI login, despite the forced display | VM starts and takes a job without a person |
| M-13 | vsock SSH for macOS guests | Connect over vsock; hand over a JIT configuration on stdin | Works without forwarded TCP ports |
| M-14 | Script tests under Linux | Run the whole list (254 commands on 2026-11-06 [OPS]) once under Ubuntu; classify every failure | Every failure classed as portable, small fix, or real macOS need |
| M-15 | Power | Mains measurement with two macOS VMs under load | Value documented for the device role |
| M-16 | Clone, boot and JIT stability (spike criterion 2) | 50 runs clone → boot → JIT registration → job → deletion | 50 runs without residue (VM, disk, GitHub runner) |
| M-17 | Two parallel clones (spike criterion 3) | Two macOS clones at once | No identity or network conflict |
| M-18 | macOS network boundary (spike criterion 4) | From the guest: host, LAN, other VM, site address | None reachable; part of gate U for macOS guests (§9.3) |
| M-19 | Session latency next to CI load | The criteria of §6.6 on one pool device with agent VMs of a test workgroup: without CI, then with rising CI share up to the maximum (Linux, then with two macOS VMs) | Deviation per CI share known; `LATENCY_BUDGET` and the shares settable (D-22) |
| M-20 | Admission and placement under mixed load | Concurrent requests of all workload kinds and sizes on a full pool; large profiles, borrowing and return of shares, one device taken away | No budget exceeded, no running VM stopped, no third macOS VM; waiting times and fragmentation documented |
| M-21 | Separation of the workloads (gate U) | From a class U VM: other CI classes, work VMs, session VMs, the runner's services, host loopback and slot files; exhaustion of CPU, RAM, disk, egress; continuous prober as in `13-work-vm.md` §6.6, in every state of the Cloudflare client | Nothing reachable, also not intermittently; quotas hold; no CI file or cache visible from an agent VM and vice versa |

## 15. Risks

| Risk | Path | Countermeasure |
|---|---|---|
| Access to host or site network | Foreign code uses the pf attribution gap (§9.3) | Recommended gate U (§9.3; open, D-7, D-20): interface boundary, `hostLoopback: false`, attack tests (M-6, M-18, M-21) |
| Deploy runner reached by other workflows | Class H runs persistently on the host of a pool device; a label or a changed `runs-on` in a PR targets it | Own group with one repository and protected workflow refs, no PR trigger; without groups pull deployment (§5, D-1 (c), D-9); check in §12.3 step c |
| CI load displaces sessions | CI VMs hold capacity or slow the device; session starts wait or fail | Shares, headroom, latency budget, stop of new CI VMs (§6.6, M-19); displacement metrics (§11.2) |
| Co-residency of foreign code with workgroup plaintext (**carried knowingly**) | CI jobs with foreign PR code run on the devices that hold workgroup plaintext; this is the client's decision ([E-36](../KONZEPT.md#e-36)), not an oversight, and no own CI device separates them | The client carries this risk by E-36; the countermeasures stay a recommendation: gate U (§9.3; open, D-7, D-20), separation rules of §9.3, D-24 |
| Side channel between workloads | Foreign code and workgroup plaintext on one host: CPU caches, timing, hypervisor bug | Nothing writable shared, separate slot accounts (§9.3); the rest is a residual risk of E-35 and E-36, carried knowingly by the client (E-36), named again with D-20. Residual risk: after an escape the job controls the whole slot account – its processes, its home (Lima `_config` with an `override.yaml`, other files) and every state a macOS account can hold outside it (examples, not exhaustive: crontab and `at` jobs, jobs in its launchd user domain `user/<uid>`, `/private/var/folders/…`, its files in `/tmp`); such state can carry over into later jobs and cannot be enumerated conclusively. Recommended: properties (a) and (b) of §9.3 – nothing of a CI job carries over into a later job or onto the device, and every account is covered by the work VM boundary (pf per UID, wrapper and sudoers allow-list) before its first process, fail-closed; how this is built is left to the build (§9.3; recommendation, open, D-20). The escape from the VM itself (hypervisor bug) stays a residual risk |
| Pool failure hits both workloads | Device, egress, update or boundary failure stops sessions and CI together | Failure reserve, rolling maintenance (one device at a time), repair way without CI (§11.3) |
| Mixing U and T | A JIT runner takes another matching job | Groups per class, repository/workflow policy, no shared labels (§5) |
| Theft of the app key | Key in the runner process or a VM | Own account or signer, organisation runner permission only, rotation (D-4) |
| Preview breaks | Change of `actions/scaleset` | Pin the version, adapter, JIT fallback |
| Fleet takes no jobs | Runner older than 30 days or a security pause | Deprecation alarm, automatic image proposal, emergency build (§11.5) |
| Cache poisoning | U writes, T reads | Separate classes, pre-warmed image, T without U cache (§9.5) |
| Supply chain | Manipulated runner/apt/image package | SHA, SBOM, signature, staged rollout (D-15) |
| CPU overload | RAM slots taken as capacity | CPU budget in admission, load test (M-5, M-9) |
| Job loss at upgrade | Restart deletes VMs | Drain/takeover, alarm, never repeat silently (D-14) |
| Disk overflow | Docker layers, Xcode/simulator, hanging VMs | Volume quota, reserve, two generations, sweep, disk alarm |
| Cloudflare outage or throttling | All VMs fail-closed on one exit | Load test (M-7), capacity alarm, D-8 |
| Double serving | Old and new runners with the same labels | New labels, controlled cut (§12) |
| Personal data in logs | Real test data or error output | Data-free fixtures, short `_diag` keeping, review (§9.8) |
| New GitHub fee | Postponed model returns | Measure minutes (M-3), watch the changelog, budget limit |
| Gap before the start | Labels without active host (§1.5) | Stage 0; interim solution decided separately |
| Unordered hardening | Conflict with [E-25](../KONZEPT.md#e-25) | Decide the protection scope first and enter it in KONZEPT (D-20) |
| Licence misread | macOS/Xcode licence interpreted wrongly | Review on the original text, customer jobs blocked (D-16, D-21) |
| Lima macOS guests experimental | Feature breaks or changes | Pin, spike, Tart as fallback (D-18, D-19) |
| Third macOS VM blocks jobs | Manual VM or image build exceeds two | Central counter, no manual VMs (§8.4) |
| Host update delays guest updates | Host must be ≥ guest | Rolling updates, image last |
| Linux move hides BSD failures | Only Linux runs remain | Keep a small macOS core suite (§8.5) |
| Tart licence or owner changes | Relicensing | Pin versions, keep the Lima way |

## 16. Sources

All URLs retrieved on 2026-11-25. Several pages were read through a summarising fetch tool or a text extractor;
quotations are excerpts and are to be checked against the original before any legal use.

### 16.1 GitHub
| Key | URL |
|---|---|
| GH-REST | https://docs.github.com/en/rest/actions/self-hosted-runners |
| GH-RUNNER-REF | https://docs.github.com/en/actions/reference/runners/self-hosted-runners |
| GH-HOSTED | https://docs.github.com/en/actions/reference/runners/github-hosted-runners |
| GH-LIMITS | https://docs.github.com/en/actions/reference/limits |
| GH-REMOVE | https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/remove-runners |
| GH-HOOKS-SCRIPTS | https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/run-scripts |
| GH-WEBHOOK-EVENTS | https://docs.github.com/en/webhooks/webhook-events-and-payloads |
| GH-WEBHOOK-FAILED | https://docs.github.com/en/webhooks/using-webhooks/handling-failed-webhook-deliveries |
| GH-RATE | https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api |
| GH-ARC-AUTH | https://docs.github.com/en/actions/how-tos/manage-runners/use-actions-runner-controller/authenticate-to-the-api |
| GH-APP-PERMS | https://docs.github.com/en/rest/authentication/permissions-required-for-github-apps |
| GH-ARC | https://docs.github.com/en/actions/concepts/runners/actions-runner-controller |
| GH-ACCESS | https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/manage-access |
| GH-GROUPS | https://docs.github.com/en/actions/concepts/runners/runner-groups |
| GH-SECURE | https://docs.github.com/en/actions/reference/security/secure-use |
| GH-SECRETS | https://docs.github.com/en/actions/security-for-github-actions/security-guides/using-secrets-in-github-actions (cited 2026-11-26: secrets and forks; "Workflow permissions" for the `GITHUB_TOKEN` default) |
| GH-CACHE | https://docs.github.com/en/actions/reference/workflows-and-actions/dependency-caching |
| GH-FORK-APPROVE | https://docs.github.com/en/actions/how-tos/manage-workflow-runs/approve-runs-from-forks |
| GH-BILLING | https://docs.github.com/en/billing/concepts/product-billing/github-actions |
| GH-RUNNER-PRICING | https://docs.github.com/en/billing/reference/actions-runner-pricing |
| GH-PRICING-2026 | https://github.com/resources/insights/2026-pricing-changes-for-github-actions |
| GH-VERSION-1 | https://github.blog/changelog/2026-09-28-self-hosted-runner-version-enforcement-date-has-moved/ |
| GH-VERSION-2 | https://github.blog/changelog/2026-06-12-github-actions-minimum-version-enforcement-timeline-for-self-hosted-runners/ |
| GH-CHANGELOG-FEB | https://github.blog/changelog/2026-02-05-github-actions-early-february-2026-updates/ |
| GH-RELEASES | https://github.com/actions/runner/releases |
| SCALESET | https://github.com/actions/scaleset, https://raw.githubusercontent.com/actions/scaleset/main/README.md |

### 16.2 Lima, Tart, other tools
| Key | URL |
|---|---|
| LIMA-LICENSE | https://raw.githubusercontent.com/lima-vm/lima/master/LICENSE |
| LIMA-TPL-2.3 | https://raw.githubusercontent.com/lima-vm/lima/v2.3.0/templates/default.yaml |
| LIMA-VZ-DRIVER | https://raw.githubusercontent.com/lima-vm/lima/v2.3.0/pkg/driver/vz/vz_driver_darwin.go |
| LIMA-VZ | https://lima-vm.io/docs/config/vmtype/vz/ |
| LIMA-CLONE | https://lima-vm.io/docs/reference/limactl_clone/ |
| LIMA-RELEASES-API | https://github.com/lima-vm/lima/releases, https://api.github.com/repos/lima-vm/lima/releases?per_page=12 |
| LIMA-MACOS | https://lima-vm.io/docs/usage/guests/macos/ |
| LIMA-MACOS-TPL | https://raw.githubusercontent.com/lima-vm/lima/master/templates/_images/macos-27.yaml |
| LIMA-BREW | https://raw.githubusercontent.com/lima-vm/lima/master/templates/homebrew-macos.yaml |
| LIMA-4614 | https://api.github.com/repos/lima-vm/lima/issues/4614 |
| LIMA-5516 | https://api.github.com/repos/lima-vm/lima/issues/5516 |
| TART-LICENSE | https://raw.githubusercontent.com/openai/tart/main/LICENSE |
| TART-README | https://raw.githubusercontent.com/openai/tart/main/README.md |
| TART-FAQ | https://tart.run/faq/ |
| TART-RELEASES | https://api.github.com/repos/cirruslabs/tart/releases?per_page=15 |
| TART-IMAGES | https://raw.githubusercontent.com/cirruslabs/macos-image-templates/master/README.md |
| SOFTNET | https://raw.githubusercontent.com/openai/softnet/main/README.md |
| CIRRUS | https://cirruslabs.org/ |
| CLODPOD-88 | https://github.com/webcoyote/clodpod/issues/88 (secondary source) |
| UTM-LICENSE | https://raw.githubusercontent.com/utmapp/UTM/main/LICENSE |
| ANKA | https://docs.veertu.com/anka/licensing/ |
| ORKA-PRICING | https://macstadium.com/pricing |
| ORKA-ENGINE | https://docs.macstadium.com/orka/orka-engine/orka-engine-30.md |

### 16.3 Apple and macOS virtualization
| Key | URL |
|---|---|
| APPLE-SLA | https://www.apple.com/legal/sla/docs/macOS27.pdf (text read through https://r.jina.ai/https://www.apple.com/legal/sla/docs/macOS27.pdf) |
| XCODE-SLA | https://www.apple.com/legal/sla/docs/xcode.pdf (text read through https://r.jina.ai/https://www.apple.com/legal/sla/docs/xcode.pdf) |
| APPLE-FORUM | https://developer.apple.com/forums/thread/845455?answerId=906368022 |
| APPLE-INSTALL | https://developer.apple.com/documentation/virtualization/installing-macos-on-a-virtual-machine.md |
| APPLE-PROVISIONING | https://developer.apple.com/documentation/virtualization/vzmacguestprovisioningoptions.md |
| APPLE-ICLOUD | https://developer.apple.com/documentation/virtualization/using-icloud-with-macos-virtual-machines.md |
| APPLE-BRIDGE | https://developer.apple.com/documentation/virtualization/vzbridgednetworkdeviceattachment.md |
| APPLE-RUN-MACOS | https://developer.apple.com/documentation/virtualization/running-macos-in-a-virtual-machine-on-apple-silicon.md |
| APPLE-FV-AUTOLOGIN | https://support.apple.com/102316 |
| APPLE-FV-DEP | https://support.apple.com/guide/deployment/intro-to-filevault-dep82064ec40/web |
| ECLECTIC-2022 | https://eclecticlight.co/2022/08/04/virtualisation-on-apple-silicon-macs-8-how-apple-limits-vms/ |
| ECLECTIC-2023 | https://eclecticlight.co/2023/10/26/how-good-is-gpu-access-for-apple-silicon-virtual-machines/ |
| SCALENINJA | https://scaleninja.com/macvisor/virtualization-framework/ (secondary source) |

### 16.4 Repository `DFXswiss/agent`
- Concept and rules: [KONZEPT.md](../KONZEPT.md) (E-34, E-35, E-36 and the passages of their "Bezug"; A20, A21).
- Specification: [00-overview.md](00-overview.md) §1, §3; [05-runner.md](05-runner.md) §2.1, §3.1, §3.2, §3.4, §4,
  §10.2, §14; [08-monitoring.md](08-monitoring.md); [13-work-vm.md](13-work-vm.md) §2, §4, §6, §7, §8, §10, §12.
- Design and security: [PLATFORM-DESIGN.md](../PLATFORM-DESIGN.md) §3;
  [work-vm-threat-model.md](../security/work-vm-threat-model.md).
- ADRs: [ADR-002](../adr/002-hub-as-coordinator.md), [ADR-009](../adr/009-vm-start-under-ten-seconds.md),
  [ADR-015](../adr/015-github-app-device-flow-rest-only.md), [ADR-059](../adr/059-work-vm-on-request.md),
  [ADR-062](../adr/062-prepaid-credit-and-usage-billing.md).
- Decisions and checks: [HOT-20](../decisions/HOT-20.md), [KOR-59](../decisions/KOR-59.md),
  [own-device manual check](../manual-checks/own-device.md), [M0 measurements](../measurements/M0.md).
- Outside `docs/platform/`: `docs/a38-guard.md`, `.github/actions/a38-guard/action.yml`, `images/workvm.yaml`,
  `images/workvm/provision.sh`, `.github/workflows/platform-runner.yml`, `.github/workflows/a38-guard.yml`, `.github/a38.json`.

### 16.5 Operator documentation
[OPS]: the operator's internal infrastructure repository (runner policy, CI runner policy, fleet inventory, host
READMEs, runbooks and records). It is private; this public document neither names its paths nor its devices. The
two research reports of 2026-11-25 that this concept is built from cite it line by line.
