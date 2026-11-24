-- 0015_event_audit_cover: how far the audit job has summarised the session's own events of each scope (KOR-40,
-- ADR-063 section 4).
--
-- Events of the kinds session.output, session.terminal, session.tool, session.hook and session.file no longer get one
-- `event.append` audit row each. The audit job writes one `event.append_batch` row per session and scope for the new
-- ones and keeps here, per event scope, the scope sequence number up to which it has looked. Per scope the events
-- commit in `scope_seq` order (the scope head is locked by the storing transaction), so everything at or below a
-- committed head is visible to the job. Existing scopes start at their current head: their events already have their
-- own rows.

CREATE TABLE hub.event_audit_cover (
  scope_id    uuid   PRIMARY KEY,
  covered_seq bigint NOT NULL CHECK (covered_seq >= 0)
);
INSERT INTO hub.event_audit_cover (scope_id, covered_seq) SELECT scope_id, seq FROM hub.scope_head;

REVOKE ALL ON hub.event_audit_cover FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE ON hub.event_audit_cover TO {writer};
