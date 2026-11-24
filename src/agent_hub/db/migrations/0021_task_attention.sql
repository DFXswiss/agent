-- 0021_task_attention: the attention record of every task (KOR-39, ADR-076; 02-data-model.md section 2.4).
--
-- `task_attention` is a projection: one row per task with the category and reason the hub derives from the task,
-- session and operation metadata it already holds (`agent_hub.attention.derive`), the object the next action refers
-- to (`ref_kind` + `ref_id`), when the task entered its category (`since`), the occasion counter `seq` and the
-- subagent counters. Only `agent_hub.attention.store.refresh` writes it; the job `attention.backfill` derives the rows
-- of tasks that have none (every task created before this migration). The list index serves the paged lists of
-- the attention endpoint (03-api.md section 4.6) (both orders read it, forwards or backwards), the workgroup index the counters over
-- the readable workgroups and the owner index the counter "waiting for me". The partial event index finds the newest
-- prompt of a task (the last human input) without reading the task's other events. `task.attention` is a new
-- notification kind; the check is replaced NOT VALID like in 0010 and 0013 (every existing row has an old kind).

CREATE TABLE hub.task_attention (
  task_id          uuid        PRIMARY KEY,
  org_id           uuid        NOT NULL,
  wg_id            uuid        NOT NULL,
  owner_id         uuid        NOT NULL,
  category         text        NOT NULL CHECK (category IN ('needs_decision', 'blocked', 'working', 'done')),
  reason           text        NOT NULL CHECK (reason ~ '^[a-z][a-z0-9_]{{0,63}}$'),
  ref_kind         text        NULL CHECK (ref_kind IN ('operation', 'session')),
  ref_id           uuid        NULL,
  since            timestamptz NOT NULL,
  seq              integer     NOT NULL CHECK (seq >= 1),
  subagents_total  integer     NOT NULL CHECK (subagents_total >= 0),
  subagents_active integer     NOT NULL CHECK (subagents_active >= 0),
  updated_at       timestamptz NOT NULL,
  CHECK ((ref_kind IS NULL) = (ref_id IS NULL))
);
CREATE INDEX task_attention_list ON hub.task_attention (org_id, category, since DESC, task_id DESC);
CREATE INDEX task_attention_wg ON hub.task_attention (wg_id, category, since);
CREATE INDEX task_attention_owner ON hub.task_attention (owner_id, category, wg_id);

CREATE INDEX event_task_prompt ON hub.event (task_id, received_at DESC) WHERE kind = 'prompt';

ALTER TABLE hub.notification DROP CONSTRAINT notification_kind_check;
ALTER TABLE hub.notification ADD CONSTRAINT notification_kind_check CHECK (kind IN (
  'whiteboard.entry', 'task.idle', 'task.failed', 'task.attention', 'invite.accepted', 'rotation.required',
  'agent.account_change', 'org.admitted'))
  NOT VALID;

REVOKE ALL ON hub.task_attention FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.task_attention TO {writer};
