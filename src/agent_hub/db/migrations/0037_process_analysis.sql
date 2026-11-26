-- 0037_process_analysis: the analysis task of "Prozesse optimieren" (KOR-53, ADR-095).
--
-- `task.analysis` marks an analysis task and keeps its scope and period `{{scope: 'org'|'wg', since, until}}` (plaintext
-- metadata of the request; NULL for every other task). The indexes bound the session reads of usage, operations and
-- audit of one workgroup (`GET /v1/device/workgroups/{{wg}}/usage|operations|audit`), newest first.
--
-- Every change is additive: the new column is nullable. The previous hub code starts on this schema and ignores it.

ALTER TABLE hub.task ADD COLUMN analysis jsonb NULL CHECK (analysis IS NULL OR jsonb_typeof(analysis) = 'object');

CREATE INDEX session_wg ON hub.session (wg_id, created_at DESC, id DESC);
CREATE INDEX operation_wg ON hub.operation (wg_id, created_at DESC, id DESC);
CREATE INDEX audit_wg ON hub.audit (wg_id, seq DESC) WHERE wg_id IS NOT NULL;
