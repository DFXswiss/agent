-- 0018_community: the community group chat of all users (KOR-26, ADR-069, docs/platform/spec/14-community.md section 2).
--
-- `community_epoch` holds the epochs of the community key `CMK_e` (only epoch 1 in this version) with the key check,
-- `community_wrap` each user's self-wrap of it, `community_request` the open key requests (a one-off X25519 key per
-- user, no user ID is ever shown to the holders that wrap for it), `community_reject` the wrappers a user caught with
-- a wrong key, `community_member` the users who joined (read position, mute), `community_moderator` the moderators
-- granted by the operator CLI and `community_message` the sealed messages in posting order. The hub never
-- holds the key, a name or a text: `header`, `ct` and `sig` are the sealed message, erased when it is deleted (the row
-- stays as a tombstone). `seq` is assigned by the hub (next after the newest, under an advisory lock), so it is gapless.
-- The user columns carry no foreign key to `hub.principal` (like `hub.audit`): the community is hub-wide, so every org
-- export carries these tables completely (`agent_hub.backup.tables`), also with users that the export of one org
-- does not contain.

CREATE TABLE hub.community_epoch (
  epoch      integer     PRIMARY KEY CHECK (epoch >= 1),
  key_check  bytea       NOT NULL CHECK (octet_length(key_check) = 32),
  created_by uuid        NOT NULL,
  created_at timestamptz NOT NULL
);

CREATE TABLE hub.community_wrap (
  epoch      integer     NOT NULL REFERENCES hub.community_epoch (epoch),
  user_id    uuid        NOT NULL,
  wrap       bytea       NOT NULL,
  sig        bytea       NOT NULL CHECK (octet_length(sig) = 64),
  created_at timestamptz NOT NULL,
  PRIMARY KEY (epoch, user_id)
);
CREATE INDEX community_wrap_user ON hub.community_wrap (user_id);

CREATE TABLE hub.community_request (
  user_id    uuid        PRIMARY KEY,
  id         uuid        NOT NULL UNIQUE,
  kem_pub    bytea       NOT NULL CHECK (octet_length(kem_pub) = 32),
  sig        bytea       NOT NULL CHECK (octet_length(sig) = 64),
  created_at timestamptz NOT NULL,
  offered_at timestamptz NULL,
  epoch      integer     NULL REFERENCES hub.community_epoch (epoch),
  wrap       bytea       NULL,
  wrapped_by uuid        NULL,
  wrap_sig   bytea       NULL CHECK (wrap_sig IS NULL OR octet_length(wrap_sig) = 64),
  wrapped_at timestamptz NULL,
  CHECK ((epoch IS NULL) = (wrap IS NULL) AND (wrap IS NULL) = (wrapped_by IS NULL)
         AND (wrap IS NULL) = (wrap_sig IS NULL) AND (wrap IS NULL) = (wrapped_at IS NULL))
);
CREATE INDEX community_request_open ON hub.community_request (created_at) WHERE wrap IS NULL;

CREATE TABLE hub.community_reject (
  wrapper_id  uuid        NOT NULL,
  user_id     uuid        NOT NULL,
  rejected_at timestamptz NOT NULL,
  PRIMARY KEY (wrapper_id, user_id)
);

CREATE TABLE hub.community_member (
  user_id       uuid        PRIMARY KEY,
  joined_at     timestamptz NOT NULL,
  last_read_seq bigint      NOT NULL DEFAULT 0 CHECK (last_read_seq >= 0),
  muted_at      timestamptz NULL,
  muted_by      uuid        NULL,
  CHECK ((muted_at IS NULL) = (muted_by IS NULL))
);
CREATE INDEX community_member_muted ON hub.community_member (muted_at) WHERE muted_at IS NOT NULL;

CREATE TABLE hub.community_moderator (
  user_id    uuid        PRIMARY KEY,
  granted_at timestamptz NOT NULL
);

CREATE TABLE hub.community_message (
  seq        bigint      PRIMARY KEY CHECK (seq >= 1),
  id         uuid        NOT NULL UNIQUE,
  author_id  uuid        NOT NULL,
  epoch      integer     NOT NULL REFERENCES hub.community_epoch (epoch),
  created_at timestamptz NOT NULL,
  ct_bytes   integer     NOT NULL CHECK (ct_bytes >= 0),
  header     jsonb       NULL CHECK (header IS NULL OR jsonb_typeof(header) = 'object'),
  ct         bytea       NULL,
  sig        bytea       NULL CHECK (sig IS NULL OR octet_length(sig) = 64),
  deleted_at timestamptz NULL,
  deleted_as text        NULL CHECK (deleted_as IN ('author', 'moderator')),
  deleted_by uuid        NULL,
  CHECK ((deleted_at IS NULL) = (deleted_as IS NULL) AND (deleted_at IS NULL) = (deleted_by IS NULL)),
  CHECK ((deleted_at IS NULL) = (header IS NOT NULL AND ct IS NOT NULL AND sig IS NOT NULL)),
  CHECK (deleted_at IS NULL OR (header IS NULL AND ct IS NULL AND sig IS NULL))
);
CREATE INDEX community_message_author ON hub.community_message (author_id, seq);

REVOKE ALL ON hub.community_epoch, hub.community_wrap, hub.community_request, hub.community_reject,
  hub.community_member, hub.community_moderator, hub.community_message FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.community_epoch, hub.community_wrap, hub.community_request,
  hub.community_reject, hub.community_member, hub.community_moderator, hub.community_message TO {writer};
