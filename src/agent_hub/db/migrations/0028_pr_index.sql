-- 0028_pr_index: the pull-request index the hub cannot read and the device request kind `github.pull_status`
-- (KOR-48, ADR-084 sections 1 and 2; 02-data-model.md sections 2.4 and 2.7, 04-crypto.md section 4.11a).
--
-- `pr_link.sealed` is the AEAD body that names the pull request (repository and number) under a key derived from the
-- workgroup key, bound to the row's workgroup, epoch, tag, task and origin; NULL for a link an older runner
-- registered. `linked_by` is the user who linked a pull request by its address; such a link has no session. Exactly
-- one of `session_id` (a link the device registered after `github.create_pull_request`) and `linked_by` is set.
-- `pr_link_list` serves the listing of a workgroup newest first (`GET /v1/workgroups/<wg>/pr-links`), the tag in byte
-- order so that the list cursor does not depend on the database collation.
-- `device_request.kind` gains `github.pull_status`, the fresh read of pull-request states by a device.

ALTER TABLE hub.pr_link ADD COLUMN sealed text NULL
  CHECK (length(sealed) = 379 AND sealed ~ '^[A-Za-z0-9_-]*[AEIMQUYcgkosw048]$');
ALTER TABLE hub.pr_link ADD COLUMN linked_by uuid NULL;
ALTER TABLE hub.pr_link ALTER COLUMN session_id DROP NOT NULL;
ALTER TABLE hub.pr_link ADD CONSTRAINT pr_link_origin CHECK ((session_id IS NULL) <> (linked_by IS NULL));
CREATE INDEX pr_link_list ON hub.pr_link (wg_id, created_at DESC, epoch DESC, tag COLLATE "C" DESC);

ALTER TABLE hub.device_request DROP CONSTRAINT device_request_kind_check;
ALTER TABLE hub.device_request ADD CONSTRAINT device_request_kind_check
  CHECK (kind IN ('github.connect', 'claude.login', 'credential.check', 'mailbox.check', 'github.pull_status'));
