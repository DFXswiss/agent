-- Names of main agents (KOR-52, ADR-095 sections 2 and 3).
--
-- `profile_event` also keeps the newest verified `task.agent_name` event of each task (workgroup scope, object = the
-- task), the name `get_context` gives the session. Newest is decided by the hub's scope sequence number of the event
-- (`scope_seq`, the position the inbox verified it at), as every reader of the name does; the other kinds keep
-- comparing their signed header (`ts`, then the event ID), so `scope_seq` is null for rows stored before this
-- migration and set for every row stored after it.
ALTER TABLE runner.profile_event DROP CONSTRAINT profile_event_kind_check;
ALTER TABLE runner.profile_event ADD CONSTRAINT profile_event_kind_check
    CHECK (kind IN ('org.profile', 'wg.profile', 'member.profile', 'task.meta', 'task.agent_name'));
ALTER TABLE runner.profile_event ADD COLUMN scope_seq bigint;
ALTER TABLE runner.profile_event ADD CONSTRAINT profile_event_agent_name_seq
    CHECK (kind <> 'task.agent_name' OR scope_seq IS NOT NULL);
