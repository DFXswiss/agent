# ADR-098: Working modes – what runs locally today becomes a selectable mode of a task, never mandatory and never on by default; the local tool and the legacy hub retire

Status: Accepted (2026-11-26, KOR-72; design only, built by the packages named in §9; reviewed by two reviewers with
fresh context, their findings are worked in)

Implements [E-32](../KONZEPT.md#e-32) (nothing runs locally; every local building block becomes a selectable way of
working of the platform, never forced and never active by default; a session started with "halte dich an A38" or
"baue Feature xy, nutze den Orchestrator" carries it out completely). Under the rules [E-04](../KONZEPT.md#e-04) and
[E-27](../KONZEPT.md#e-27) (an agent can do everything a person can), [E-05](../KONZEPT.md#e-05) (a missing
prerequisite is initiated), [E-11](../KONZEPT.md#e-11) (no restriction nobody asked for),
[E-25](../KONZEPT.md#e-25) (no protection layers beyond what is needed), [E-26](../KONZEPT.md#e-26) (simple for
agents) and [E-31](../KONZEPT.md#e-31) (subscriptions only where their owner switches them on), the concept's
[§10](../KONZEPT.md#k-10) (the session executes nothing itself; every action is a controlled platform operation,
checked [formally, not by content](../KONZEPT.md#k-10-b7)), [Idee 34](../KONZEPT.md#k-idee-34) (one delegated main
orchestrator per user), [Idee 42](../KONZEPT.md#k-idee-42) (roles keep work, review and release apart) and
[Idee 43](../KONZEPT.md#k-idee-43) (one main agent per task, fresh context), and the interpretations
[A3](../KONZEPT.md#a-3) (the execution ban concerns platform sessions, not the user's computer) and
[A15](../KONZEPT.md#a-15) (every instruction belongs to the verified person who gave it).

## Context
- **What runs locally today** (complete inventory with evidence, gaps and effort: the KOR-72 report of the operator,
  not in the repository). Three groups:
  1. The legacy tool `agent` (`src/agent_cli`, about 77 000 lines): a ledger of sessions, tasks, rounds and checks in a
     loopback PostgreSQL synchronised with the legacy hub; a device daemon (LaunchAgent) with the knock loop and one
     coordinator loop per configured worker; the issue coordinator with lanes, orders, preconditions and conflict
     mandates; A38 (test report, review rounds, comments, the PR guard, local CI comments, workflow and environment
     approvals); AI accounts (pools, logins, model slots, token keep-alive, usage readers); error-fix, mail and
     Telegram acts; a loopback dashboard; skills that tell a session how to use all of it.
  2. The legacy hub `agent-core` (`src/agent_core`): teams, the team record, passkeys, device keys.
  3. The operator's work-package pipeline: a dispatcher that starts packages as headless sessions on other
     subscriptions, its supervisor and watchers, the merge lane (precheck, train, renumbering, gates on test Macs,
     fast-forward push), release and operations scripts (including a runner package with Apple signature and
     notarisation), a browser login for subscriptions and a periodic transfer of subscription logins to the platform
     device.
- **What the platform already has** and the modes build on: tasks with one main agent and full subagents
  ([ADR-088](088-full-subagents-and-limits-as-settings.md)); rights profiles, `review.verdict`, `github.merge` and merge
  criteria ([ADR-094](094-rights-profiles-and-agent-roles.md)); test runs in fresh VMs
  ([ADR-080](080-test-runs-as-operations.md)); the work VM ([ADR-059](059-work-vm-on-request.md)); the browser
  ([ADR-085](085-browser-as-platform-operation.md)); the pull-request index and "Fix conflicts"
  ([ADR-084](084-fix-conflicts-by-merge-commit.md)); GitHub identities ([ADR-078](078-github-identities.md)); central
  subscriptions with use per subscription ([ADR-090](090-every-subscription-logged-in-centrally.md),
  [ADR-092](092-full-subscription-login-and-renewal-on-the-device.md)) and model access with fallback
  ([ADR-081](081-model-access-fallback-and-dfx-access-on-own-hardware.md)); the main orchestrator on platform hardware
  ([ADR-082](082-agent-own-identity.md) §6); the own-device runner package ([ADR-074](074-own-device-without-terminal.md));
  the situation view ([ADR-076](076-situation-view-and-attention-categories.md)); in-app notifications
  ([ADR-020](020-notifications-in-app-and-session-pings.md)); and a precedent for a brief that a task carries
  (the process analysis, [ADR-097](097-process-analysis.md) §1).
- **What is missing in general** (beyond each building block's own gap): a way to choose a way of working per task,
  platform components that do deterministic multi-step work over time (watching pull requests, trains, release
  steps) instead of a model polling, operations for a few GitHub steps (leave draft, review comments) and the
  accesses that some ways of working need (Apple signing, production, mailboxes, Telegram).
- The legacy design already separates the two roles this ADR keeps: scripts own monitoring, lanes, tests and every
  GitHub step; models do the work they are asked for and never poll (DESIGN.md §§19.1, 19.7).

## Decision

### 1. A working mode is a named building block of a task
- A **mode** is a catalogue entry `{name, version, title (de, en), brief, operations, drivers, settings, needs,
  accesses}`:
  - `brief` – a section of the harness rules (`images/harness/RULES.md`, one section per mode, shipped with the golden
    like the process analysis brief) that tells the session what the mode expects of it;
  - `operations` – the operation types the mode uses, new ones included (§8);
  - `drivers` – platform components that do the deterministic part (§4);
  - `settings` – a JSON schema of what a person may set for the mode (e.g. the target repositories of A38); stored per
    task, defaults per workgroup allowed (§2);
  - `needs` – other modes it requires (§2: never switched on implicitly) and prerequisites of the register
    [spec/15-prerequisites.md](../spec/15-prerequisites.md);
  - `accesses` – the decision points of §7 the mode can use, each optional.
- The catalogue is a contract (`contracts/modes/INDEX.json`, one file per mode), versioned with the hub, readable by
  every member (`GET /v1/modes`) and by agents (`agent-platform modes list`, one line per mode, E-26).
- **Catalogue v1** (names are stable identifiers; the web shows the titles):

  | Mode | Title | Replaces locally |
  |---|---|---|
  | `a38` | A38 | the author's side of A38: `agent a38 run`, the report and review comments, review rounds of model slots `first`/`second` (the guard is a GitHub workflow and stays, §6) |
  | `orchestrator` | Orchestrator | the work-package pipeline: plan, packages, helpers on own subscriptions, review, release proposal |
  | `coordinator` | Issue coordinator | `agent coordinate`, orders, preconditions, conflict mandates |
  | `merge_lane` | Merge lane | precheck, train, register renumbering, gates and fast-forward push of the pipeline |
  | `mac_checks` | Checks on Mac hardware | gates and end-to-end runs on test Macs |
  | `release_ops` | Releases and operations | release and deploy scripts, operations checks, runner package signing |
  | `notifications` | Notifications | mail and Telegram acts (outgoing messages, incoming replies) |
  | `prod_error_fix` | Error fix from production | `agent watch errors` and the error-fix skill |
  | `subscription_login` | Subscription login | browser and mail login of subscriptions, keep-alive, transfer of logins |

- Modes **add** behaviour; they never restrict (that is a rights profile, ADR-094) and never widen what the person
  who chose them may do ([E-04](../KONZEPT.md#e-04), A3). A mode that starts children may name a built-in role for
  them (A38 review children get the role `review`); ADR-094 applies unchanged (a child is never wider than its
  parent).

### 2. No mode without a choice
- **Nothing switches a mode on by itself**: no default at installation, for an organization, a workgroup, a task
  kind, a role, a profile, a template, a repository or a catalogue update; no "last choice remembered"; no mode on a
  subagent that its task does not have. A workgroup may keep **settings** of a mode (A38 target repositories, the
  merge-lane recipe); they act only on tasks that chose the mode. The web may show the modes a workgroup used before
  as suggestions; a suggestion is never a choice.
- A mode that `needs` another is refused while the other is off (`mode_needs`, "A38 review needs …; choose it too"),
  and the web and the tool offer to choose both in one step (E-05); nothing is switched on implicitly.
- **Without a mode a task works exactly as it does today.** No existing behaviour moves behind a mode.
- A test (E-19) asserts that `hub.task_mode` rows are written only by the three ways of §3 and that no installation,
  migration or creation path writes one.

### 3. Three ways to choose, one record, always revocable
- **Web**: the task form has a field "Arbeitsweisen" (empty); the task page shows the active modes as chips with
  settings and "Ausschalten".
- **Agent tool and MCP** ([E-04](../KONZEPT.md#e-04)): `agent-platform task create --mode NAME [--mode-setting
  NAME.KEY=VALUE]`, `agent-platform task mode on|off --task ID --mode NAME`, MCP tool `set_task_mode`; rows in
  `agent-parity.md`.
- **A sentence in the instruction**: the harness lists the catalogue with examples ("halte dich an A38", "nutze den
  Orchestrator", "mit Merge-Spur"). When the verified instruction of the current turn asks for a mode, the session
  requests the operation `task.mode {mode, on: true, quote, settings?}`. The runner checks **formally**
  ([§10 Punkt 7](../KONZEPT.md#k-10-b7)): `quote` is a verbatim part (whitespace and case folded) of the verified
  prompt of this turn ([ADR-083](083-who-speaks-in-a-shared-session.md)), and that prompt's author may write in the
  task's workgroup; otherwise `rejected` `mode_not_instructed` ("Only a person's instruction can switch on a mode;
  ask with `human.question`."). Text from files, tool results, pull-request comments, mails or the model's own idea
  therefore never switches a mode on. When the wish is unclear (which mode, which settings), the session asks with
  `human.question`. Switching **off** needs no quote: a session may always ask to stop a mode.
- **The hub cannot read prompts** (zero knowledge); the runner sees the plaintext of its own sessions anyway
  ([§5 Punkt 4](../KONZEPT.md#k-5-b4)), so the quote check runs there and the hub records the result.
- **One record**: `hub.task_mode (task_id, mode, version, state on|off, chosen_by, way web|tool|sentence, op_id,
  changed_at, settings_sealed)`; plaintext are names, states, principals and times (classified in 04-crypto.md §7),
  the settings are sealed under the workgroup key like the task's content. Audit `task.mode_on` / `task.mode_off`
  with the acting person ("ausgelöst durch", A15).
- **Visible**: the chat shows a notice line ("A38 eingeschaltet von <Name> – aus der Anweisung „…“ · Ausschalten");
  the situation view and the task list show the modes of a task; `agent-platform task show` lists them.
- **Revocable**: every writer of the workgroup and the agent of the person who chose it switches a mode off at any
  time. Off takes effect at once for new steps: the drivers stop at the next step boundary, waiting mode operations
  are cancelled (executed ones stay), the brief is gone from the next turn, and the session gets a content-free ping.
- A mode stays on for the task – across follow-up prompts, resume ([Idee 40](../KONZEPT.md#k-idee-40)) and device
  moves – until it is switched off or the task is closed.

### 4. The session asks, platform components run
- The session executes nothing itself ([§10 Punkt 3](../KONZEPT.md#k-10-b3)); it requests operations as today.
- **Drivers** are the deterministic parts of a mode: platform code in the runner of the device that serves the task
  (the legacy design's "scripts"). A driver watches external state (pull requests, checks, a mailbox, error logs),
  advances step machines (gates, trains, review rounds, releases) and requests the same operations a session would,
  under the task's GitHub identities and the rights of the person who chose the mode (A15). Every driver step is an
  operation record with audit (§11), and a driver never runs a model in place of the session: when there is work for
  the model (a review finding, a red gate, a new error) it gives the session a ping and a sealed event, resuming a
  paused session first ([ADR-072](072-session-context-resume.md)). **Models never poll and never wait.**
- External state is **read by the runner** (as `github.pull_status` in ADR-084), not pushed to the hub: GitHub
  webhooks to the hub would hand the hub plaintext content. Polling intervals are mode settings with a generous
  default and share one rate budget per GitHub identity.
- A mode that watches beyond one turn (A38, coordinator, notifications, error fix) keeps its task **open** while it is
  on; the task's attention record (ADR-076) shows "watching" with the next step. A paused session costs nothing; driver
  time is runner time, not VM time ([ADR-062](062-prepaid-credit-and-usage-billing.md) unchanged; a resumed session
  is billed as every session).
- Drivers keep their state in the runner database and report it to the hub as metadata (state names, counters,
  times) plus sealed events. They survive restarts and device moves ([ADR-087](087-device-failure.md)): the new
  device takes over the driver state from the hub's copy of the sealed events.

### 5. What "completely" means
A mode is complete when the platform does, without anything local, what the local building block does today. The
acceptance of each build package checks this list against the legacy behaviour.

| Mode | Complete means |
|---|---|
| `a38` | Draft right after the first signed commit; the repository's A38 policy (`.github/a38.json`) run job by job with `test.run` (commands, compose, HTTP smoke and immutable adapters) and the report posted as the local-CI comment (`docs/a38.md`, `docs/local-ci-v1.md`) for the current head; the review runs A (quality and logic, model slot `first`) and B (slot `second`), each a child with the role `review` on the pinned provider (until a second agent software is supported, [§4 Punkt 6](../KONZEPT.md#k-4-b6), both slots run Claude Code on different subscriptions, D6), findings as pull-request review comments and the records as `A38-REVIEW:v1` comments, a new head starts a new run; zero findings on the current head (or a later head that differs only in `.md` files); CI green on this head; leave draft where the repository has no guard. The second review may come from configured team members instead of slot `second` (asked with `human.question`, answered on the platform). **Merge stays human** unless the workgroup sets merge criteria (ADR-094 §5) and a person asks for a merge. |
| `orchestrator` | From one goal: a plan as packages with dependencies and ownership; every package is a **task** of the workgroup with its own main agent (Idee 43), started when its dependencies are done; helpers use the subscriptions their owner switched on for sessions (E-31) with fallback (ADR-081); gates by `test.run` (and `mac_checks` if chosen); an independent review by a child with the role `review`; merge by `merge_lane` if chosen, else by the A38 rules or `github.merge` with the workgroup's criteria; a release proposal as `human.approval`. The plan, its packages and their states are visible in the situation view; the orchestrator session itself is an ordinary main session (or the user's main orchestrator, ADR-082 §6). |
| `coordinator` | Picks up issues assigned to a configured GitHub login and orders created on the platform; drives each through implement, inner review, tests, the A38 steps and leave-draft as tasks; preconditions with override and skip by a person (`human.approval`); duplicate check against open pull requests; a round limit with an assessment and a question to a person; conflicts by "Fix conflicts" (ADR-084, merge commit; a rebase stays unbuilt, A2). |
| `merge_lane` | One lane per repository and base: precheck of a branch on the current base, a train of the waiting branches, renumbering of the repository's registers by a recipe in the repository, one gate run, an atomic fast-forward push; a red train names the red tests and the package; nothing is forced. |
| `mac_checks` | Test runs on macOS VMs of platform Macs (`test.run` kind `macos`), the same result, log and teardown rules as ADR-080; end-to-end runs that need several VMs as one run with a declared size; capacity by the device's core and memory budget. |
| `release_ops` | A release proposal (version, changes, gates) approved by a person; signing and notarisation of packages, deploys to the hosts the workgroup names and the operations checks afterwards, each as an operation with result and log; only with the accesses of §7. |
| `notifications` | Messages to the person (mail, Telegram) on events the person chose (needs a decision, blocked, done); replies by the person become instructions of the task with the person as author, once the channel proves the sender. |
| `prod_error_fix` | Reads new errors of the production logs the workgroup names, records each once, lets the session triage (fix, skip with reason), and fixes in an ordinary task with a draft pull request through the `a38` rules if chosen. |
| `subscription_login` | Logs in and renews every subscription on the platform device (ADR-090, ADR-092), including the sign-in code from a mailbox and other vendors whose usage the legacy tool reads; nothing is transferred from a user's computer. |

### 6. What stays with the user, and how the legacy parts retire
- **At most one Claude session that steers agent.dfx.swiss**: either a platform session made the user's main
  orchestrator (ADR-082 §6; nothing installed) or a session on the user's computer with the agent tool as its
  connector (ADR-056, KOR-63; a process the session starts, no service). No LaunchAgent, no local database, no tmux,
  no local copy of a subscription login, no local test, gate or release.
- **The legacy tool `agent`** retires in three stages per building block: (1) the platform mode is built and accepted;
  (2) the operator switches its own use over and the legacy command prints where the platform does it; (3) the
  command, its loop in the device daemon and its documentation are removed. When the last loop is gone, the
  LaunchAgent of the legacy tool is removed. Commands without a platform counterpart are listed in the report and
  either mapped to a mode or dropped with a reason (the ledger and its skills are replaced by tasks, sessions and
  operations).
- **Repositories that use A38** keep working without a break. Their guard is not local: it is a GitHub workflow of
  the repository that runs the guard pinned from this repository (`.github/actions/a38-guard`, `docs/a38-guard.md`).
  What is local is the author's side: `agent a38 run` on the author's computer, the report comment and the review
  comments. The mode `a38` produces **exactly these comments** (`<!-- DFX-LOCAL-CI:v1 -->` report, `<!-- A38-REVIEW:v1
  -->` review records, posted with the task's GitHub identity), so a repository's guard accepts a pull request from
  the platform as one from a local author, and no adopting repository has to change anything. Where a repository
  has the guard, the guard alone decides Ready (the mode does not call `github.ready_for_review` there); where it has
  none, the mode leaves draft itself. The guard action, its modules and the A38 documents therefore **stay** in this
  repository after the legacy tool's commands are removed (a separate, smaller guard package), until a later
  decision moves the guard itself.
- **Imports first.** Platform packages still import a few legacy modules (the loopback PostgreSQL helper, the signed
  request text, the mailbox reader of the subscription login, a store error, the legacy hub's signing helpers, passkey
  errors and the record connection; [05-runner.md](../spec/05-runner.md) §1.5, [spec-deviations.md](../spec-deviations.md)). They are copied into the platform
  packages before any legacy module is removed.
- **The legacy hub `agent-core`** is retired by [CUTOVER.md](../CUTOVER.md) §7 (step S12; no data migration, §9): teams become
  organizations and workgroups by hand, the team record stays readable from its own database, nothing is migrated
  into the platform; after the last legacy device stopped syncing, the legacy hub is stopped and its code and the
  protocol documents (`PROTOCOL.md`, `RECORD-PROTOCOL.md`, `KEY-PROTOCOL.md`, `PASSKEY-PROTOCOL.md`) move to an
  archive directory.
- **The operator's pipeline** is replaced by `orchestrator`, `merge_lane` and `mac_checks` and removed after their
  acceptance; its test devices become platform devices of the operator's organization.

### 7. Accesses are decisions of the owner, one by one
No access below is assumed. Each is a decision point of the owner; until it is decided, a mode works without that
part and asks for it in the task (E-05: a `human.approval` to the owner naming the access, never a silent grant).
A granted access is a sealed credential of the organization on the platform devices that serve the mode (never in a
session, a work VM or at the hub in plaintext), used only by drivers of tasks that chose the mode, audited per use and
revocable at once.

| Point | Access | Needed by |
|---|---|---|
| D1 | Apple Developer ID signing and notarisation | `release_ops` (runner package for own devices) |
| D2 | Production hosts (deploy, operations checks) | `release_ops` |
| D3 | Production error logs (read) | `prod_error_fix` |
| D4 | Mailboxes (read sign-in codes, send and read notifications) | `subscription_login`, `notifications` |
| D5 | A Telegram bot | `notifications` |
| D6 | Further subscriptions for helpers (each by its owner's switch, E-31) and further vendors | `orchestrator`, `a38`, `coordinator`, `subscription_login` |
| D7 | GitHub accounts beyond the user's own (team second review, worker logins of the coordinator; the operator's account is never shared) | `a38`, `coordinator` |
| D8 | Which of the operator's Macs serve as platform devices for checks (own hardware, ADR-074) | `mac_checks`, `merge_lane` |

### 8. New contracts and operations (built by the packages of §9)
- `contracts/modes/` (catalogue), `task.mode` (class `write`), routes `GET /v1/modes`, `PUT /v1/tasks/{id}/modes/{mode}`,
  `DELETE /v1/tasks/{id}/modes/{mode}`; device state section `task_modes`.
- GitHub: `github.ready_for_review` (leave draft; refused while a mode condition fails), `github.review_comment`
  (findings on lines), `github.set_draft` (guard back to draft).
- `test.run` kind `macos` (`mac_checks`), `release.*` (`release_ops`), `notify.send` (`notifications`), `error.record`
  and `error.triage` (`prod_error_fix`).
- Each new type gets a class in `contracts/ops/classes.json` (ADR-094) and a guest feature flag for older goldens.

### 9. Build order
Most local work first. Each package removes the local parts named with it once accepted.

| Step | Package | Needs | Then removable locally |
|---|---|---|---|
| 1 | Mode foundation: catalogue, `task.mode` with the quote check, record, web, tool, MCP, harness sections, driver framework, "no default" test | – | – (prerequisite) |
| 2 | `orchestrator` (plan as tasks with dependencies, start by dependency, plan view, wake-ups) · `a38` test report · `subscription_login` rest | 1 | dispatcher and its supervisor, local A38 runs, browser login and keep-alive |
| 3 | `orchestrator` helpers, review and release proposal · `mac_checks` · `a38` review runs and Ready · `notifications` | 2 | helper sessions on local subscriptions, gates on test Macs, local review lanes, mail and Telegram acts |
| 4 | `merge_lane` · `coordinator` pickup and orders · `prod_error_fix` | 1, 3 | merge scripts and worktrees, coordinator loops, error watch |
| 5 | `coordinator` duplicates and conflicts · `release_ops` | 4, D1, D2 | conflict mandate, release and operations scripts |
| 6 | Retire the legacy tool (imports copied first, the guard kept) and the legacy hub (CUTOVER §7) | 2–5 | the legacy LaunchAgent, its database and dashboard |

## Consequences
- A person or an agent chooses a way of working with a sentence, in the web or with the tool; the choice is visible,
  attributed and switched off with one action. A task without a choice behaves as before.
- The deterministic parts move from local scripts into runner drivers; the session still only asks.
- New plaintext at the hub: mode names, states and driver states per task (no content).
- The legacy tool and hub stay until each mode is accepted; nothing breaks for repositories that still use them.
- Accesses stay with the owner; modes are useful without them and say what is missing.

## Alternatives considered
- **Modes as rights profiles** (ADR-094): profiles restrict and are never on by default either, but a mode adds
  behaviour and drivers; mixing them would make a restriction switch on work. Kept separate.
- **Default modes per organization or workgroup**: forbidden by E-32 ("auch nicht per default aktiv").
- **Recognising the sentence at the hub**: the hub cannot read prompts (zero knowledge).
- **Switching on by the model without a quote**: a sentence in a repository file or a comment could switch on
  `release_ops`; the quote check is the minimal formal proof that a person asked (E-25: nothing more).
- **Running the legacy tool inside the work VM**: it would keep local loops without platform audit and need platform
  credentials in the VM ([E-08](../KONZEPT.md#e-08): the work VM holds none).
- **GitHub webhooks to the hub**: plaintext events at the hub; the runner reads the state instead.

## Deviation from DESIGN.md
DESIGN.md §19 (static scripts own lanes, tests and GitHub; models never poll) is kept: drivers are those scripts on
the platform. The legacy tool's local ledger, daemon and dashboard (DESIGN.md as a whole) are replaced by the platform
stage by stage (§6); until a stage is done, none beyond ADR-001.
