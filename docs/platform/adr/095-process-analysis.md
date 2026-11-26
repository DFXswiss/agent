# ADR-095: Process analysis – a button starts a read-only analysis task over authorised usage, audit, operations and metrics

Status: Accepted (2026-11-26, after a review of the draft with fresh context; its findings are worked in)

Implements [Idee 33](../KONZEPT.md#k-idee-33) ("Ein Dashboard-Button „Prozesse optimieren“ startet eine separate
Analyse-Session mit Zugriff auf autorisierte Logs und Metriken … Keine automatischen Systemänderungen"),
[§2 Punkt 5](../KONZEPT.md#k-2) (every request is a task, the analysis too), [§8 Punkt 5](../KONZEPT.md#k-8-b5) and
[§8 Punkt 6](../KONZEPT.md#k-8-b6) (only authorised reads, every read logged), interpretation
[A4](../KONZEPT.md#a-4) (usage figures are plaintext at the hub), under the rules [E-04](../KONZEPT.md#e-04)
(agents reach everything people reach), [E-05](../KONZEPT.md#e-05) (a refusal names what resolves it),
[E-11](../KONZEPT.md#e-11) (no restriction nobody asked for) and [E-19](../KONZEPT.md#e-19) (bounded, paged reads).
Builds on [ADR-089](089-sessions-read-like-their-human.md) (sessions read like their human) and
[ADR-094](094-rights-profiles-and-agent-roles.md) (rights profiles).

## Context
- The raw data exist at the hub in plaintext metadata: per-session usage counters and charges (KOR-31, A4), the audit
  chain (KOR-40), operation rows, the session tree and the monitoring series. A session can read tasks, chat,
  whiteboards and operations (ADR-089), but no usage, audit or metrics, and operations only per task.
- A profile that allows only reading exists (ADR-094 `read_only`); nothing assigns it to a task at creation.
- Hub and devices are end-to-end encrypted. Content (titles, prompts, output) lives in sealed events; usage, audit,
  operation and metric rows carry IDs, numbers, states and times only. Brauchbare Vorschläge must come from these
  metadata without content of other workgroups.

## Decision

### 1. The analysis is a task of the kind "analysis"
- `POST /v1/tasks` takes an optional `analysis {scope: "org"|"wg", since, until}` (plaintext). The hub stores it on
  the task (`hub.task.analysis` jsonb, null for every other task); task rows carry `analysis` (null without), so the
  web, the agent tool and the situation view mark the task as an analysis.
- **Workgroup.** The task lives in the workgroup of the request like every task (`wg:write`); the web offers the
  default workgroup for the organization button and the current workgroup for the workgroup button, both changeable.
  Scope `wg` analyses exactly the task's workgroup; scope `org` the workgroups of the organization the session may
  read (§3).
- **Period.** `since` and `until` are times (`until` default the request time, `since` default seven days before);
  `since < until`, `until` at most one minute after the hub's time, at most 92 days. Invalid → `400 request_invalid`
  with the pointer. The period is part of the prompt the client seals (`Optimize processes: …`, §6), so the session
  reads it from its instruction; the hub keeps it for display.
- **Fixed brief.** The brief is a section of the harness rules (`images/harness/RULES.md` "Process analysis"), shipped
  with the golden and named by the harness version; the session start names it for a session whose rights profile is
  `analysis` (§2). The prompt a client sends is one fixed sentence with scope and period (§6) – the same in the web
  and the agent tool.
- The rest is an ordinary task (§2 point 5): chat, follow-up prompts, close, costs, attention.

### 2. Read only, technically enforced: the built-in profile `analysis`
- The hub assigns the analysis task, **in the creating transaction**, the snapshot of the built-in profile `analysis`:
  the operation types of the classes `read`, `delegate`, `ask` (the same classes as `read_only`), reach `all` for scope
  `org` and `workgroup` for scope `wg`. `analysis` becomes a built-in role name (reserved; listed with the others; a
  person may also assign it to a task like every role). No class `write`, `run`, `scan`, `review`, `merge`: no
  repository, issue, task title, whiteboard, setting or merge changes; no work VM, test run or browser.
- **No work VM** (the package let the design decide): the new tools return aggregated, paged metadata the model reads
  directly; a work VM would add a second place where the hub's metadata of several workgroups lies, with internet
  access and without credentials, for no analysis the tools cannot answer. A person who wants a computation over
  exported figures starts an ordinary task.
- Enforcement is ADR-094's, three times: the hub (H2b, `rights_denied`, audited), the runner (pre-check, `rejected`,
  audited) and the guest (local refusal, work VM and browser tools not offered). Subagents of the analysis are
  bounded by it (intersection along the chain).
- **The profile of an analysis task cannot be changed or cleared**: `PUT /v1/tasks/{task}/agent-profile` on a task
  with `analysis` answers `409 version_conflict` with detail `{"reason": "analysis"}` (the web and the tool say:
  "An analysis task always reads only; start an ordinary task to change something."). The task is created only
  while every active device assigned to a workgroup of the organization announced `agent.profiles` and
  `platform.analysis` (the new read tools; `409 peer_outdated` with the devices otherwise, ADR-094 §3).
- Settings are not operations: a session has no route to change settings at all. Whiteboard writes of a session are
  not operations either – the analysis session writes nothing but its chat output.

### 3. Four new read tools (03-api.md §8.1), for every session
They are ordinary platform read tools (class `read` in `contracts/ops/classes.json`), available to every session
under the read scope of ADR-089, not only to analyses (E-11: what a person may read, their AI may read). Each reads
**one workgroup** – `workgroup_id`, default the session's – through `ReadScope.readable`: owner ∩ instructing human,
effective access, device assigned to the workgroup, within the profile's reach; otherwise `not_found`, `forbidden` or
`unavailable` exactly as ADR-089 (an unreadable workgroup is absent, never partly visible). Organization-level rows
(audit rows without a workgroup, the organization's billing) are not read by sessions: for people they are an admin
view, and a figure that is not of a workgroup cannot be bound to the intersection.

| Tool | Arguments | Result |
|---|---|---|
| `get_usage` | `{workgroup_id?, since, until?, by?: "task"\|"session", before?, limit? ≤ 200}` | `{workgroup_id, since, until, by, total, rows, next_before}`; one row per task (`by` `task`, default) or session started in the period: requests, input/output/cache tokens, provider cost and charged amount in micro-USD, runtime, operations and failed operations; session rows add `task_id`, `parent_session_id`, `depth`, `state`, `model_id`, `created_at`, `ended_at` – the subagent tree across tasks. `total` covers the whole period |
| `list_audit` | `{workgroup_id?, since?, until?, action?, task_id?, session_id?, before?, limit? ≤ 200}` | `{rows:[{seq, at, actor_kind, actor_id, action, result, task_id, session_id, target_kind, target_id, detail}], next_before}` – the audit rows of the workgroup newest first; `action` is the action or a dotted group (`operation`); `detail` as stored (IDs and codes, never content) |
| `list_workgroup_operations` | `{workgroup_id?, since?, until?, type?, state?, before?, limit? ≤ 200}` | `{operations:[{operation_id, task_id, session_id, type, state, error_code, created_at, finished_at}], next_before}` – operations of every task of the workgroup, newest first |
| `get_metrics` | `{workgroup_id?, entity_kind: "session"\|"vm"\|"workvm", entity_id, metric, since, until?, step}` | `{entity_kind, entity_id, metric, points:[[ts, value]]}` – the monitoring series of a session, workgroup VM or work VM of the workgroup (the rules of `GET /v1/monitoring/series`: metrics, steps, rollups, at most 1000 points) |

- Every page is bounded (default 50, at most 200) with an opaque cursor; a period is at most 92 days. Large audits are
  read page by page (E-19); the result never cuts silently.
- The answer comes from new hub device routes with right `device:wg` (§4); the runner checks every row of the answer
  (organization and workgroup match, the contract's form) and drops the rest, as for tasks (ADR-089 §2: a hub cannot
  widen a read).
- **Audit.** Every call is a `read.session.<tool>` audit row with `read_wg_id` and `triggered_by` and a `platform.read`
  event (03-api.md §8), the refusals included; the hub writes its read log `read.device.workgroup.<resource>` for each
  device route. So the read log names who read what and on whose instruction.
- `list_operations` and `get_task` stay as they are; `get_task` answers the session tree of one task with states,
  `get_usage` by session the tree across tasks with figures.

### 4. Hub device routes (03-api.md §6.1)
`GET /v1/device/workgroups/{wg}/usage`, `/audit`, `/operations`, `/metrics` (`device:wg`; an unknown or unassigned
workgroup `404`; read resources `device.workgroup.usage|audit|operations|metrics`). Query parameters mirror the tool
arguments (`since`, `until`, `by`, `action`, `task`, `session`, `type`, `state`, `entity_kind`, `entity_id`, `metric`,
`step`, `before`, `limit`). They read existing tables only (`hub.session`, `hub.usage_counter`, `hub.charge`,
`hub.operation`, `hub.audit`, `hub.metric_sample`/`metric_rollup`); audit and billing logic are not changed. New
indexes keep them bounded: `audit (wg_id, seq DESC)`, `operation (wg_id, created_at DESC, id DESC)`, `session (wg_id,
created_at DESC, id DESC)`.

Usage semantics: rows are the sessions (or tasks with sessions) **started** in the period with their whole counters
(`hub.usage_counter`) and their charges (`hub.charge`, any time) – a session's figures are not split at the period's
edges. Operations count the session's operation rows.

### 5. The report and "adopt"
- The harness brief tells the session: read with the new tools (and `list_workgroups`, `list_tasks`, `get_task`), then
  answer with a report in six sections in this order – Efficiency, Token usage, Repetitions, Subagents, Workflows,
  Resources –, each proposal with its evidence (task, operation or session ID, or a figure from a tool) and the
  expected effect; state where data are missing; propose, never change anything; write in the language of the
  prompt; name another workgroup's facts only as figures and IDs, never its content.
- The report is the session's chat output in the task (sealed for the task's workgroup like every output).
- **Adopt** ("übernehmen") is a person's action: the web shows on every output of an analysis task the button
  "Übernehmen", which writes the output's text as a `whiteboard.entry` of the task's workgroup or of the organization
  (the person chooses; only where the person may write), sealed and signed by the person – the existing whiteboard
  path. The session itself never writes a whiteboard (its profile has no write class). Agents:
  `agent-platform optimize adopt --task T [--level workgroup|org]` (the newest output of the task).

### 6. Clients
- **Web.** The situation view (organization) and the workgroup view get the button "Prozesse optimieren", shown to a
  person who may write a workgroup. It opens a small dialog: workgroup (default as §1), period (7 days preset; 1, 7,
  30, 90 days), a sentence on who will read the report (§7), and "Starten". It creates the task through the ordinary
  path (`prepareTask`/`sendTask`, sealed `task.meta` title `Optimize processes` and the prompt) with `analysis`, and
  opens it. Task rows show a badge "Analyse"; the profile line shows `analysis` without the change button.
- **Prompt (fixed).** `Optimize processes: analyse <the organization|workgroup <name>> from <since> to <until> and
  propose improvements.` (times ISO-8601 UTC), the same text in web and tool (`contracts/tasks/analysis.json` vectors,
  tested on both sides).
- **Agents (E-04).** `agent-platform optimize (--org ORG | --wg WG) [--days N | --since T] [--until T]` creates the
  analysis task (scope `org` in the organization's default workgroup or `--in WG`; scope `wg` in WG) and answers
  like `task create`; `optimize adopt` as §5. Rows in `agent-parity.md`.

### 7. Privacy between workgroups
- A session reads only what both the person who pressed the button (the task owner) and the instructing person may
  read, on a device assigned to that workgroup (ADR-089). A workgroup without that right is absent from every answer;
  one the device does not serve answers `unavailable` with the remedy (E-05).
- The tools return metadata only (IDs, numbers, states, times, audit codes). Titles and chat of other workgroups are
  readable through the existing tools under the same rule; the brief tells the session not to need them.
- **The report lands in the task's workgroup** and is readable there by every reader (ADR-089 §5, like a person
  copying a figure). Scope `wg` reads only its own workgroup (reach `workgroup`), so nothing crosses. For scope `org`
  the dialog says, before the start, "Der Bericht ist für alle Mitglieder der Workgroup <name> sichtbar und kann
  Kennzahlen aller Workgroups enthalten, die du lesen kannst." and lets the person choose a smaller workgroup; the
  agent tool's answer names the same.

## Consequences
- One click gives a report with evidence; nothing changes without a person.
- Every session can read usage, audit, operations and metrics of the workgroups its people may read – useful beyond
  the analysis (cost questions, failure patterns).
- An older guest (golden before this ADR) has no new tools and no brief; the session still has the `analysis`
  profile and the prompt. Rollout: hub, runner (announces `platform.analysis`), golden.

## Alternatives considered
- A separate session type outside tasks: contradicts §2 point 5 and duplicates chat, costs and attention.
- The brief in the prompt the client sends: two copies (web, tool) and a long text in every analysis chat; the
  harness carries rules anyway.
- Hub-side analysis (SQL report without a model): no suggestions, and the hub would compute across workgroups without
  the intersection of people's rights.
- A stricter read for analyses (only what every reader of the task's workgroup may read): the default workgroup has
  every member, so an admin's analysis would see almost nothing (E-11); the dialog's sentence and the choice of a
  workgroup give the person the decision.
- Reading organization-level audit rows for admins: possible later; the intersection with the instructing human and
  the absence of a workgroup make it a separate rule.

## Deviation from DESIGN.md
None beyond ADR-001.
