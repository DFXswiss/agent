-- 0035_golden_switch: workgroup VMs move to the current golden by themselves (KOR-70, ADR-093).
--
-- `vm.upgrade` is the switch status of an outdated workgroup VM as its device reports it with `vm_state` (contracts
-- `vm_upgrade`: golden, state, holds, sessions that move; NULL once current), `vm.replaced_by` the VM that replaced a
-- switched one (on its `deleted` report). `vm.switch_requested_at/by` record a member with administration rights who
-- asked for the switch now (`POST /v1/workgroups/{wg}/vms/{vm}/switch`); the desired state names such VMs to their
-- device until the VM is current or deleted. `session.context_reason` gains `golden_switch` for a restored session
-- whose VM was switched while it was paused (the whole workspace came along).
--
-- Every change is additive: new columns are nullable, the relaxed checks admit every existing row. The previous hub code
-- starts on this schema and ignores the columns.

ALTER TABLE hub.vm ADD COLUMN upgrade jsonb NULL;
ALTER TABLE hub.vm ADD COLUMN replaced_by text NULL CHECK (replaced_by ~ '^vm-[0-9a-f]{{8}}$');
ALTER TABLE hub.vm ADD COLUMN switch_requested_at timestamptz NULL;
ALTER TABLE hub.vm ADD COLUMN switch_requested_by uuid NULL;
CREATE INDEX vm_switch_requested ON hub.vm (device_id) WHERE switch_requested_at IS NOT NULL;

ALTER TABLE hub.session DROP CONSTRAINT session_context_reason_check;
ALTER TABLE hub.session ADD CONSTRAINT session_context_reason_check CHECK (context_reason IN (
  'context_missing', 'context_invalid', 'context_unavailable', 'version_incompatible', 'restore_failed',
  'workspace_partial', 'workspace_omitted', 'golden_switch'));
ALTER TABLE hub.session DROP CONSTRAINT session_context_reason;
ALTER TABLE hub.session ADD CONSTRAINT session_context_reason CHECK (CASE context_restore
  WHEN 'fallback' THEN context_reason IS NOT NULL AND context_reason IN (
    'context_missing', 'context_invalid', 'context_unavailable', 'version_incompatible', 'restore_failed')
  WHEN 'restored' THEN context_reason IS NULL OR context_reason IN ('workspace_partial', 'workspace_omitted', 'golden_switch')
  ELSE context_reason IS NULL END);
