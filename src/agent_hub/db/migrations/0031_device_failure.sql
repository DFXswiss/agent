-- 0031_device_failure: interrupted sessions of a failed device and their successor on another device (KOR-58,
-- ADR-087; 02-data-model.md sections 2.1 and 2.4).
--
-- `device.offline_since` is the hub time at which the device went offline (its presence row was deleted by a
-- disconnect or by the expiry job); a connect clears it. A device without presence row now counts as offline since
-- this migration (hub time, never a device clock). A device has failed while it is offline and `offline_since` lies
-- `AGENT_HUB_DEVICE_FAILOVER_SECONDS` or more back.
-- `session.interrupted_at` marks an active session of a failed device (set by the job `failover.sweep`), and
-- `session.interruption_ended_at` the time its device was back before a move; the session is interrupted while the
-- first is set and not older than the second (the mark stays, so a restarted device's report still counts);
-- `session.failover_of` names, for the main session the hub created on another device, the lost session it
-- continues (no foreign key, like `parent_session_id`: both rows go with their task). Neither column carries content.

ALTER TABLE hub.device ADD COLUMN offline_since timestamptz NULL;
UPDATE hub.device d SET offline_since = now()
  WHERE NOT EXISTS (SELECT 1 FROM hub.device_presence p WHERE p.device_id = d.id);

ALTER TABLE hub.session ADD COLUMN interrupted_at timestamptz NULL;
ALTER TABLE hub.session ADD COLUMN interruption_ended_at timestamptz NULL;
ALTER TABLE hub.session ADD COLUMN failover_of uuid NULL;

-- `session.redeliver_event_id` is, for a lost session whose turn was running, the prompt the hub hands to the
-- successor again (chosen once at the move: the last one it accepted); `session.restore_object` is, for a successor,
-- the checkpoint the hub pinned at the move, so a late upload of the lost device never changes what it restores.
ALTER TABLE hub.session ADD COLUMN redeliver_event_id uuid NULL;
ALTER TABLE hub.session ADD COLUMN restore_object uuid NULL;
