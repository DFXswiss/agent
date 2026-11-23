-- Profile events keyed by their scope as well (KOR-03, decisions/KOR-03.md; AUD-01 A-17, RUN-08 decision 21). With
-- the key (kind, org_id, object_id) a newer `task.meta` of workgroup A naming a task ID of workgroup B replaced B's
-- stored title, so B's sessions saw none. The signed header's `scope_id` (the org for org-scoped kinds, the
-- workgroup for `task.meta`; 04-crypto.md section 5.4) is now part of the key: each scope keeps its own latest event.
ALTER TABLE runner.profile_event ADD COLUMN scope_id uuid;
UPDATE runner.profile_event SET scope_id = (sealed -> 'header' ->> 'scope_id')::uuid;
ALTER TABLE runner.profile_event ALTER COLUMN scope_id SET NOT NULL;
ALTER TABLE runner.profile_event DROP CONSTRAINT profile_event_pkey;
ALTER TABLE runner.profile_event ADD PRIMARY KEY (kind, org_id, scope_id, object_id);
