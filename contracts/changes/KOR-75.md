# KOR-75 – contract changes

ADR-027 section 3 (additive). Decision: ADR-098 (working modes), step 1 of its §9.

## Catalogue (`modes/`)
- New directory: `modes/INDEX.json` (the catalogue, one data file per mode), `modes/mode.json` (schema of an entry:
  `name, version, title{de,en}, summary{de,en}, brief, operations, drivers, settings, needs{modes, prerequisites},
  accesses, passes_on, test`), `modes/test.json` (the only mode of catalogue v1, listed only by a hub configured for
  tests). `settings` admits only the empty closed object until the first mode with settings.

## Common (`common.json`)
- New `mode_name` (`^[a-z][a-z0-9_]{0,31}$`), `mode_way` (`web`, `tool`, `sentence`, `inherited`), `task_mode {mode,
  version, chosen_by, way, origin_task, changed_at}`, `mode_driver_state {step, counters, next_at, at}`,
  `device_task_mode {mode, version, chosen_by, way, changed_at, driver}`.

## Operations (`ops/`)
- New type `task.mode {mode, on, quote?}` → `{mode, on}` (class `write`, not deduplicated) with its
  `operation.params` variant; operation error codes `mode_not_instructed`, `mode_needs`.

## API (`api/INDEX.json`, section 4.15 of 03-api.md and 6.1)
| Endpoint | Right | Purpose |
|---|---|---|
| `GET /v1/modes` (`modes-list`) | `user` | the catalogue this hub offers |
| `PUT /v1/tasks/{task}/modes/{mode}` (`task-mode-put`) | `wg:write` | switch a mode on (web, tool, inherited); `403 mode_needs_instruction`, `409 mode_needs` |
| `DELETE /v1/tasks/{task}/modes/{mode}` (`task-mode-delete`) | `wg:write` | switch a mode off |
| `PUT /v1/device/tasks/{task}/modes/{mode}` (`device-task-mode-put`) | `device:wg` | the outcome of an executing `task.mode` (way sentence) |
| `PUT /v1/device/tasks/{task}/modes/{mode}/driver` (`device-task-mode-driver-put`) | `device:wg` | the driver's state (metadata) |

- `POST /v1/tasks` takes the optional `modes` (names); `task_row` adds the optional `modes` (`task_mode` list).
- `GET /v1/device/state` adds the section `task_modes {api: 1, tasks: {task_id: [device_task_mode]}}`.
- Error codes `mode_needs` (409), `mode_needs_instruction` (403).

## Audit (`audit/`, `enums/audit-actions.json`)
- `task.mode_on {mode, version, way, chosen_by, operation_id, origin_task}`, `task.mode_off {mode, reason
  (person|session|chooser_left), chosen_by, operation_id}` (group `task`).
