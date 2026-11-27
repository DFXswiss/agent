# 17 – Working modes

Design: [ADR-098](../adr/098-working-modes.md); rule [E-32](../KONZEPT.md#e-32); built by KOR-75 (step 1 of ADR-098
§9: the foundation, with no mode but a test mode). Decisions of the build: [decisions/KOR-75.md](../decisions/KOR-75.md).

## 1. Catalogue

- `contracts/modes/INDEX.json` lists the modes in order, one data file each (`contracts/modes/<name>.json`), every
  entry valid against `contracts/modes/mode.json#/$defs/mode`: `name` (stable identifier), `version`, `title` and
  `summary` (de, en), `brief` (the heading `Working mode: <name>` of its section in `images/harness/RULES.md`),
  `operations`, `drivers`, `settings` (a JSON schema; catalogue v1 admits only the empty closed object), `needs`
  (`modes`, `prerequisites` of [15-prerequisites.md](15-prerequisites.md)), `accesses` (D1–D9 of ADR-098 §7),
  `passes_on` (a session of a task with the mode may pass it on to the tasks it creates), `test`.
- Catalogue v1: `test` only. The hub lists an entry with `test: true` only with `AGENT_HUB_MODES_TEST=1` (03-api.md
  §2); E2E and development set it, production does not.
- Hub and runner keep copies (`agent_hub.modes.catalog`); a test holds them equal to the contract. A new mode or a new
  version never switches anything on.

## 2. Choosing and switching off

| Way | Who | Route | `way` |
|---|---|---|---|
| Web | a writer of the workgroup in the browser | `PUT /v1/tasks/{task}/modes/{mode}`, `modes` of `POST /v1/tasks` | `web` |
| Agent tool, MCP `set_task_mode` | the person's agent outside the platform | the same routes | `tool` |
| Sentence | a session, with a quote of the verified instruction of its turn | operation `task.mode` → `PUT /v1/device/tasks/{task}/modes/{mode}` | `sentence` |
| Passing on | an agent paired inside a session of a task that has the mode (`passes_on`), for a task it created | the person routes | `inherited` |

- An agent whose pairing belongs to a session gets `403 mode_needs_instruction` on the person routes for anything
  else. A mode whose `needs` are off is `409 mode_needs` with `detail.modes`; modes named together satisfy each other.
- Switching off: `DELETE /v1/tasks/{task}/modes/{mode}` (every writer and every agent of a writer, also a session's
  agent) or `task.mode {on: false}` (any session, no quote). Off takes effect at once: the device state no longer
  lists the mode, the driver stops at its next step boundary, the brief is gone from the session's next start or
  resume (as a session notice of a resumed conversation).
- **The chooser's rights count**: after every membership-log append (signal `log.appended`) the hub switches off each
  mode whose `chosen_by` lost `wg:write` on the task's workgroup (reason `chooser_left`, actor the hub).

## 3. The sentence (`task.mode`)

- Parameters `{mode, on, quote?}` (class `write`); `on: true` needs `quote` (3–1000 characters).
- The runner's pre-check (`agent_runner.modes.quote`): the newest prompt accepted into the requesting **main**
  session whose verified author is the operation's acting user (the person of the turn) is opened into memory, and
  the quote must be a part of it with white space collapsed and case folded. Otherwise `rejected`
  `mode_not_instructed` ("Only a person's instruction can switch on a mode; ask with human.question."). A child
  session, a turn of several persons and an unreadable prompt never pass.
- The executor reports the outcome with `PUT /v1/device/tasks/{task}/modes/{mode}` `{on, operation_id}`. The hub
  accepts it only for an `accepted`/`executing` `task.mode` of that task registered by the calling device; switching
  on needs the operation's `triggered_by` to hold `wg:write` (`403`), who becomes `chosen_by`. Refusals become the
  operation's error (`not_found`, `rights_denied`, `mode_needs`). The quote never reaches the hub.

## 4. Record, audit, visibility

- `hub.task_mode` (migration 0042, 02-data-model.md): one row per task and mode ever chosen – `state`, `version`,
  `chosen_by`, `way`, `op_id`, `origin_task`, `changed_at`, `changed_by`, `reason`, `driver_state`, `settings_sealed`
  (always NULL in v1). Plaintext classification: 04-crypto.md §7 ("Working modes").
- Audit: `task.mode_on {mode, version, way, chosen_by, operation_id, origin_task}`, `task.mode_off {mode, reason,
  chosen_by, operation_id}`, `task.mode_driver {mode, step}` (a new driver step), `read.modes`.
- The task row carries `modes` (the modes that are on; absent without), so the task list, the situation view and
  `agent-platform task show` show them; the web shows chips with "Ausschalten" and a notice line in the task header.

## 5. Brief

- `images/harness/RULES.md` has the section "Working modes" (how a mode is chosen, quoted, refused and switched off)
  and one section per catalogue entry ("Working mode: test"); a test checks every entry has its section.
- The runner adds to the context block of a session start the line "Working modes of this task: …" naming the
  sections (request amender `modes.brief`). Without a mode nothing is added.

## 6. Drivers

- Device state section `task_modes {api: 1, tasks: {task_id: [{mode, version, chosen_by, way, changed_at, driver}]}}`
  for the tasks with live sessions on the device; the runner keeps it (`runner.task_mode`, migration 0040).
- `agent_runner.modes.drivers`: a driver per mode (`DRIVERS`); `run_once` (reconcile step `mode_drivers`) works each
  due mode once: state in `runner.mode_driver` (take-over from the hub's last reported state on a new device), rate
  budget per GitHub identity (`runner.mode_rate`, 600 per hour for all drivers of the device), the step, its
  operations recorded as operations of the task's main session with the chooser as acting user (the ordinary
  pipeline: checks, registration, audit, execution), the wake-up (a paused session is resumed and gets a content-free
  platform sentence naming the operation, the continuation path of ADR-081) and the report `PUT
  /v1/device/tasks/{task}/modes/{mode}/driver` (metadata only; `{on: false}` stops the driver).
- Drivers run no repository code and start no process (a structural test); repository code runs only as `test.run` in
  a fresh VM.
- The test driver requests `github.list_identities` once and wakes the session with it.

## 7. Open

- Mode settings (sealed under the workgroup key) – with the first mode that has settings.
- A guest feature flag for `task.mode` on older goldens (they refuse the unknown type locally).
- Sealed driver events beside the metadata state – with the first driver that needs content.
- D7 and D9 of ADR-098 §7 stay open decisions of the owner; nothing of this package depends on them.
