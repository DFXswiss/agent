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
D8 decided, D7 and D9 open – neither is touched by this package, no access is built here). Specification:
[17-working-modes.md](../spec/17-working-modes.md).

## 1. Cut

| Part | What | Where |
|---|---|---|
| Catalogue | `contracts/modes/INDEX.json`, `contracts/modes/mode.json` (`$defs/mode`), one data file per mode; v1 holds only `test` | contracts |
| Choice by a person | `GET /v1/modes`; `PUT`/`DELETE /v1/tasks/{task}/modes/{mode}` (browser and agent principals, `wg:write`); `modes` on `POST /v1/tasks` | hub (`agent_hub.modes`) |
| Choice by a sentence | operation `task.mode {mode, on, quote?}` (class `write`); quote check in the runner's pre-check; the executor records it through `PUT /v1/device/tasks/{task}/modes/{mode}` | runner, hub |
| Tool path closed for session agents | an agent whose pairing belongs to a session (`hub.agent.session_id`) gets `403 mode_needs_instruction`, except `inherited` | hub (`modes.choice`) |
| Record | `hub.task_mode` (migration 0040) + audit `task.mode_on`/`task.mode_off`/`task.mode_driver`/`read.modes` + column classification (04-crypto.md §7, `e2e/platform/checks/hub_columns.py`) + export rule | hub |
| Rights loss | subscriber of `log.appended`: a chooser without `wg:write` switches the mode off (`chooser_left`) | hub (`modes.signals`) |
| Device state | section `task_modes {api, tasks}` for the tasks of the device's live sessions, with the last reported driver state | hub, runner (`modes.state`, migration 0038) |
| Harness | section "Working modes" and "Working mode: test" in `images/harness/RULES.md`; the runner adds "Working modes of this task: …" to the context block (request amender) | guest rules, runner |
| Web | task form field "Arbeitsweisen" (empty), chips with "Ausschalten" on the task page, modes in the task list and the situation view, a notice line in the task header | web |
| Agent tool | `agent-platform modes list`, `task create --mode`, `task mode on|off --task --mode`, `task show` lists modes; MCP `set_task_mode`; rows in `agent-parity.md` | agent tool |
| Driver frame | `agent_runner.modes.drivers`: registry, state in `runner.mode_driver`, steps as operations of the task's main session, wake-up and resume, take-over, rate budget per GitHub identity, report to the hub; no repository code (structural test) | runner |
| Test mode | `test`: listed only where the hub runs with `AGENT_HUB_MODES_TEST=1`; its driver requests `github.list_identities` once and wakes the session with it | contracts, runner |
| Tests | hub, runner, tool and web suites; contracts; "no default" (`tests/platform/test_modes_no_default.py`) | tests |

## 2. Reused, not rebuilt

- Operations, their classes and profiles (ADR-094): `task.mode` is one more type of class `write`.
- The operation pipeline (05-runner.md §12): driver steps are recorded with `operations.records.insert` in state
  `received` and go through checks, registration, audit, execution and result like a session's request.
- The acting user (`credentials.acting`, ADR-083): the stamped `actor_id` is whose instruction is quoted, the hub's
  `triggered_by` is the chooser; prompts are opened with `prompts.verify.open_text`.
- Request amenders (`sessions.extensions`) for the harness line, as the profiles area does.
- Device-state sections, signals (`log.appended`, bound context of `device.signals`), `Effects`, the audit chain,
  `rights.can`/`require`, `task_for_write`, `audit.recorder.unchanged`.
- The continuation after a model or credit pause (ADR-081 §5; `billing.pause`, `model.fallback`): the wake-up resumes
  a paused session with `deliverer.resumer` and hands a platform sentence with a stable prompt ID through
  `prompts.speaker.register`; `pings.send.stable_ping_id` gives the ID.
- Device failure take-over (ADR-087): the new device starts from the driver state the hub has.

## 3. Decisions

1. **The test mode is hidden unless configured** (`AGENT_HUB_MODES_TEST`): E-32 forbids defaults, and a "Testmodus" in
   production would be a choice nobody needs.
2. **The hub learns a sentence's outcome through a device route, not from the operation**: parameters and results are
   sealed (§5). The hub accepts it only for an `accepted`/`executing` `task.mode` of that task registered by that
   device (the `executing` transition travels through the outbox and may arrive later), with the operation's
   `triggered_by` as chooser, who must hold `wg:write`.
3. **`passes_on` instead of the name `orchestrator`**: the inherited way is keyed by a catalogue flag that
   `orchestrator` will carry; the test mode carries it so the path is tested now. "A task it created" is the task
   whose first `task.meta` event the agent authored.
4. **Rights loss by signal**: a chooser loses `wg:write` only through the membership log, so `log.appended` is the
   moment; no polling job.
5. **The task row lists `modes` only when one is on**, so every existing row (and test) stays as it was.
6. **The driver state is metadata only** (step name, counters, times); a report is audited (`task.mode_driver`) only
   when the step changes, every other report is stored and marked unchanged (ADR-063: the drivers' operations carry
   their own audit).
7. **The wake-up is the continuation sentence of ADR-081**, not a new ping kind: a ping reaches only a running turn,
   and a new guest ping kind would need a new golden. The sentence names the operation only.
8. **The rate budget is keyed by the chooser** (`github:<user>`, 600 per hour for all drivers of the device); which
   identity a driver operation acts with is still decided by the ordinary pre-check of each operation.

## 4. Open (named, not built)

- **`docs/platform/agents.md` must receive the "Working modes" block of `src/agent_platform/guide.md`.** A test keeps
  both byte-identical and `web/public/agents.md` is built from it; platform sessions cannot see or write that file, so
  a person copies it (`cp src/agent_platform/guide.md docs/platform/agents.md`) before the merge.
- Mode settings (sealed under the workgroup key) – with the first mode that has settings.
- A guest feature flag for `task.mode` on older goldens (they refuse the unknown type locally until the golden ships
  the new contracts).
- Sealed driver events beside the metadata state – with the first driver that needs content.
- An E2E spec of the sentence path (instruction → `task.mode` → chip → off) – it needs a golden with the new rules.
