-- 0042_attachments: files and images attached to prompts as encrypted objects (ADR-102; 02-data-model.md section
-- 2.4.5).
--
-- `attachment_object` is one attached file of a workgroup: uploaded chunk by chunk by a member (`uploading`,
-- `chunks`/`size` count what is stored so far, `updated_at` = the last chunk) and sealed by the commit (`complete`,
-- with the digest the uploader named and the hub checked). The hub holds no key: chunks are AES-GCM ciphertext under a
-- key derived from the workgroup key (04-crypto.md section 4.14); file name and media type exist only inside the
-- prompt that names the object. An object stays with its workgroup (no foreign key: the workgroup rows are a projection
-- of the membership log and are rebuilt, `agent_hub.backup.rebuild`); an upload without a chunk for 6 h is removed by
-- the job `attachments.expire`.

CREATE TABLE hub.attachment_object (
  id           uuid        PRIMARY KEY,
  org_id       uuid        NOT NULL,
  wg_id        uuid        NOT NULL,
  author_id    uuid        NOT NULL,
  state        text        NOT NULL CHECK (state IN ('uploading', 'complete')),
  chunks       integer     NOT NULL DEFAULT 0 CHECK (chunks >= 0),
  size         bigint      NOT NULL DEFAULT 0 CHECK (size >= 0),
  digest       text        NULL,
  created_at   timestamptz NOT NULL,
  updated_at   timestamptz NOT NULL,
  completed_at timestamptz NULL,
  CHECK ((state = 'complete') = (completed_at IS NOT NULL)),
  CHECK ((state = 'complete') = (digest IS NOT NULL))
);
CREATE INDEX attachment_object_org ON hub.attachment_object (org_id);
CREATE INDEX attachment_object_stale ON hub.attachment_object (updated_at) WHERE state = 'uploading';

CREATE TABLE hub.attachment_chunk (
  object_id   uuid    NOT NULL REFERENCES hub.attachment_object (id) ON DELETE CASCADE,
  chunk_index integer NOT NULL CHECK (chunk_index >= 0),
  ct          bytea   NOT NULL,
  sha256      bytea   NOT NULL,
  PRIMARY KEY (object_id, chunk_index)
);

REVOKE ALL ON hub.attachment_object, hub.attachment_chunk FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON hub.attachment_object, hub.attachment_chunk TO {writer};
