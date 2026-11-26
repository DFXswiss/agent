# ADR-098: Working modes – what runs locally today becomes a selectable mode of a task, never mandatory and never on by default; the local tool and the legacy hub retire

Status: Accepted (2026-11-26, KOR-72; design only, built by the packages named in §9; the draft was reviewed by two
reviewers with fresh context – conformity with the concept, completeness of the inventory – and their findings are
worked in)

Implements [E-32](../KONZEPT.md#e-32) (nothing runs locally; every local building block becomes a selectable way of
working of the platform, never forced and never active by default; a session started with "halte dich an A38" or
"baue Feature xy, nutze den Orchestrator" carries it out completely), with the interpretation
[A19](../KONZEPT.md#a-19) (what only stores, shows or wakes up goes into the platform's basic functions and is not a
mode). Under the rules [E-04](../KONZEPT.md#e-04) and [E-27](../KONZEPT.md#e-27) (an agent can do everything a person
can), [E-05](../KONZEPT.md#e-05) (a missing prerequisite is initiated), [E-11](../KONZEPT.md#e-11) (no restriction
nobody asked for), [E-25](../KONZEPT.md#e-25) (no protection layers beyond what is needed),
[E-26](../KONZEPT.md#e-26) (simple for agents) and [E-31](../KONZEPT.md#e-31) (subscriptions only where their owner
switches them on), the concept's [§5](../KONZEPT.md#k-5) (the hub sees no content), [§10](../KONZEPT.md#k-10) (the
session executes nothing itself; every action is a controlled platform operation, checked
[formally, not by content](../KONZEPT.md#k-10-b7)), [§12 Punkt 5](../KONZEPT.md#k-12-b5) (content-free pings),
[Idee 34](../KONZEPT.md#k-idee-34) (one delegated main orchestrator per user), [Idee 42](../KONZEPT.md#k-idee-42)
(roles keep work, review and release apart) and [Idee 43](../KONZEPT.md#k-idee-43) (one main agent per task, fresh
context), and the interpretations [A2](../KONZEPT.md#a-2) (branches are updated by merge commit, a rebase only after
an explicit decision), [A3](../KONZEPT.md#a-3) (the execution ban concerns platform sessions, not the user's
computer) and [A15](../KONZEPT.md#a-15) (every instruction belongs to the verified person who gave it).

Amends [ADR-020](020-notifications-in-app-and-session-pings.md) (a content-free outside channel, §5),
[ADR-021](021-single-agent-software-v1.md) only by naming the open point of §5 (no second agent software is
decided here) and [CUTOVER.md](../CUTOVER.md) §7 (preconditions for retiring the legacy hub, §6).

## Context
- **What runs locally today** (inventory with evidence, gaps and effort per building block and per command: the
  package report of KOR-72, kept outside the repository because it names devices). Three groups:
  1. The legacy tool `agent` (`src/agent_cli`, about 77 000 lines): a ledger of sessions, tasks, rounds and checks in a
     loopback PostgreSQL synchronised with the legacy hub; a device daemon (LaunchAgent) with the knock loop and one
     coordinator loop per configured worker; the issue coordinator with lanes, orders, preconditions and conflict
     mandates; A38 (test report, review records, the PR guard, local CI comments, workflow and environment
     approvals); AI accounts (pools, logins, model slots, token keep-alive, usage readers); error-fix, mail and
     Telegram acts; a loopback dashboard; skills that tell a session how to use all of it.
  2. The legacy hub `agent-core` (`src/agent_core`): teams, the team record, passkeys, device keys.
  3. The tooling with which the platform itself is built, checked, merged and released (E-32 names it): starting
     work packages as sessions on several subscriptions, gates and end-to-end runs on test devices, a merge lane,
     release and operations steps including the signed runner package of ADR-074.
- **What the platform already has** and the modes build on: tasks with one main agent and full subagents
  ([ADR-088](088-full-subagents-and-limits-as-settings.md)); rights profiles, `review.verdict`, `github.merge` and merge
  criteria ([ADR-094](094-rights-profiles-and-agent-roles.md)); test runs in fresh VMs
  ([ADR-080](080-test-runs-as-operations.md)); the work VM ([ADR-059](059-work-vm-on-request.md)); the browser
  ([ADR-085](085-browser-as-platform-operation.md)); the pull-request index and "Fix conflicts"
  ([ADR-084](084-fix-conflicts-by-merge-commit.md)); GitHub identities ([ADR-078](078-github-identities.md)); central
  subscriptions with use per subscription and the sign-in link read from a mailbox on the device
  ([ADR-011](011-claude-subscription-login.md), [ADR-090](090-every-subscription-logged-in-centrally.md),
  [ADR-092](092-full-subscription-login-and-renewal-on-the-device.md)) and model access with fallback
  ([ADR-081](081-model-access-fallback-and-dfx-access-on-own-hardware.md)); the main orchestrator on platform hardware
  ([ADR-082](082-agent-own-identity.md) §6); the own-device runner package ([ADR-074](074-own-device-without-terminal.md));
  the situation view ([ADR-076](076-situation-view-and-attention-categories.md)); in-app notifications
  ([ADR-020](020-notifications-in-app-and-session-pings.md)); and a precedent for a brief that a task carries
  (the process analysis, [ADR-097](097-process-analysis.md) §1).
- **What is missing in general** (beyond each building block's own gap): a way to choose a way of working per task,
  platform components that do deterministic multi-step work over time (watching pull requests, trains, release
  steps) instead of a model polling, operations for a few GitHub steps (leave draft, back to draft, line comments)
  and the accesses that some ways of working need (Apple signing, production, mailboxes, Telegram).
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
    task, kept per workgroup as a template (§2);
  - `needs` – other modes it requires (§2: never switched on implicitly) and prerequisites of the register
    [spec/15-prerequisites.md](../spec/15-prerequisites.md);
  - `accesses` – the credentials of §7 the mode can use, each optional.
- The catalogue is a contract (`contracts/modes/INDEX.json`, one file per mode), versioned with the hub, readable by
  every member (`GET /v1/modes`) and by agents (`agent-platform modes list`, one line per mode, E-26).
- **Catalogue v1** (names are stable identifiers; the web shows the titles):

  | Mode | Title | Replaces locally |
  |---|---|---|
  | `a38` | A38 | the author's side of A38: `agent a38 run`, the report and review comments, review rounds of model slots `first`/`second` (the guard stays, §6) |
  | `orchestrator` | Orchestrator | starting and following work packages: plan, packages, helpers on own subscriptions, review, release proposal |
  | `coordinator` | Issue coordinator | `agent coordinate`, orders, preconditions, conflict mandates, GitHub mention intake |
  | `merge_lane` | Merge lane | precheck, train, register renumbering, one gate run per train |
  | `mac_checks` | Checks on Mac hardware | gates and end-to-end runs on test devices |
  | `release_ops` | Releases and operations | release and deploy steps, operations checks, runner package signing |
  | `notifications` | Notifications | mail and Telegram acts (content-free messages, §5) |
  | `prod_error_fix` | Error fix from production | `agent watch errors` and the error-fix skill |
  | `subscription_login` | Subscription login | browser login of subscriptions, keep-alive, usage readers of further vendors |

- Modes **add** behaviour to a task; they never narrow what the task's own sessions may do (that is a rights profile,
  ADR-094) and never widen what the person who chose them may do ([E-04](../KONZEPT.md#e-04), A3). A mode that
  starts children may give them a built-in role (A38 review children get the role `review`); ADR-094 applies
  unchanged (a child is never wider than its parent).
- **Basic functions are not modes** ([A19](../KONZEPT.md#a-19)): the ledger, the knock wake-up, the dashboard, the
  session store and the legacy hub only store, show or wake up; their platform counterparts (tasks, sessions,
  operations, pings, the web client, the situation view) exist without any choice and change no way of working. The
  legacy spine, checklists and done gates are not rebuilt; their effect is carried by operations, `review.verdict`,
  merge criteria and the drivers of the modes.

### 2. No mode without a choice
- **Nothing switches a mode on by itself**: no default at installation, for an organization, a workgroup, a task
  kind, a role, a profile, a template, a repository or a catalogue update; no "last choice remembered"; no mode on a
  subagent that its task does not have. A workgroup may keep **settings** of a mode (A38 target repositories, the
  merge-lane recipe); they act only on tasks that chose the mode. The web may show the modes a workgroup used before
  as suggestions; a suggestion is never a choice.
- A mode that `needs` another is refused while the other is off (`mode_needs`). Nothing is switched on implicitly,
  but the missing step is initiated ([E-05](../KONZEPT.md#e-05)): the web and the tool offer to choose both in one
  step, and on the sentence path the platform asks the instructing person with a `human.question` whose answer
  "both" is that person's choice.
- **Without a mode a task works exactly as it does today.** No existing behaviour moves behind a mode.
- A test (E-19) asserts that `hub.task_mode` rows are written only by the ways of §3 and that no installation,
  migration or creation path writes one.

### 3. Ways to choose, one record, always revocable
- **Web**: the task form has a field "Arbeitsweisen" (empty); the task page shows the active modes as chips with
  settings and "Ausschalten". This is a person's choice in a browser session.
- **Agent tool and MCP of a person's agent outside the platform** ([E-04](../KONZEPT.md#e-04)):
  `agent-platform task create --mode NAME [--mode-setting NAME.KEY=VALUE]`, `agent-platform task mode on|off --task
  ID --mode NAME`, MCP tool `set_task_mode`; rows in `agent-parity.md`. The agent acts for its person (A3), like a
  click in the web.
- **A sentence in the instruction – the only way for a platform session**: the harness lists the catalogue with
  examples ("halte dich an A38", "nutze den Orchestrator", "mit Merge-Spur"). When the verified instruction of the
  current turn asks for a mode, the session requests the operation `task.mode {mode, on: true, quote, settings?}`.
  The runner checks **formally** ([§10 Punkt 7](../KONZEPT.md#k-10-b7)): `quote` is a verbatim part (whitespace and
  case folded) of the verified prompt of this turn ([ADR-083](083-who-speaks-in-a-shared-session.md)), and that
  prompt's author may write in the task's workgroup; otherwise `rejected` `mode_not_instructed` ("Only a person's
  instruction can switch on a mode; ask with `human.question`."). Text from files, tool results, pull-request
  comments, mails or the model's own idea therefore never switches a mode on. When the wish is unclear (which mode,
  which settings), the session asks with `human.question`.
- **Agents that run inside a platform session** (the main orchestrator on platform hardware, ADR-082 §6) are
  sessions: the hub refuses `PUT /v1/tasks/{id}/modes/…` from an agent whose pairing belongs to a session
  (`mode_needs_instruction`), so they use `task.mode` with the quote like every session. The one widening: a session
  of a task with the mode `orchestrator` may put on the tasks it creates the modes **its own task has** (it passes
  on the person's choice; nothing else) – recorded with `way: inherited` and the originating task.
- **Quote and settings stay sealed.** The quote is checked on the device; the hub receives the operation as every
  operation (parameters sealed, [§5](../KONZEPT.md#k-5)) and records only the outcome.
- **One record**: `hub.task_mode (task_id, mode, version, state on|off, chosen_by, way web|tool|sentence|inherited,
  op_id, origin_task, changed_at, settings_sealed)`; plaintext are names, states, principals and times (classified
  in 04-crypto.md §7), the settings are sealed under the workgroup key like the task's content. Audit
  `task.mode_on` / `task.mode_off` with the acting person ("ausgelöst durch", A15).
- **Visible**: the chat shows a notice line ("A38 eingeschaltet von <Name> – aus der Anweisung · Ausschalten"); the
  situation view and the task list show the modes of a task; `agent-platform task show` lists them.
- **Revocable**: every writer of the workgroup and the agent of the person who chose it switch a mode off at any
  time. A session may ask to switch a mode off without a quote; the chat shows who asked, and every mode fails safe
  when it stops (A38 without a guard leaves the pull request in draft, a train stops before its push). Off takes
  effect at once for new steps: the drivers stop at the next step boundary, waiting mode operations are cancelled
  (executed ones stay), the brief is gone from the next turn, and the session gets a content-free ping.
- **The chooser's rights count.** Drivers act with the rights of the person who chose the mode (A15). If that
  person loses write access to the workgroup or leaves the organization, the mode is switched off (`chooser_left`)
  and the chat says who can switch it on again.
- A mode stays on for the task – across follow-up prompts, resume ([Idee 40](../KONZEPT.md#k-idee-40)) and device
  moves – until it is switched off or the task is closed.

### 4. The session asks, platform components run
- The session executes nothing itself ([§10 Punkt 3](../KONZEPT.md#k-10-b3)); it requests operations as today.
- **Drivers** are the deterministic parts of a mode: platform code in the runner of the device that serves the task
  (the legacy design's "scripts"). A driver watches external state (pull requests, checks, a mailbox, error logs),
  advances step machines (gates, trains, review rounds, releases) and requests the same operations a session would,
  under the task's GitHub identities and the rights of the person who chose the mode. Every driver step is an
  operation record with audit ([§11](../KONZEPT.md#k-11)), and a driver never runs a model in place of the session:
  when there is work for the model (a review finding, a red gate, a new error) it gives the session a ping and a
  sealed event, resuming a paused session first ([ADR-072](072-session-context-resume.md)). **Models never poll and
  never wait.**
- **Drivers run no repository code on the device.** Everything that executes code of a repository (tests, the A38
  jobs, a renumbering recipe, a release build) runs as `test.run` in a fresh VM (ADR-080) or a VM of its kind;
  the driver only reads the result. Credentials are used by the driver outside the VM (signing an artefact the VM
  built, pushing a ref), never handed into it ([E-08](../KONZEPT.md#e-08)).
- External state is **read by the runner** (as `github.pull_status` in ADR-084), not pushed to the hub: GitHub
  webhooks to the hub would hand the hub plaintext content. Polling intervals are mode settings with a generous
  default and share one rate budget per GitHub identity.
- A mode that watches beyond one turn (A38, coordinator, notifications, error fix) keeps its task **open** while it is
  on; the task's attention record (ADR-076) shows "watching" with the next step. A paused session costs nothing; driver
  time is runner time, not VM time ([ADR-062](062-prepaid-credit-and-usage-billing.md) unchanged; a resumed session
  and every test VM are billed as usual).
- Drivers keep their state in the runner database and report it to the hub as metadata (state names, counters,
  times) plus sealed events. They survive restarts and device moves ([ADR-087](087-device-failure.md)): the new
  device takes over the driver state from the sealed events.

### 5. What "completely" means
A mode is complete when the platform does, without anything local, what the local building block does today. The
acceptance of each build package checks this list against the legacy behaviour; a known difference is named here and
stays open in [KONZEPT part D](../KONZEPT.md#d-offen) until closed.

| Mode | Complete means |
|---|---|
| `a38` | Draft right after the first signed commit; the repository's A38 policy (`.github/a38.json`) run job by job with `test.run` (commands, compose, HTTP smoke and immutable adapters) and the report posted as the local-CI comment (`docs/a38.md`, `docs/local-ci-v1.md`) for the current head; the review runs A (quality and logic, model slot `first`) and B (slot `second`), each a child with the role `review` on the provider pinned for its slot, findings as pull-request line comments and the records as `A38-REVIEW:v1` comments, a new head starts a new run; zero findings on the current head, or on an earlier reviewed commit from which the current head differs only in `.md` files; CI green on this head; leave draft where the repository has no guard. The second review may come from configured team members instead of slot `second` (asked with `human.question`, answered on the platform). **Merge stays human**: the mode never requests `github.merge`; a person may ask for one under the workgroup's merge criteria (ADR-094 §5). **Open difference:** the legacy slots pin providers of different vendors; the platform runs only Claude Code (ADR-021, [§4 Punkt 6](../KONZEPT.md#k-4-b6)), so both slots run Claude Code on different subscriptions until a second agent software is decided by the owner. |
| `orchestrator` | The task's main session is the user's main orchestrator ([Idee 34](../KONZEPT.md#k-idee-34), ADR-082 §6); if it is not, the platform initiates that person's step "Make this session my main orchestrator" (E-05). From one goal: a plan as packages with dependencies; every package is a **task** of the workgroup with its own main agent (Idee 43), started when its dependencies are done, carrying the modes of the orchestrator's task (§3); helpers use the subscriptions their owner switched on for sessions (E-31) with fallback (ADR-081); gates by `test.run` (and `mac_checks` if chosen); an independent review by a child with the role `review`; merge by `merge_lane` if chosen, else by the A38 rules or by a person; a release proposal as `human.approval`. Plan, packages and their states are visible in the situation view. |
| `coordinator` | Picks up issues and mentions for a configured GitHub login and orders created on the platform; drives each through implement, inner review, tests, the A38 steps and leave-draft as tasks; preconditions with override and skip by a person (`human.approval`); duplicate check against open pull requests; a round limit with an assessment and a question to a person; conflicts by "Fix conflicts" (ADR-084, merge commit; a rebase stays unbuilt, A2). |
| `merge_lane` | One lane per repository and base: precheck of a branch against the current base, a train of the waiting branches, renumbering of the repository's registers by a recipe in the repository (run in a test VM), one gate run, then the base moves forward. **Following A2 the train updates branches by merge commit** (ADR-084), and the base moves by `github.merge` (with the workgroup's criteria, ADR-094 §6) or, where the workgroup sets no merge rule, by a fast-forward `github.push`. A rebasing train, as the local lane does it, is the owner decision D9 (§7). A red train names the red tests and the package; nothing is forced. |
| `mac_checks` | Test runs on macOS VMs of platform Macs (`test.run` kind `macos`), with the result, log and teardown rules of ADR-080; end-to-end runs that need several VMs as one run with a declared size; capacity by the device's core and memory budget. |
| `release_ops` | A release proposal (version, changes, gates) approved by a person; the build in a fresh VM, signing and notarisation of the built artefact by the driver, deploys to the hosts the workgroup names and the operations checks afterwards, each as an operation with result and log; only with the accesses of §7. |
| `notifications` | Messages to the person (mail, Telegram) on events the person chose (needs a decision, blocked, done). **Messages are content-free** like the pings of [§12 Punkt 5](../KONZEPT.md#k-12-b5): the kind of event, the task's number and a link, never a title, prompt or output. A reply does not become an instruction; the link leads to the task, where the person answers as always (ADR-083). This amends ADR-020, which built no outside channel. |
| `prod_error_fix` | Reads new errors of the production logs the workgroup names, records each once, lets the session triage (fix, skip with reason), and fixes in an ordinary task with a draft pull request through the `a38` rules if chosen. |
| `subscription_login` | Logs in and renews every subscription on the platform device (ADR-090, ADR-092; the mailbox sign-in link exists, ADR-011) and reads the usage of further vendors the legacy tool reads; nothing is transferred from a user's computer. Using a further vendor's agent software for sessions is not part of this mode (ADR-021). |

### 6. What stays with the user, and how the legacy parts retire
- **At most one Claude session that steers agent.dfx.swiss**: either a platform session made the user's main
  orchestrator (ADR-082 §6; nothing installed, only a browser) or a Claude session on the user's computer with the
  agent tool as its connector (ADR-056, KOR-63). The connector is one package with the user's keys, started by that
  session; it runs no service and keeps no database. A connector at the hub is not offered: the hub would hold the
  keys and see content (zero knowledge). No LaunchAgent, no local database, no tmux, no local test, gate or release.
- **The legacy tool `agent`** retires per building block in three stages: (1) the platform mode is built and
  accepted; (2) the operator switches its own use over and the legacy command prints where the platform does it;
  (3) the command, its loop in the device daemon and its documentation are removed. When the last loop is gone, the
  LaunchAgent of the legacy tool is removed from the operator's computer; installs of other developers are theirs to
  remove. Every command of the legacy tool is mapped to a mode, a basic function (A19) or "dropped" with a reason in
  the package report; the repository's own instructions (`AGENTS.md`, `CONTRIBUTING.md`) change in the same stage.
- **Imports first.** Platform packages still import legacy modules: the loopback PostgreSQL helper (runner store and
  VM tooling), the signed request text, the mailbox reader of the subscription login, a store error, the legacy hub's
  signing helper and most of the legacy passkey code (relying party, WebAuthn, certificates, encoding) plus the
  record connection ([05-runner.md](../spec/05-runner.md) §1.5, [spec-deviations.md](../spec-deviations.md)). They
  are moved into the platform packages, under the coverage gate, before any legacy module is removed.
- **Repositories that use A38** keep working without a break. Their guard is a GitHub workflow of the repository
  that runs the guard pinned from this repository (`.github/actions/a38-guard`, `docs/a38-guard.md`); where Actions
  are not available, `agent pr-guard` runs it by hand. What is local is the author's side: `agent a38 run`, the report
  comment and the review comments. The mode `a38` produces **exactly these comments** (`<!-- DFX-LOCAL-CI:v1 -->`
  report, `<!-- A38-REVIEW:v1 -->` review records, posted with the task's GitHub identity), so a repository's guard
  accepts a pull request from the platform as one from a local author, and no adopting repository changes anything.
  Where a repository has the guard, the guard alone decides Ready (the mode does not call
  `github.ready_for_review` there); where it has none, the mode leaves draft itself. **The guard and the A38 author
  commands (`a38`, `local-ci`, `pr-guard`) stay** as a small separate package of this repository for authors who do
  not use the platform; they are removed only when the owner decides that no author depends on them. An author who
  wants to switch joins the platform as every user (invitation or direct admission, E-18), connects GitHub (ADR-077)
  and chooses the mode on a task of the repository's workgroup.
- **The legacy hub `agent-core`** is retired by [CUTOVER.md](../CUTOVER.md) §7 (step S12, with the owner's approval;
  no data migration, §9). This ADR adds two preconditions: the A38 second review by team members and the coordinator
  no longer go through the legacy hub (the modes `a38` and `coordinator` are accepted), and no device of the operator
  still syncs with it. Teams become organizations and workgroups by hand; the team record stays readable from its own
  database; the protocol documents (`PROTOCOL.md`, `RECORD-PROTOCOL.md`, `KEY-PROTOCOL.md`, `PASSKEY-PROTOCOL.md`)
  move to an archive directory.
- **The tooling that builds the platform** is replaced by `orchestrator`, `merge_lane` and `mac_checks` and removed
  after their acceptance; its test devices become platform devices of the operator's organization (D8).

### 7. Accesses are decisions of whoever provides them
- An access is a credential that an organization provides for a mode: a sealed credential of the organization on the
  platform devices that serve the mode (never in a session, a work VM or at the hub in plaintext), used only by drivers
  of tasks that chose the mode, audited per use and revocable at once. **Providing it is the decision**: an admin of an
  organization who adds the organization's own credential needs no further approval
  ([E-11](../KONZEPT.md#e-11)). A mode without the credential works without that part and initiates the step in the
  task (E-05: the admins are asked to add it).
- **For the operator's organization the owner has decided** (2026-11-26, on [E-32](../KONZEPT.md#e-32): "alles sofort
  umziehen, ohne zu fragen"): every access the modes need moves to the platform as the mode is built, without a further
  question. D1–D6 and D8 below are therefore decided and are provided by the operator's organization when their package
  lands. D7 and D9 are no accesses but rules of work and stay decisions of the owner. Services on the owner's computer
  that do not come from this repository are not part of the inventory and do not move (E-32).

| Point | Access | Needed by | State |
|---|---|---|---|
| D1 | Apple Developer ID signing and notarisation | `release_ops` (runner package for own devices) | decided |
| D2 | Production hosts (deploy, operations checks) | `release_ops` | decided |
| D3 | Production error logs (read) | `prod_error_fix` | decided |
| D4 | Mailboxes (sending notifications) | `notifications` | decided |
| D5 | A Telegram bot | `notifications` | decided |
| D6 | Further subscriptions for helpers (each by its owner's switch, E-31) and further vendors' usage | `orchestrator`, `a38`, `coordinator`, `subscription_login` | decided |
| D7 | GitHub accounts beyond the user's own (team second review, worker logins of the coordinator; the operator's account is never shared) | `a38`, `coordinator` | owner decides |
| D8 | Which of the operator's Macs serve as platform devices for checks (own hardware, ADR-074) | `mac_checks`, `merge_lane` | decided |
| D9 | A rebasing train with `--force-with-lease` instead of merge commits (A2) | `merge_lane` | owner decides |

### 8. New contracts and operations (built by the packages of §9)
- `contracts/modes/` (catalogue), `task.mode` (class `write`), routes `GET /v1/modes`, `PUT /v1/tasks/{id}/modes/{mode}`,
  `DELETE /v1/tasks/{id}/modes/{mode}`; device state section `task_modes`; task dependencies for `orchestrator`.
- GitHub (new): `github.ready_for_review` (leave draft), `github.set_draft` (back to draft), `github.review_comment`
  (findings on lines).
- `test.run` kind `macos` (`mac_checks`), `release.*` (`release_ops`), `notify.send` (`notifications`), `error.record`
  and `error.triage` (`prod_error_fix`).
- Each new type gets a class in `contracts/ops/classes.json` (ADR-094) and a guest feature flag for older goldens.

### 9. Build order
Most local work first. Each package removes the local parts named with it once accepted.

| Step | Package | Needs | Then removable locally |
|---|---|---|---|
| 1 | Mode foundation: catalogue, `task.mode` with the quote check, record, web, tool, MCP, harness sections, driver framework, "no default" test | – | – (prerequisite) |
| 2 | `orchestrator` plan and packages · `mac_checks` · `a38` test report · `subscription_login` rest | 1 | local A38 runs, browser login and keep-alive, gates on test devices |
| 3 | `orchestrator` helpers, review and release proposal · `merge_lane` · `a38` review runs and Ready · `notifications` (D4, D5) | 2 | the package dispatcher and its helpers on local subscriptions, the merge scripts, local review lanes, mail and Telegram acts |
| 4 | `coordinator` pickup and orders · `prod_error_fix` (D3) · `release_ops` (D1, D2) | 1, 3 | coordinator loops, error watch, release and operations scripts |
| 5 | `coordinator` duplicates and conflicts | 4 | conflict mandate |
| 6 | Retire the legacy tool (imports moved first, the guard package kept) and the legacy hub (CUTOVER §7) | 2–5 | the legacy LaunchAgent, its database and dashboard |

## Consequences
- A person or an agent chooses a way of working with a sentence, in the web or with the tool; the choice is visible,
  attributed and switched off with one action. A task without a choice behaves as before.
- The deterministic parts move from local scripts into runner drivers; the session still only asks, and repository
  code still runs only in VMs.
- New plaintext at the hub: mode names, states and driver states per task (no content).
- The legacy tool and hub stay until each mode is accepted; repositories that use A38 change nothing, and the guard
  package stays for authors outside the platform.
- Accesses stay with whoever provides them; modes are useful without them and say what is missing.

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
- **Replies by mail or Telegram as instructions**: a mail sender is easy to forge and the content would leave the
  platform; the link back to the task keeps instructions signed (ADR-083).

## Deviation from DESIGN.md
DESIGN.md §19 (static scripts own lanes, tests and GitHub; models never poll) is kept: drivers are those scripts on
the platform. The legacy tool's local ledger, daemon and dashboard (DESIGN.md as a whole) are replaced by the platform
stage by stage (§6); until a stage is done, none beyond ADR-001.
