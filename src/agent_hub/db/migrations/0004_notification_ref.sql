-- 0004_notification_ref: lookup indexes of the whiteboard fan-out (HUB-15).
--
-- After the commit of a notified whiteboard entry the hub reads the entry's `notification` rows and its undelivered
-- `session_ping` rows by kind and reference (03-api.md section 4.9); without these indexes both reads would scan the
-- growing tables.

CREATE INDEX notification_kind_ref ON hub.notification (kind, ref);
CREATE INDEX session_ping_ref_undelivered ON hub.session_ping (ref) WHERE delivered_at IS NULL;
