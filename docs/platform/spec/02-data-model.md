# 02 Data model: hub, runner store, guest journal, event log, migrations

> As built: where the implementation refines or deviates from this document, see the register
> [`spec-deviations.md`](../spec-deviations.md#02-data-model) (DOC-01).

Status: binding. Column types are PostgreSQL unless stated. All timestamps `timestamptz` (UTC). All IDs
`uuid` (UUIDv7) unless stated. `bytea` holds raw bytes (APIs carry them as b64u). Names in this document are
the names in code; renaming requires an ADR.

## 1. Principles

1. The hub stores **ciphertext + the metadata of `04-crypto.md` §7** – nothing else. A column that would hold
   confidential plaintext is a defect.
2. **Append-only** tables carry the trigger `hub.refuse_change()` (pattern of `agent_core/record/migrate.py`):
   `BEFORE UPDATE OR DELETE … FOR EACH ROW` and `BEFORE TRUNCATE … FOR EACH STATEMENT`, `ENABLE ALWAYS`.
   Marked **[AO]** below.
3. **Projections** (derived from the membership log, platform log and events) are updated in the same
   transaction as the appended source row and can be rebuilt with `agent-hub rebuild-projections`
   (test: rebuild equals incremental state). Marked **[P]**.
4. Roles: owner (migrations only, `AGENT_HUB_OWNER_DATABASE`), writer (the hub, `AGENT_HUB_DATABASE`).
   PUBLIC is revoked. Writer gets `SELECT, INSERT` on [AO] tables and `SELECT, INSERT, UPDATE, DELETE` on the
   others.
5. Lists exposed to the UI are **newest first** with a deterministic tie-breaker (`created_at DESC, id DESC`),
   per the repository rule; documented exceptions: chat/event streams and logs (ascending by sequence).

## 2. Hub schema `hub`

### 2.1 Principals, authentication

| Table | Columns | Notes |
|---|---|---|
| `principal` | `id` PK, `kind` text CHECK in (`user`,`device`), `created_at`, `disabled_at` NULL | |
| `user_identity` | `user_id` PK→principal, `kem_pub` bytea(32), `sig_pub` bytea(32), `recovery_id` text UNIQUE, `created_at` | identity public keys |
| `user_settings` | `user_id` PK, `model_access` text CHECK in (`auto`,`subscription`,`api_key`,`platform`) default `auto`, `model_id` text NULL, `locale` text CHECK in (`de`,`en`) default `en`, `updated_at` | plaintext preferences |
| `identity_bundle` | (`user_id`,`slot`) PK, `ct` bytea, `updated_at` | slots `passkey:<b64u id>`, `recovery` |
| `passkey` | `credential_id` bytea PK, `user_id`, `public_key` bytea(65) (uncompressed P-256 point; only ES256 is offered and accepted), `sign_count` bigint, `transports` text[], `prf_seen` bool, `created_at`, `last_used_at`, `removed_at` NULL | verification via `agent_core.passkey` |
| `ceremony` | `id` PK, `kind` CHECK in (`register`,`login`,`add_passkey`,`recovery`,`recovery_rotate`), `challenge` bytea(32), `user_id` NULL, `invite_id` NULL, `hub_value` bytea NULL, `created_at`, `expires_at` (= +300 s), `used_at` NULL, `pairing_id` NULL (a login that confirms an agent pairing, KOR-24) | purged after 1 day; `recovery_rotate`: passkey proof before a recovery code is replaced (`03-api.md` §4.1, KOR-01) |
| `browser_session` | `key_id` text PK (`session.<32 hex>`), `user_id`, `credential_id`, `public_key` bytea (P-256 point), `statement` text, `created_at`, `expires_at`, `ended_at` NULL, `pairing_id` NULL (KOR-24) | |
| `agent` | delegated agent access of a user (`11-agent-access.md` §2, migration 0010): `id` PK, `user_id` NULL, `kem_pub`, `sig_pub`, `state`, `bundle_ct`, `label_ct`, `claim_hash`, `claim_misses`, `source`, `created_at`, `expires_at`, `confirmed_at`, `claimed_at`, `last_used_at`, `revoked_at` | |
| `request_nonce` | (`key_id`,`nonce`) PK, `seen_at` | rows older than 600 s deleted on insert (legacy rule) |
| `device` | `id` PK→principal, `kind` CHECK in (`platform`,`org`), `kem_pub`, `sig_pub`, `owner_org` NULL, `state` CHECK in (`pending_certification`,`active`,`decertified`,`removed`), `enrolled_at`, `certified_seq` NULL, `agent_version`, `os`, `arch`, `capacity` jsonb (`{"vm_memory_budget_mib","vm_memory_used_mib","vm_slots_total","vm_slots_free","pool_ready"}`), `updated_at` | |
| `device_presence` | `device_id` PK, `hub_instance` text, `connected_at`, `last_heartbeat_at` | online ⇔ heartbeat < 30 s ago |
| `device_enrollment` | `invite_id` PK, `device_id`, `request` jsonb (enrollment body incl. signatures), `created_at`, `certified_at` NULL | platform devices only |
| `invite` | `id` text PK (= `invite_id`), `scope` CHECK in (`platform`,`org`), `kind` CHECK in (`user`,`device`), `org_id` NULL, `log_seq` int NULL, `created_by`, `created_at`, `expires_at`, `registered_user` NULL (user registered through this invitation; at most one), `registered_at` NULL, `used_at` NULL, `used_by` NULL, `revoked_at` NULL, `package_ct` bytea NULL, `pk_inv` NULL (public invitation key, ADR-043; required to register) | package deleted (set NULL) on use, revocation or expiry; platform user invitations from the operator CLI also appear as `platform_entry` type `user_invite`; registrations without accept/genesis within 24 h are deleted and `registered_user` reset |

### 2.2 Organizations, logs, keys

| Table | Columns | Notes |
|---|---|---|
| `org` | `id` PK, `private` bool, `platform_access` bool [P] (true for private orgs created from a platform invitation; else from `org_platform_access` platform-log entries, ADR-026), `default_wg` uuid, `created_by`, `created_at`, `log_head_seq` int, `log_head_hash` text, `rotation_pending` jsonb (list of `{scope,scope_id}`) | |
| `membership_entry` [AO] | (`org_id`,`seq`) PK, `entry_text` text (canonical JSON of the entry), `entry_hash` text UNIQUE, `type`, `by_id`, `subject_id` NULL, `received_at` | appended under `SELECT … FOR UPDATE` of `org` row |
| `platform_entry` [AO] | `seq` PK, `entry_text`, `entry_hash` UNIQUE, `type`, `subject_id` NULL, `received_at` | |
| `org_member` [P] | (`org_id`,`principal_id`) PK, `kind`, `role` CHECK in (`admin`,`member`,`device`), `joined_seq`, `removed_seq` NULL | |
| `wg` [P] | `id` PK, `org_id`, `is_default` bool, `created_seq`, `archived_seq` NULL | |
| `wg_access` [P] | (`wg_id`,`user_id`) PK, `access` CHECK in (`read`,`write`), `granted_seq`, `revoked_seq` NULL | default wg rows created for every member |
| `wg_device` [P] | (`wg_id`,`device_id`) PK, `assigned_seq`, `unassigned_seq` NULL | |
| `platform_role` [P] | (`user_id`,`role`) PK, `granted_seq`, `revoked_seq` NULL | role `inviter` |
| `scope_epoch` | (`scope`,`scope_id`,`epoch`) PK, `created_at`, `created_seq` NULL | scope ∈ `org`,`wg`,`cred`; current = max epoch |
| `key_wrap` | (`scope`,`scope_id`,`epoch`,`recipient_id`) PK, `wrap` bytea, `wrapped_by`, `sig` bytea, `created_at` | insert-only by convention; `org`/`wg` wraps are deleted when the recipient is removed from the org, `cred` wraps are kept (the browser rotates `CK` away from holders it does not entitle, `06-web.md` §11 job 5, KOR-04) |

### 2.3 Events

| Table | Columns | Notes |
|---|---|---|
| `scope_head` | `scope_id` PK, `scope` text, `seq` bigint | `UPDATE … SET seq = seq + 1 RETURNING seq` inside the insert transaction ⇒ gapless per scope |
| `event` [AO] | (`scope_id`,`scope_seq`) PK; `id` UNIQUE; `scope`, `org_id`, `wg_id` NULL, `task_id` NULL, `session_id` NULL, `author_id`, `author_kind`, `kind`, `ref` text, `epoch`, `ts` (header), `lh` int, `header_text` text (canonical), `ct` bytea, `sig` bytea(64), `size` int, `origin_device_id` NULL, `origin_seq` bigint NULL, `received_at` | indexes: (`task_id`,`scope_seq`), (`session_id`,`kind`,`scope_seq`), (`kind`,`ref`), (`received_at`) |
| `device_stream` | `device_id` PK, `last_origin_seq` bigint | gapless device outbox (§5) |

Retention: events are kept for the lifetime of the organization (no deletion in v1). Size limits from
`04-crypto.md` E8.

### 2.4 Tasks, sessions, VMs

| Table | Columns | Notes |
|---|---|---|
| `task` | `id` PK, `org_id`, `wg_id`, `owner_id`, `state` CHECK in (`scheduling`,`starting`,`working`,`idle`,`failed`,`closed`), `state_reason` NULL, `created_at`, `state_at`, `last_activity_at`, `closed_at` NULL | title only in `task.meta` events |
| `task_transition` [AO] | (`task_id`,`seq`) PK, `state`, `reason`, `at`, `by_id` | |
| `session` | `id` PK, `task_id`, `org_id`, `wg_id`, `owner_id` (task owner), `device_id` NULL, `vm_id` NULL, `parent_session_id` NULL, `origin_operation_id` NULL, `depth` int (0 = main), `state` (§2.4.1), `state_reason` NULL, `model_access` CHECK in (`subscription`,`api_key`,`platform`) NULL, `model_id` NULL, `agent_kind` default `claude-code`, `agent_version` NULL, `estimate_ms` int NULL, `created_at`, `ready_at` NULL, `first_working_at` NULL, `ended_at` NULL | |
| `session_transition` [AO] | (`session_id`,`seq`) PK, `state`, `reason`, `at` (device clock), `received_at`, `reported_by` | drives the countdown status list |
| `vm` | `id` text PK (`vm-<8 hex>`), `device_id`, `wg_id` NULL, `state` CHECK in (`pool`,`assigning`,`running`,`stopping`,`stopped`,`deleted`), `golden_version`, `cpus`, `memory_mib`, `disk_gib`, `created_at`, `state_at` | reported by runner |

#### 2.4.1 Session states

`requested` → `vm_assigning` → `vm_ready` → `sandbox_ready` → `agent_starting` → `prompt_delivering` →
`working` ⇄ `idle` → (`suspended` ⇄ `resuming` → `working`) → `stopping` → `stopped`; any state → `failed`.
`ready_at` = first `prompt_delivering`; `first_working_at` = first `working` (= prompt accepted by the
UserPromptSubmit hook). Task state is derived: any session `working` ⇒ task `working`; main session `idle`
⇒ `idle`; main session before `working` ⇒ `starting` (`scheduling` while no device chosen); `failed` if the
main session failed and no retry is pending; `closed` only by user action.

### 2.5 Operations

| Table | Columns | Notes |
|---|---|---|
| `operation` | `id` PK, `org_id`, `wg_id`, `task_id`, `session_id`, `device_id`, `actor_id` (task owner), `type`, `state` CHECK in (`received`,`rejected`,`accepted`,`duplicate`,`denied`,`executing`,`succeeded`,`failed`,`expired`), `error_code` NULL, `retryable` bool, `dedupe_epoch` NULL, `dedupe_tag` NULL, `duplicate_of` NULL, `attempt` int, `params_event_id` NULL, `result_event_id` NULL, `created_at` (session request time), `decided_at`, `started_at`, `finished_at` | |
| `operation_transition` [AO] | (`operation_id`,`seq`) PK, `state`, `error_code`, `at`, `by_id` | |
| `dedupe_slot` | (`wg_id`,`epoch`,`tag`) PK, `operation_id`, `expires_at` | slot taken on `accepted`, released on `failed`/`rejected`/`expired` of that operation |

### 2.6 Whiteboards and notifications

| Table | Columns | Notes |
|---|---|---|
| `whiteboard_entry` [P] | `id` PK (= `ref`), `org_id`, `level` CHECK in (`org`,`workgroup`,`task`), `wg_id` NULL, `task_id` NULL, `author_id`, `created_event_id`, `latest_event_id`, `created_at`, `updated_at`, `deleted` bool, `notify` bool | projection of whiteboard events (history = the events) |
| `session_ping` | `id` PK, `session_id`, `kind` CHECK in (`whiteboard.entry`,`subagent.finished`,`operation.finished`), `level` NULL, `ref`, `created_at`, `delivered_at` NULL | durable content-free pings; undelivered rows are returned in `GET /v1/device/state`; set delivered by the outbox audit item `ping.delivered`; the hub writes only `whiteboard.entry` rows (runner pings live in the runner's `ping` table, KOR-07) |
| `notification` | `id` PK, `recipient_id`, `kind` CHECK in (`whiteboard.entry`,`task.idle`,`task.failed`,`invite.accepted`,`rotation.required`), `org_id`, `wg_id` NULL, `task_id` NULL, `ref` NULL, `created_at`, `read_at` NULL | content-free |

### 2.7 Credentials and device requests

| Table | Columns | Notes |
|---|---|---|
| `credential` | `id` PK, `owner_id`, `kind`, `version` int, `epoch` int, `header_text`, `ct`, `sig`, `updated_at` | UNIQUE (`owner_id`,`kind`); **hard-deleted** on disconnect (only mutable content table) |
| `device_request` | `id` PK, `user_id`, `device_id`, `kind` CHECK in (`github.connect`,`claude.login`,`credential.check`,`mailbox.check`), `state` CHECK in (`pending`,`delivered`,`answered`,`failed`,`expired`), `envelope` text, `created_at`, `expires_at` (+15 min) | |
| `device_response` | (`request_id`,`seq`) PK, `envelope` text, `created_at` | |

### 2.8 Metrics, usage, quotas

| Table | Columns | Notes |
|---|---|---|
| `metric_sample` | (`entity_kind`,`entity_id`,`ts`) PK; `entity_kind` CHECK in (`device`,`vm`,`session`); `device_id`; `cpu_ratio` real (1.0 = one core); `mem_used_bytes`, `mem_limit_bytes`, `disk_used_bytes`, `disk_limit_bytes`, `net_rx_bytes`, `net_tx_bytes` (counters), `uptime_seconds`, `reachable` bool, `state` text, `free_vm_slots` int NULL, `free_mem_bytes` bigint NULL | raw, kept 48 h |
| `metric_rollup` | same key with `ts` = 5-minute bucket; avg and max columns for gauges, last value for counters | kept 90 days; built every 5 min |
| `usage_counter` | `session_id` PK, `requests`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `thinking_tokens`, `cost_micro_usd` bigint, `cost_basis` CHECK in (`provider`,`list_price`,`subscription_equivalent`), `context_used_tokens`, `context_window_tokens`, `last_provider` text NULL, `updated_at` | |
| `quota_snapshot` | (`owner_id`,`window`) PK, `used_percent` real, `resets_at`, `severity` NULL, `fetched_at`, `device_id` | windows `five_hour`, `seven_day`, `weekly:<model>`; an `unavailable` report of the polling device deletes the owner's rows fetched at or before it (KOR-05) |

### 2.9 Audit and read log

| Table | Columns | Notes |
|---|---|---|
| `audit` [AO] | `seq` bigint PK (gapless, assigned under advisory lock `742180101`), `at`, `actor_id` NULL, `actor_kind` CHECK in (`user`,`device`,`session`,`hub`,`operator`,`anonymous`), `action` text, `result` CHECK in (`ok`,`denied`,`rejected`,`duplicate`,`failed`), `org_id`, `wg_id`, `task_id`, `session_id`, `device_id`, `target_kind`, `target_id`, `detail` jsonb, `event_id` NULL, `prev_hash` text, `hash` text, `agent_id` NULL (KOR-24: the agent whose request wrote the row; part of the hashed row only when set) | `hash = sha256_hex("agent-audit-v1\n" ‖ seq ‖ "\n" ‖ canonical(row without hash fields) ‖ "\n" ‖ (prev_hash or "-"))` |
| `audit_checkpoint` [AO] | (`hub_key_id`,`seq`) PK, `hash`, `published_at`, `signature` | hourly, text `agent-audit-head-v1\n<key>\n<seq>\n<hash>\n<time>` signed with the hub key (format of `record/checkpoint.py`) |

`action` values (closed list, `contracts/enums/audit-actions.json`; the subset runners may send via the outbox is `contracts/enums/outbox-audit-actions.json`, `03-api.md` §6.1): authentication (`auth.register`, `auth.login`,
`auth.recovery` (detail `step`: `begin`, `finish`, `rotate`), `auth.passkey_add`, `auth.passkey_remove`, `auth.session_end`, `auth.refused`), invitations,
log appends (`log.<type>`), `event.append`, `task.create`, `task.close`, `session.<state>`, `session.control`,
`operation.<state>`, `credential.put`, `credential.delete`, `device_request.<state>`, `device.enroll`,
`device.certify`, `device.connect`, `device.disconnect`, `whiteboard.notify`, `ping.delivered`,
`hook.denied` (metadata only: session, tool name), `session.prompt_refused`, `model.provider_outside_allowlist`, `runner.inbound_rejected`, `runner.log_rollback`, `runner.log_fork`, `runner.alert`, `platform.<entry type>`, `read.<resource>` (every authenticated read endpoint and
every session platform read reported by a runner). `detail` schemas per action list only metadata fields.

## 3. Event log format

- Wire and storage format of one event: `04-crypto.md` §4.3. The hub stores `header_text` exactly as received
  (canonical) plus extracted columns; `ct` and `sig` as bytes.
- Order: `scope_seq` per scope (org or workgroup), gapless, assigned at receipt. Readers keep one cursor per
  scope. There is no global order across scopes; UIs merge by `(ts, scope_seq)`.
- Event bodies per kind: `03-api.md` §10 and `contracts/events/<kind>.json`.
- Export/backup: `agent-hub export --org <id>` writes JSON Lines `agent-hub-export-v1` (header line, log
  entries, wraps, events, credentials metadata, signed trailer) with the export writer form of `record/export.py`;
  restore is into an empty database only. Besides every row of the org and of its principals, each org export
  holds the hub-wide single chains complete – platform log and the whole audit chain with checkpoints, including
  other orgs' entries – so it is an operator backup, not an org data takeout (DR-01 decision 1,
  `runbooks/export-restore.md`). Required for disaster recovery tests (`07-testing.md` §5.6).

## 4. Runner store (PostgreSQL schema `runner`, local cluster)

Cluster managed like `agent_cli.pg` (`initdb --auth=trust`, loopback only, own port in `$AGENT_RUNNER_HOME/pg/port`).
Identity is kept outside the database in `$AGENT_RUNNER_HOME/device.json` (`device_id`, `kind`, `hub_url`,
`hub_key`, `enrolled_at`) and `$AGENT_RUNNER_HOME/keys/device.key` (`{"kem","sig","v":1}`, 0600) so a database
wipe never changes the identity (legacy rule).

| Table | Columns | Notes |
|---|---|---|
| `scope_key` | (`scope`,`scope_id`,`epoch`) PK, `key_ct` bytea (AES-GCM under the storage key), `source` CHECK in (`package`,`wrap`), `created_at` | |
| `log_entry` | (`org_id`,`seq`) PK, `entry_text`, `entry_hash` | verified copy; `log_head` (`org_id` PK, `seq`, `hash`) |
| `platform_log_entry` | `seq` PK, `entry_text`, `entry_hash` | |
| `outbox` | `origin_seq` bigint PK (gapless, `pg_advisory_xact_lock(742180201)` + `MAX+1`), `item_kind` CHECK in (`event`,`session_transition`,`vm_state`,`operation_transition`,`usage`,`quota`,`metrics`,`audit`,`wrap`) (= `contracts/enums/outbox-item-kinds.json`), `body_ct` bytea (item body as plaintext JSON encrypted with the storage key), `sealed` jsonb NULL (for `event`: the sealed event, stored at the first push attempt and reused for every retry; replaced only after `status:"reseal"`), `created_at`, `pushed_at` NULL | operation registrations, credentials and device responses use their own endpoints (`03-api.md` §6.1), never the outbox; after a store wipe numbering continues at `last_origin_seq + 1` from `GET /v1/device/state`; acknowledged items older than 1 h are deleted (a prefix only, the newest item stays; KOR-03, `05-runner.md` §2.3); events that cannot be sealed yet wait in `outbox_held` (ADR-047) |
| `sync_state` | `key` PK, `value` | `pushed_origin_seq`, `hub_ws_last_connect` |
| `inbox_cursor` | `scope_id` PK, `scope_seq` | |
| `golden` | `version` PK, `lima_name`, `claude_version`, `guest_version`, `image_digest`, `built_at`, `state` CHECK in (`building`,`ready`,`retired`) | |
| `vm` | `id` PK (`vm-<8 hex>`), `lima_name` UNIQUE, `golden_version`, `wg_id` NULL, `org_id` NULL, `state`, `control_token_hash`, `cpus`, `memory_mib`, `created_at`, `assigned_at`, `last_session_at` | partial UNIQUE index on (`wg_id`) WHERE `state` <> `deleted` – one VM per workgroup and device |
| `session` | `id` PK, `task_id`, `org_id`, `wg_id`, `vm_id`, `parent_id`, `depth`, `uid` int, `state`, `claude_session_id`, `model_access`, `model_id`, `credential_owner`, `created_at`, `ready_at`, `ended_at`, `last_prompt_at` | |
| `prompt_delivery` | `event_id` PK, `session_id`, `state` CHECK in (`pending`,`registered`,`typed`,`accepted`,`refused`,`failed`), `updated_at` | |
| `operation` | `id` PK, `session_id`, `task_id`, `wg_id`, `type`, `state`, `params` jsonb (plaintext, local only), `result` jsonb, `error_code`, `dedupe_tag`, `attempt`, `created_at`, `updated_at` | |
| `guest_cursor` | `vm_id` PK, `journal_seq` | |
| `credential_cache` | (`owner_id`,`kind`) PK, `version`, `record` jsonb (encrypted record as stored at the hub) | decrypted only in memory |
| `device_request` | `id` PK, `kind`, `user_id`, `state`, `updated_at` | local progress of multi-step requests |
| `ping` | `id` uuid PK, `session_id`, `kind` CHECK in (`whiteboard.entry`,`subagent.finished`,`operation.finished`), `level` NULL CHECK in (`org`,`workgroup`,`task`) (set exactly for `whiteboard.entry`), `ref`, `created_at`, `sent_at` NULL, `delivered_at` NULL | pings this device hands to a guest: the durable `pings` of `GET /v1/device/state` and the runner's own `subagent.finished`/`operation.finished` (05 §9); `sent_at` once the guest kept it (unsent rows are resent by reconcile), `delivered_at` once the guest journaled `ping.delivered`; rows of sessions ended an hour ago are deleted (RUN-14) |

Further runner tables added by later migrations (`profile_event`, `prompt_event`, `workgroup_settings`,
`operation_result`, `metric_pending`, `read_event`, `operator_binding` – the operator's token file a session is bound
to, file name only, ADR-052) and added columns are listed in
[`../spec-deviations.md`](../spec-deviations.md#02-data-model).

## 5. Guest journal (SQLite, `/var/lib/agent-guest/journal.db`, owner root (the `agent-guest-control` process), 0600; the unprivileged `agent-guest-api` process writes only through `api.sock`)

| Table | Columns | Notes |
|---|---|---|
| `journal` | `seq` INTEGER PK AUTOINCREMENT, `ts` text, `session_id`, `kind`, `body` text (JSON, plaintext) | append-only by code; rows acked by the runner (`POST /v1/journal/ack`) and older than 24 h are pruned (at start and every 10 min) |
| `session` | `id` PK, `uid`, `token_sha256`, `state`, `workspace`, `claude_session_id`, `created_at` | token compared constant-time against the hash |
| `prompt_registry` | (`session_id`,`sha256`) PK, `registered_at`, `used_at` NULL | |
| `pending` | `id` PK, `session_id`, `kind` CHECK in (`operation`,`query`), `request` text, `state`, `answer` text NULL, `created_at` | MCP calls waiting for the runner; a row unanswered for 1 h is removed (at start and every 10 min) with `guest.error pending_expired` of its session (KOR-02) |
| `ping` | `id` PK, `session_id`, `kind` CHECK in (`whiteboard.entry`,`subagent.finished`,`operation.finished`), `level` NULL, `ref`, `created_at`, `noticed_at` NULL, `delivered_at` NULL | content-free notices; `noticed_at` when a hook answer carried the notice, `delivered_at` when `get_notifications` returned it (RUN-14) |

Journal kinds: `session.state`, `session.output`, `session.tool`, `session.hook`, `session.file`,
`session.terminal`, `usage`, `operation.request`, `platform.query`, `ping.delivered`, `platform.read`, `platform.refused`, `metrics`, `guest.error`.
Bodies are defined in `03-api.md` §7.2.

## 6. Migrations

| Item | Hub | Runner | Guest |
|---|---|---|---|
| Location | `src/agent_hub/db/migrations/NNNN_<name>.sql` | `src/agent_runner/store/migrations/NNNN_<name>.sql` | `src/agent_guest/schema.sql` (+ `PRAGMA user_version`) |
| Tool | `agent-hub migrate` (owner DSN; refuses superuser and the writer role; `SET ROLE NONE`, `search_path = pg_catalog, pg_temp`; advisory lock `742180100`, 60 s lock timeout) | applied by `agent-runner` at start under advisory lock `742180200` | applied by `agent-guest` at start |
| Version table | `hub.schema_version(version int PK, name, checksum text, applied_at)` | `runner.schema_version` (same) | `user_version` |
| Rules | forward-only; checksum of applied files must match (`migration_checksum_mismatch`); a migration never edits an applied file; every migration has a test that applies it on the previous schema with fixtures | same | the golden image version pins the schema; a new schema needs a new golden image |
| Output | one line: `applied <n>`, `unchanged <n>` or `failed <step>`; never database error text (legacy rule) | same | log line |

Test databases: `AGENT_HUB_TEST_POSTGRES` (fixture creates `t_hub_<hex>` with owner/writer roles, pattern of
`agent-core/tests/conftest.py`), runner tests use `AGENT_RUNNER_TEST_PG` or an ephemeral cluster (pattern of
`agent/tests/conftest.py`).
