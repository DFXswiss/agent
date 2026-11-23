-- 0011_wg_access_inherited: organization admins read every workgroup (KOR-34, ADR-058; 04-crypto.md section 4.7,
-- 02-data-model.md section 2.2).
--
-- `hub.wg_access` holds the effective access of every active user. Since ADR-058 an active admin reads every
-- workgroup of its org without explicit access; such a row is marked `inherited`. Credential eligibility and the
-- subscription polling ignore inherited rows (the credential scope uses explicit access only).
--
-- Back-fill: every active admin gets an active inherited `read` row on every workgroup of its org where it has no
-- active row, with `granted_seq` = the entry that made the access effective in this form: the latest of the admin's
-- current role (its last `role` entry since it joined, else its join), the creation of the workgroup and the end of an
-- earlier explicit access (`revoked_seq`). The active rows then equal what `agent-hub rebuild-projections` writes;
-- the rebuild additionally recreates historical, already revoked inherited rows (demoted or removed admins), which
-- no right reads.

ALTER TABLE hub.wg_access ADD COLUMN inherited boolean NOT NULL DEFAULT false;
ALTER TABLE hub.wg_access ADD CONSTRAINT wg_access_inherited_read CHECK (NOT inherited OR access = 'read');

WITH admin_since AS (
  SELECT m.org_id, m.principal_id AS user_id,
         COALESCE((SELECT max(e.seq) FROM hub.membership_entry e
                   WHERE e.org_id = m.org_id AND e.type = 'role' AND e.subject_id = m.principal_id
                     AND e.seq > m.joined_seq), m.joined_seq) AS since
  FROM hub.org_member m
  WHERE m.kind = 'user' AND m.role = 'admin' AND m.removed_seq IS NULL
)
INSERT INTO hub.wg_access AS a (wg_id, user_id, access, inherited, granted_seq, revoked_seq)
SELECT w.id, s.user_id, 'read', true, GREATEST(s.since, w.created_seq), NULL
FROM admin_since s JOIN hub.wg w ON w.org_id = s.org_id
ON CONFLICT (wg_id, user_id) DO UPDATE
  SET access = 'read', inherited = true, granted_seq = GREATEST(EXCLUDED.granted_seq, a.revoked_seq), revoked_seq = NULL
  WHERE a.revoked_seq IS NOT NULL;
