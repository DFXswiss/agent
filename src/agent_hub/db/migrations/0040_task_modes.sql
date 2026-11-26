-- 0040_task_modes: the working modes of a task (KOR-75, ADR-098 section 3; 02-data-model.md section 2.4).
--
-- One row per task and mode that was ever switched on: `state` 'on' or 'off', the catalogue `version` it was chosen
-- in, `chosen_by` (the person whose choice it is: a browser user, the user of an agent, or the verified author of the
-- instruction a session quoted), `way` ('web', 'tool', 'sentence', 'inherited'), `op_id` (the `task.mode` operation of
-- the last change by a session; always set for a sentence), `origin_task` (the task whose mode an `inherited` choice passed on), `changed_at`/`changed_by` (the last
-- change and the principal that made it, NULL for the hub itself), `reason` (why it is off: 'person', 'session', 'chooser_left'), `driver_state` (the state of
-- the mode's driver as its device reports it: step name, counters, times - metadata only) and `settings_sealed` (the
-- settings sealed under the workgroup key; no mode of catalogue v1 has settings, so it stays NULL). Names, states,
-- principals and times are plaintext metadata (04-crypto.md section 7).
--
-- Nothing writes a row by default: no migration, installation or creation path (ADR-098 section 2). Every change is
-- additive; the previous hub code starts on this schema and ignores the table.

CREATE TABLE hub.task_mode (
  task_id         uuid        NOT NULL REFERENCES hub.task (id),
  mode            text        NOT NULL CHECK (mode ~ '^[a-z][a-z0-9_]{{0,31}}$'),
  version         integer     NOT NULL CHECK (version >= 1),
  state           text        NOT NULL CHECK (state IN ('on', 'off')),
  chosen_by       uuid        NOT NULL,
  way             text        NOT NULL CHECK (way IN ('web', 'tool', 'sentence', 'inherited')),
  op_id           uuid        NULL,
  origin_task     uuid        NULL,
  changed_at      timestamptz NOT NULL,
  changed_by      uuid        NULL,
  reason          text        NULL CHECK (reason IN ('person', 'session', 'chooser_left')),
  driver_state    jsonb       NULL CHECK (driver_state IS NULL OR jsonb_typeof(driver_state) = 'object'),
  settings_sealed jsonb       NULL,
  PRIMARY KEY (task_id, mode),
  CHECK (way <> 'sentence' OR op_id IS NOT NULL),
  CHECK ((way = 'inherited') = (origin_task IS NOT NULL)),
  CHECK ((state = 'off') = (reason IS NOT NULL))
);
CREATE INDEX task_mode_chooser ON hub.task_mode (chosen_by) WHERE state = 'on';

REVOKE ALL ON hub.task_mode FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.task_mode TO {writer};
