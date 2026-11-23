-- 0012_workvm_metrics: work VMs in the monitoring (KOR-29, ADR-059; agent docs/platform/spec/13-work-vm.md section 10,
-- 02-data-model.md section 2.8).
--
-- A runner samples its work VMs like its workgroup VMs, as entity kind `workvm` with the work VM's own ID
-- (`wvm-<8 hex>`). The hub keeps no row per work VM: it only learns the ID and the session the VM belongs to from the
-- samples (`session_id`, set for `workvm` samples only), which places the work VM under its device and session in the
-- overview and decides who may read it (the readers of the session's workgroup). The index serves the overview's
-- lookup of the work VMs of the visible sessions.

ALTER TABLE hub.metric_sample DROP CONSTRAINT metric_sample_entity_kind_check;
ALTER TABLE hub.metric_sample ADD CONSTRAINT metric_sample_entity_kind_check
  CHECK (entity_kind IN ('device', 'vm', 'session', 'workvm'));
ALTER TABLE hub.metric_sample ADD COLUMN session_id uuid NULL;
ALTER TABLE hub.metric_sample ADD CONSTRAINT metric_sample_workvm_session
  CHECK ((entity_kind = 'workvm') = (session_id IS NOT NULL));
CREATE INDEX metric_sample_workvm ON hub.metric_sample (session_id, ts) WHERE entity_kind = 'workvm';

ALTER TABLE hub.metric_rollup DROP CONSTRAINT metric_rollup_entity_kind_check;
ALTER TABLE hub.metric_rollup ADD CONSTRAINT metric_rollup_entity_kind_check
  CHECK (entity_kind IN ('device', 'vm', 'session', 'workvm'));
