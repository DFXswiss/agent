-- 0020_resource_choice: several VMs and devices per workgroup, own choice, waiting instead of refusing (KOR-41, ADR-075).
--
-- `org_device` projects the assignments of devices to the whole organization (`device_assign_org` until
-- `device_unassign_org` or `remove`); `wg_device` keeps holding the effective assignments, explicit and through the
-- organization, so the scheduler and the device lists read one table. `wg_placement` is a workgroup's default device
-- and VM for new tasks; `task.placement_*` the device and VM chosen for a task. `session.not_before` holds back the
-- placement of an attempt after repeated refusals for lack of capacity. Every change is additive: the previous hub code
-- starts on this schema and ignores the new columns and tables.

CREATE TABLE hub.org_device (
  org_id         uuid    NOT NULL,
  device_id      uuid    NOT NULL,
  assigned_seq   integer NOT NULL CHECK (assigned_seq >= 0),
  unassigned_seq integer NULL,
  PRIMARY KEY (org_id, device_id)
);
CREATE INDEX org_device_device ON hub.org_device (device_id);

CREATE TABLE hub.wg_placement (
  wg_id     uuid        PRIMARY KEY,
  device_id uuid        NOT NULL,
  vm_id     text        NULL CHECK (vm_id ~ '^vm-[0-9a-f]{{8}}$'),
  set_by    uuid        NOT NULL,
  set_at    timestamptz NOT NULL
);

ALTER TABLE hub.task ADD COLUMN placement_device_id uuid NULL;
ALTER TABLE hub.task ADD COLUMN placement_vm_id text NULL CHECK (placement_vm_id ~ '^vm-[0-9a-f]{{8}}$');
ALTER TABLE hub.task ADD CONSTRAINT task_placement_vm_needs_device
  CHECK (placement_vm_id IS NULL OR placement_device_id IS NOT NULL);
ALTER TABLE hub.session ADD COLUMN not_before timestamptz NULL;

GRANT SELECT, INSERT, UPDATE, DELETE ON hub.org_device TO {writer};
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.wg_placement TO {writer};
