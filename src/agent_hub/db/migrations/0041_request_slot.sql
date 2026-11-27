-- 0041_request_slot: logins of different subscriptions side by side (KOR-73, ADR-100 section 2).
--
-- `device_request.slot` is the plaintext subject of a root request (`POST /v1/device-requests` `slot`; for
-- `claude.login` the subscription record it fills). An active root is replaced only by a new root of the same user,
-- device, kind and slot; NULL is a slot of its own (the requests from before keep replacing each other).
--
-- Every change is additive: the new column is nullable. The previous hub code starts on this schema and ignores it.

ALTER TABLE hub.device_request ADD COLUMN slot uuid NULL;
