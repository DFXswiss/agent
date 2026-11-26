-- 0037_main_agents: the handles of main agents and subagents, the open main agents of a person and the agent as the
-- actor of an audit row (KOR-52, ADR-095 sections 1, 4 and 5; 02-data-model.md sections 2.4.4 and 2.9).
--
-- `task.number` numbers the tasks of an organization 1, 2, 3 ... (the handle `#<number>` of the task's main agent),
-- never reused: the BEFORE INSERT trigger `task_number` takes the next value of the organization's row in
-- `task_counter` in the creating transaction (the row lock serialises concurrent creations of one organization) when
-- the insert names no number - after the counter and after the highest number of the organization, so a number
-- inserted explicitly (a restore, a repair) never makes the counter collide; a restore inserts the exported numbers
-- and counters unchanged. Existing tasks are numbered per organization by (`created_at`, `id`) and the counter
-- continues after the highest number. The writer never deletes a counter (no DELETE right).
-- `session.agent_path` is a subagent's place below its main agent (handle `#<number>.<agent_path>`, the form of
-- `contracts/api/objects.json#/$defs/agent_path`; NULL exactly for a main session): the BEFORE INSERT trigger
-- `session_agent_path` gives a child of a main session (a parent without a path) the next ordinal among the task's
-- first-level paths (so a child
-- of a later main session never repeats an earlier handle) and a deeper child its parent's path, a dot and the next
-- ordinal among the parent's children. It relies on its callers holding the task row (`agent_hub.operations.children`
-- does, as every session insert of a task); UNIQUE (`task_id`, `agent_path`) refuses a race that would break this.
-- Existing children are numbered the same way by (`created_at`, `id`).
-- `task_open_owner` serves the person grouping of the situation view (the open tasks of an organization per owner,
-- newest activity first); `event_task_agent_name` the newest name event of a task (`name_event_id` of every task row,
-- without walking the task's whole event history); `audit_on_behalf_of` the audit filter `on_behalf_of`.
-- `audit.on_behalf_of` names the person a `main_agent`/`subagent` actor acted for (ADR-095 section 4); the actor kinds
-- are widened NOT VALID, as in 0013, 0021 and 0025 (every stored row keeps a kind of the former list).

CREATE TABLE hub.task_counter (
  org_id uuid   PRIMARY KEY,
  last   bigint NOT NULL CHECK (last >= 1)
);

ALTER TABLE hub.task ADD COLUMN number bigint NULL CHECK (number >= 1);
UPDATE hub.task t SET number = numbered.number
FROM (SELECT id, row_number() OVER (PARTITION BY org_id ORDER BY created_at, id) AS number FROM hub.task) numbered
WHERE numbered.id = t.id;
ALTER TABLE hub.task ALTER COLUMN number SET NOT NULL;
ALTER TABLE hub.task ADD CONSTRAINT task_org_number UNIQUE (org_id, number);
INSERT INTO hub.task_counter (org_id, last) SELECT org_id, max(number) FROM hub.task GROUP BY org_id;

CREATE FUNCTION hub.task_number() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.number IS NULL THEN
    INSERT INTO hub.task_counter AS counter (org_id, last)
      VALUES (NEW.org_id, coalesce((SELECT max(t.number) FROM hub.task t WHERE t.org_id = NEW.org_id), 0) + 1)
      ON CONFLICT (org_id) DO UPDATE
        SET last = greatest(counter.last, (SELECT max(t.number) FROM hub.task t WHERE t.org_id = NEW.org_id)) + 1
      RETURNING counter.last INTO NEW.number;
  END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION hub.task_number() FROM PUBLIC;
CREATE TRIGGER task_number BEFORE INSERT ON hub.task FOR EACH ROW EXECUTE FUNCTION hub.task_number();

ALTER TABLE hub.session ADD COLUMN agent_path text NULL;
WITH RECURSIVE ordinal AS (
  SELECT id, parent_session_id, depth,
    row_number() OVER (
      PARTITION BY depth = 1, CASE WHEN depth = 1 THEN task_id ELSE parent_session_id END ORDER BY created_at, id
    ) AS n
  FROM hub.session WHERE depth > 0
), placed AS (
  SELECT id, n::text AS agent_path FROM ordinal WHERE depth = 1
  UNION ALL
  SELECT ordinal.id, placed.agent_path || '.' || ordinal.n
  FROM ordinal JOIN placed ON placed.id = ordinal.parent_session_id
)
UPDATE hub.session s SET agent_path = placed.agent_path FROM placed WHERE placed.id = s.id;
ALTER TABLE hub.session ADD CONSTRAINT session_agent_path_check CHECK (
  (depth = 0) = (agent_path IS NULL) AND agent_path ~ '^[1-9][0-9]{{0,8}}(\.[1-9][0-9]{{0,8}})*$'
);
ALTER TABLE hub.session ADD CONSTRAINT session_task_agent_path UNIQUE (task_id, agent_path);

CREATE FUNCTION hub.session_agent_path() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  prefix text;
  ordinal bigint;
BEGIN
  IF NEW.agent_path IS NOT NULL OR NEW.depth = 0 THEN
    RETURN NEW;
  END IF;
  SELECT agent_path INTO prefix FROM hub.session WHERE id = NEW.parent_session_id;
  IF prefix IS NULL THEN
    SELECT coalesce(max(agent_path::bigint), 0) + 1 INTO ordinal
    FROM hub.session WHERE task_id = NEW.task_id AND agent_path IS NOT NULL AND position('.' IN agent_path) = 0;
    NEW.agent_path := ordinal::text;
  ELSE
    SELECT coalesce(max(substring(agent_path FROM '[0-9]+$')::bigint), 0) + 1 INTO ordinal
    FROM hub.session WHERE parent_session_id = NEW.parent_session_id;
    NEW.agent_path := prefix || '.' || ordinal::text;
  END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION hub.session_agent_path() FROM PUBLIC;
CREATE TRIGGER session_agent_path BEFORE INSERT ON hub.session
  FOR EACH ROW EXECUTE FUNCTION hub.session_agent_path();

CREATE INDEX task_open_owner ON hub.task (org_id, owner_id, last_activity_at DESC, id DESC) WHERE state <> 'closed';
CREATE INDEX event_task_agent_name ON hub.event (task_id, scope_seq DESC) WHERE kind = 'task.agent_name';

ALTER TABLE hub.audit ADD COLUMN on_behalf_of uuid NULL;
ALTER TABLE hub.audit DROP CONSTRAINT audit_actor_kind_check;
ALTER TABLE hub.audit ADD CONSTRAINT audit_actor_kind_check CHECK (actor_kind IN (
  'user', 'device', 'session', 'hub', 'operator', 'anonymous', 'main_agent', 'subagent'))
  NOT VALID;
CREATE INDEX audit_on_behalf_of ON hub.audit (on_behalf_of, seq DESC) WHERE on_behalf_of IS NOT NULL;

REVOKE ALL ON hub.task_counter FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON hub.task_counter TO {writer};
