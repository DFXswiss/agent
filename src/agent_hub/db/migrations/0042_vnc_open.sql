-- 0042_vnc_open: the device request kind `vnc.open` (ADR-103 section 3): the owner asks a device for a VNC connection
-- to the screen of a sandbox that waits for a human; the tunnel itself is relayed in memory and stores nothing.
--
-- Additive: the previous hub code starts on this schema and never writes the new kind.

ALTER TABLE hub.device_request DROP CONSTRAINT device_request_kind_check;
ALTER TABLE hub.device_request ADD CONSTRAINT device_request_kind_check
  CHECK (kind IN ('github.connect', 'claude.login', 'credential.check', 'mailbox.check', 'github.pull_status',
                  'vnc.open'));
