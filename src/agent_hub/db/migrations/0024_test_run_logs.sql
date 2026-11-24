-- Logs of test runs and work VM jobs as encrypted objects (KOR-43, ADR-080 section 3).
--
-- `context_object.kind` tells a context checkpoint (`context`, ADR-072) from a log (`log`): the complete output of a
-- `test.run` operation or a long work VM job, sealed by the device exactly like a checkpoint chunk (uncompressed
-- pieces of at most 256 KiB). Listing, pruning beyond `AGENT_HUB_CONTEXT_KEEP`, the device state's `context` and the
-- device downloads see only `context`; logs are read by the members through
-- `GET /v1/tasks/{{task}}/logs/{{object}}` (the braces are doubled for `agent_hub.db.migrate.compose`) and pruned
-- beyond `AGENT_HUB_LOG_KEEP` per task. Existing rows are checkpoints.
ALTER TABLE hub.context_object
  ADD COLUMN kind text NOT NULL DEFAULT 'context' CHECK (kind IN ('context', 'log'));

CREATE INDEX context_object_task_log ON hub.context_object (task_id, created_at DESC, id DESC)
  WHERE state = 'complete' AND kind = 'log';
