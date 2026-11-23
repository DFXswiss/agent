-- 0009_session_capacity_refusal: the sessions a device refused for lack of capacity (KOR-14, ADR-049; 05-runner.md
-- section 3.4, 03-api.md section 4.6.2).
--
-- The scheduler does not count a device as free while it refused a session start with `capacity_exhausted` after
-- its last heartbeat, and it looks up whether the newest session of a workgroup on a device was refused
-- (`agent_hub.scheduler.load`); the indexes keep both checks small on every placement.

CREATE INDEX session_capacity_refused ON hub.session (device_id, ended_at)
  WHERE state = 'failed' AND state_reason = 'capacity_exhausted';

CREATE INDEX session_device_wg ON hub.session (device_id, wg_id, created_at, id) WHERE device_id IS NOT NULL;
