-- 0040_captured_tasks: tasks captured like tickets (E-41, ADR-100 sections 1 and 2; 02-data-model.md section 2.4).
--
-- `task.status` is the ticket status `todo` | `inProgress` | `completed`, `task.urgency` `low` | `normal` | `high` |
-- `urgent`, `task.assignee_id` the responsible user of the organization (a person or an agent account) and
-- `task.topic_id` the topic (the `ref` of a sealed org-scope `topic.profile` event); IDs and codes only, every text
-- stays sealed. Existing tasks are `completed` when closed and `inProgress` otherwise (A22). The trigger
-- `task_status` keeps closed ⇔ completed on every write - a path that closes or reopens a task without naming the
-- status (also the previous hub code) keeps it right - and the check states it. The defaults let the previous hub
-- code insert tasks on this schema. The partial indexes bound the filtered task lists of a workgroup.

ALTER TABLE hub.task ADD COLUMN status text NOT NULL DEFAULT 'inProgress'
  CHECK (status IN ('todo', 'inProgress', 'completed'));
ALTER TABLE hub.task ADD COLUMN urgency text NOT NULL DEFAULT 'normal'
  CHECK (urgency IN ('low', 'normal', 'high', 'urgent'));
ALTER TABLE hub.task ADD COLUMN assignee_id uuid NULL;
ALTER TABLE hub.task ADD COLUMN topic_id uuid NULL;
UPDATE hub.task SET status = 'completed' WHERE state = 'closed';

CREATE FUNCTION hub.task_status() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF NEW.state = 'closed' THEN
    NEW.status := 'completed';
  ELSIF NEW.status = 'completed' THEN
    NEW.status := 'inProgress';
  END IF;
  RETURN NEW;
END
$$;
REVOKE ALL ON FUNCTION hub.task_status() FROM PUBLIC;
CREATE TRIGGER task_status BEFORE INSERT OR UPDATE ON hub.task FOR EACH ROW EXECUTE FUNCTION hub.task_status();
ALTER TABLE hub.task ADD CONSTRAINT task_status_closed CHECK ((state = 'closed') = (status = 'completed'));

CREATE INDEX task_wg_status ON hub.task (wg_id, status, last_activity_at DESC, id DESC);
CREATE INDEX task_wg_assignee ON hub.task (wg_id, assignee_id, last_activity_at DESC, id DESC)
  WHERE assignee_id IS NOT NULL;
CREATE INDEX task_wg_topic ON hub.task (wg_id, topic_id, last_activity_at DESC, id DESC) WHERE topic_id IS NOT NULL;
