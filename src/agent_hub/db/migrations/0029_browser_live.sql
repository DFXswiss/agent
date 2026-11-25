-- 0029_browser_live: watch leases and the latest live frame of the platform browser (KOR-44, ADR-085 section 6;
-- 13-work-vm.md section 13.6).
--
-- `browser_watch` holds one lease per watched session and hub process (`instance`, the live service's ID): written when
-- the first stream of the process starts watching the session's browser, renewed every 10 s (`until` = now + 30 s,
-- `kbps` the largest wish of its streams) and deleted when its last stream stops; a process that dies leaves a row that
-- ends with its `until` (the renewals of the other processes remove it). `browser_live` holds the latest live frame of
-- a session (ciphertext under the workgroup key, 04-crypto.md section 4.13; the hub never decrypts): replaced by every
-- frame the session's device posts, read by every hub process to send it to its watching streams and as the first
-- picture of a new viewer, deleted when no valid lease remains. Both are UNLOGGED: they are live state, lost on a
-- crash like the open streams themselves, and keep no frame in the write-ahead log or the backups.

CREATE UNLOGGED TABLE hub.browser_watch (
  session_id uuid        NOT NULL,
  instance   text        NOT NULL,
  until      timestamptz NOT NULL,
  kbps       integer     NOT NULL CHECK (kbps BETWEEN 100 AND 10000),
  PRIMARY KEY (session_id, instance)
);

CREATE UNLOGGED TABLE hub.browser_live (
  session_id  uuid        PRIMARY KEY,
  browser_id  text        NOT NULL CHECK (browser_id ~ '^br-[0-9a-f]{{12}}$'),
  seq         bigint      NOT NULL CHECK (seq >= 0),
  frame       jsonb       NOT NULL,
  received_at timestamptz NOT NULL
);

GRANT SELECT, INSERT, UPDATE, DELETE ON hub.browser_watch, hub.browser_live TO {writer};
