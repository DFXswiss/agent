-- 0013_admit: direct admission of existing accounts (KOR-37, ADR-060; 04-crypto.md sections 4.7, 4.8, 5.13).
--
-- `device.features` holds the capabilities a runner announced on its last live-channel connection (header
-- `Agent-Features`, replaced on every connection): the hub refuses an `admit` batch while an active device of the org
-- has not announced `log.admit` (`peer_outdated`), so an older runner never meets an entry type it would refuse.
-- `user_identity.id_sig`/`id_invite_seq` keep the identity proof of a registration through a platform invitation
-- (the signature of the invitation key over the user ID and its identity keys, and the platform-log seq of that
-- `user_invite`); NULL for every account registered before and for registrations through an org invitation.
-- `org_member.consented` is false for a user that entered by `admit` and has not joined by its own entry: the devices
-- of that org are not eligible for the user's credential key (`agent_hub.credentials.eligibility`, W2 for `cred`).
-- The notification kind `org.admitted` tells an admitted user who admitted it where. Every change is additive and the
-- previous hub code starts on this schema (the new kind is added NOT VALID, as in 0010).

ALTER TABLE hub.device ADD COLUMN features text[] NOT NULL DEFAULT '{{}}';

ALTER TABLE hub.org_member ADD COLUMN consented boolean NOT NULL DEFAULT true;

ALTER TABLE hub.user_identity ADD COLUMN id_sig bytea NULL CHECK (octet_length(id_sig) = 64);
ALTER TABLE hub.user_identity ADD COLUMN id_invite_seq integer NULL CHECK (id_invite_seq >= 0);
ALTER TABLE hub.user_identity ADD CONSTRAINT user_identity_proof_check
  CHECK ((id_sig IS NULL) = (id_invite_seq IS NULL));

ALTER TABLE hub.notification DROP CONSTRAINT notification_kind_check;
ALTER TABLE hub.notification ADD CONSTRAINT notification_kind_check CHECK (kind IN (
  'whiteboard.entry', 'task.idle', 'task.failed', 'invite.accepted', 'rotation.required', 'agent.account_change',
  'org.admitted'))
  NOT VALID;
