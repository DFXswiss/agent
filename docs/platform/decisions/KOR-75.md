# KOR-75 – Mode foundation (ADR-098 step 1): draft and decisions

Package: the foundation of working modes – catalogue, choice, record, visibility, revocation and the driver frame –
with no mode but a test mode. Rules [E-32](../KONZEPT.md#e-32) (selectable, never forced, never on by default),
[E-33](../KONZEPT.md#e-33) (independent work runs in parallel, each in its own sandbox), [E-04](../KONZEPT.md#e-04)
(web and tool can do the same), [E-05](../KONZEPT.md#e-05), [E-19](../KONZEPT.md#e-19), [E-25](../KONZEPT.md#e-25)
(no protection beyond what is needed); the owner's rule given with the package order ("what the platform can do itself
is a platform tool and needs no VM; a session never sees the secret of an access" – „wenn ein agent einen unbuntu
braucht um ein e-mail postfach abzurufen dann haben wir was falsch gemacht“; recorded as E-34 on the documentation
branch, whose number develop still gives to the CI runner rule). Design:
[ADR-098](../adr/098-working-modes.md) §§1–4 and 8, step 1 of §9; §7 as of the branch `ad5f2026-doc-10` (D1–D6 and
D8 decided, D7 and D9 open – neither is touched by this package, no access is built here).

## 1. Cut

| Part | What | Where |
|---|---|---|
| Catalogue | `contracts/modes/INDEX.json` (names and files), `contracts/modes/mode.json` (schema of an entry), one data file per mode; v1 holds only `test` | contracts |
| Choice by a person | `GET /v1/modes`; `PUT`/`DELETE /v1/tasks/{task}/modes/{mode}` (browser and agent principals, `wg:write`); `modes` on `POST /v1/tasks` | hub |
| Choice by a sentence | operation `task.mode {mode, on, quote?}` (class `write`); quote check in the runner's pre-check; the executor records it through `PUT /v1/device/tasks/{task}/modes/{mode}` | runner, hub |
| Tool path closed for session agents | an agent whose pairing belongs to a session (`hub.agent.session_id`) gets `403 mode_needs_instruction` on the person routes, except `inherited` (below) | hub |
| Record | `hub.task_mode` + audit `task.mode_on` / `task.mode_off` + column classification (04-crypto.md §7, `e2e/platform/checks/hub_columns.py`) | hub |
| Rights loss | subscriber of `log.appended`: a chooser without `wg:write` (left, removed, narrowed) switches the mode off, reason `chooser_left` | hub |
| Device state | section `task_modes` `{api, tasks: {task_id: [{mode, version, chosen_by}]}}` for the tasks of the device's live sessions | hub, runner |
| Harness | one rules section per mode in `images/harness/RULES.md` (`## Working mode: test`) plus the catalogue sentence; the runner adds the line "Working modes of this task: …" to the context block (request amender) | guest rules, runner |
| Web | task form field "Arbeitsweisen" (empty), chips on the task page with "Ausschalten", the modes in the task list and the situation view, chat notice line | web |
| Agent tool | `agent-platform modes list`, `task create --mode`, `task mode on|off --task --mode`, `task show` lists modes; MCP `set_task_mode`; rows in `agent-parity.md` | agent tool |
| Driver frame | runner area `modes`: registry of drivers per mode, state in `runner.mode_driver`, steps as ordinary operations of the task's main session (`operations.records.insert`, the existing pipeline), wake-up and resume, take-over after a device move, one rate budget per GitHub identity, no repository code (structural test) | runner |
| Test mode | `test`: listed only where the hub is configured with `HUB_MODES_TEST=1` (E2E, development); its driver advances a counter by one step per run and wakes the session once, which is enough to test the frame | contracts, runner |
| Tests | unit tests at 100 % (gate), contract fixtures, "no default" test, one E2E spec | tests |
| Spec | `docs/platform/spec/17-working-modes.md`; rows in 02-data-model, 03-api, 04-crypto §7, 05-runner | docs |

Out of the cut: every real mode, new GitHub operations (`github.ready_for_review` …), accesses (§7), task dependencies,
mode **settings** (no mode of v1 has any; the column `settings_sealed` exists and stays NULL, the catalogue's
`settings` schema of `test` is the empty closed object – the first mode with settings seals them under the workgroup
key like task content).

## 2. Reused, not rebuilt

- Operations, their classes and profiles (ADR-094): `task.mode` is one more type of class `write`; profiles restrict
  it like any write.
- The operation pipeline (05-runner.md §12): checks, pre-check, registration, execution and result; driver steps go
  through it as operations of the task's main session (`records.insert` in state `received`).
- The acting user (`credentials.acting`, ADR-083): the turn's verified author is the chooser of a sentence; the
  prompt events of the turn (`runner.prompt_event`, opened only into memory) are what the quote is checked against.
- Request amenders (`sessions.extensions`, as the profiles area does) for the harness line.
- Device-state sections (`<area>/device_state.py:SECTION`), signals (`log.appended`), jobs, `Effects`, audit chain,
  `rights.require`.
- Pings and resume (ADR-020, ADR-039): a driver wakes the session with a content-free ping; a suspended session is
  resumed and continued with a platform sentence through the path of ADR-081 (`prompts.resume` + `speaker.register`
  with a stable prompt ID, as `billing.pause` and `model.fallback` do).
- Device failure take-over (ADR-087): the new device reads the reported driver state from the hub.

## 3. Contracts

- `contracts/modes/INDEX.json`, `contracts/modes/mode.json`, `contracts/modes/test.json`.
- `api/modes-list.response.json`, `api/task-mode-put.{request,response}.json`,
  `api/task-mode-delete.response.json`, `api/device-task-mode-put.{request,response}.json`, rows in `api/INDEX.json`;
  `task_row.modes` in `api/objects.json`; `modes` on `task-create.request.json`.
- `ops/task.mode.{params,result}.json`, class `write`, not deduplicated.
- `audit/task.mode_on.json`, `audit/task.mode_off.json`; enums: audit actions, error codes `mode_unknown`,
  `mode_needs`, `mode_not_instructed`, `mode_needs_instruction`, operation error `mode_not_instructed`.
- Device state field `task_modes`.
- `changes/KOR-75.md`, `VERSION`.

## 4. Migrations

- Hub `0040_task_modes.sql`: `hub.task_mode (task_id, mode, version, state on|off, chosen_by, way
  web|tool|sentence|inherited, op_id, origin_task, changed_at, changed_by, reason, settings_sealed)`, primary key
  `(task_id, mode)`; additive, nothing written by the migration.
- Runner `00NN_mode_driver.sql`: `runner.task_mode` (the device's copy of the section) and `runner.mode_driver
  (task_id, mode, state, step, counters, next_at, updated_at)`.

## 5. Tests

- Hub: routes (rights, unknown mode, `mode_needs`, session agent refused, inherited allowed only for modes of the
  origin task with `passes_on`), device route (operation must be this device's executing `task.mode` of the task),
  `chooser_left`, device-state section, audit details, backup tables, schema classification.
- Runner: quote check (verbatim with whitespace and case folded; only the verified prompts of the current turn; an
  author without `wg:write`; a child session has no prompt of a person – refused), executor, amender, driver frame
  (steps as operations, stop at the next step boundary when off, wake and resume, take-over, rate budget).
- "No default" (E-19, ADR-098 §2): only the three write paths (person route, device route, inherited) write
  `hub.task_mode`; no migration, installation or task creation without an explicit list writes a row.
- Web (unit and component), agent tool (CLI and MCP), contract harness, one E2E spec (choose by sentence, see the chip,
  switch off).

## 6. Decisions

1. **The test mode is hidden unless configured.** E-32 forbids defaults, and a "Testmodus" in production would be a
   choice nobody needs; the hub lists it only with `HUB_MODES_TEST=1`.
2. **The hub learns a sentence's outcome through a device route, not from the operation.** Parameters and results are
   sealed (§5); the runner's executor names mode and state in plaintext metadata, and the hub accepts it only for an
   executing `task.mode` operation of that task on that device, with the operation's `triggered_by` as chooser.
3. **`passes_on` instead of the name `orchestrator`.** The inherited way is keyed by a catalogue flag that
   `orchestrator` will carry; the test mode carries it so the path is tested now.
4. **Rights loss by signal.** The `log.appended` signal of the membership log is the moment a chooser can lose
   `wg:write`; no polling job.
</content>
</invoke>
