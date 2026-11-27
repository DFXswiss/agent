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

The community tables (`community_*`, migration 0014) are specified in `14-community.md` §2 (KOR-26, ADR-069).

### 2.1 Principals, authentication

| Table | Columns | Notes |
|---|---|---|
| `principal` | `id` PK, `kind` text CHECK in (`user`,`device`), `created_at`, `disabled_at` NULL | |
| `user_identity` | `user_id` PK→principal, `kem_pub` bytea(32), `sig_pub` bytea(32), `recovery_id` text UNIQUE, `created_at`, `id_sig` bytea(64) NULL, `id_invite_seq` NULL (both or neither) | identity public keys; `id_sig` = the identity proof of a registration through the platform invitation at `id_invite_seq` (ADR-060) |
| `user_settings` | `user_id` PK, `model_access` text CHECK in (`auto`,`subscription`,`api_key`,`platform`) default `auto`, `model_id` text NULL, `locale` text CHECK in (`de`,`en`) default `en`, `fallback_own_to_dfx` bool default false, `fallback_operator_to_platform` bool default true (KOR-50, [ADR-081](../adr/081-model-access-fallback-and-dfx-access-on-own-hardware.md) §1), `updated_at` | plaintext preferences |
| `identity_bundle` | (`user_id`,`slot`) PK, `ct` bytea, `updated_at` | slots `passkey:<b64u id>`, `recovery` |
| `passkey` | `credential_id` bytea PK, `user_id`, `public_key` bytea(65) (uncompressed P-256 point; only ES256 is offered and accepted), `sign_count` bigint, `transports` text[], `prf_seen` bool, `created_at`, `last_used_at`, `removed_at` NULL | verification via `agent_core.passkey` |
| `ceremony` | `id` PK, `kind` CHECK in (`register`,`login`,`add_passkey`,`recovery`,`recovery_rotate`), `challenge` bytea(32), `user_id` NULL, `invite_id` NULL, `hub_value` bytea NULL, `created_at`, `expires_at` (= +300 s), `used_at` NULL, `pairing_id` NULL (a login that confirms an agent pairing, KOR-24) | purged after 1 day; `recovery_rotate`: passkey proof before a recovery code is replaced (`03-api.md` §4.1, KOR-01) |
| `browser_session` | `key_id` text PK (`session.<32 hex>`), `user_id`, `credential_id`, `public_key` bytea (P-256 point), `statement` text, `created_at`, `expires_at`, `ended_at` NULL, `pairing_id` NULL (KOR-24) | |
| `agent` | delegated agent access of a user (`11-agent-access.md` §2, migration 0010): `id` PK, `user_id` NULL, `kem_pub`, `sig_pub`, `state`, `bundle_ct`, `label_ct`, `claim_hash`, `claim_misses`, `source`, `created_at`, `expires_at`, `confirmed_at`, `claimed_at`, `last_used_at`, `revoked_at`; hub migration 0026 (ADR-082): `own_identity` bool, `held_identity` bool (default true), `gen` int NULL, `cert` bytea(64) NULL, `user_ct` bytea NULL (sealed user anchor, in `bundle_ct`'s place), `session_id` uuid NULL, `revoked_reason` NULL CHECK in (`revoked`,`replaced`,`unclaimed`,`session_ended`); KOR-57 stage b (migration 0031, ADR-082 §6): `device_id` uuid NULL and `device_sig` bytea(64) NULL (exactly with `session_id`: the orchestrator session's device and its signature over the keys' fingerprint), `handover_sig` bytea(64) NULL (the task owner's signature over `handover_ct` of a session row) | a confirmed or active row holds `bundle_ct` or `user_ct`; an own-identity row has `cert` and `gen` and no `bundle_ct`; `gen` counts per user (`max+1`), so an own-identity row is revoked (`unclaimed`), never deleted |
| `request_nonce` | (`key_id`,`nonce`) PK, `seen_at` | rows older than 600 s deleted on insert (legacy rule) |
| `device` | `id` PK→principal, `kind` CHECK in (`platform`,`org`), `kem_pub`, `sig_pub`, `owner_org` NULL, `state` CHECK in (`pending_certification`,`active`,`decertified`,`removed`), `enrolled_at`, `certified_seq` NULL, `agent_version`, `os`, `arch`, `capacity` jsonb (`{"vm_memory_budget_mib","vm_memory_used_mib","vm_slots_total","vm_slots_free","pool_ready"}` plus the optional counters and settings of ADR-075 §2), `updated_at`, `offline_since` NULL (KOR-58), `features` text[] (capabilities announced on the last live-channel connection, e.g. `log.admit`; ADR-060) | |
| `device_presence` | `device_id` PK, `hub_instance` text, `connected_at`, `last_heartbeat_at` | online ⇔ heartbeat < 30 s ago |
| `device_enrollment` | `invite_id` PK, `device_id`, `request` jsonb (enrollment body incl. signatures), `created_at`, `certified_at` NULL | platform devices only |
| `invite` | `id` text PK (= `invite_id`), `scope` CHECK in (`platform`,`org`), `kind` CHECK in (`user`,`device`), `org_id` NULL, `log_seq` int NULL, `created_by`, `created_at`, `expires_at`, `registered_user` NULL (user registered through this invitation; at most one), `registered_at` NULL, `used_at` NULL, `used_by` NULL, `revoked_at` NULL, `package_ct` bytea NULL, `pk_inv` NULL (public invitation key, ADR-043; required to register) | package deleted (set NULL) on use, revocation or expiry; platform user invitations from the operator CLI also appear as `platform_entry` type `user_invite`; registrations without accept/genesis within 24 h are deleted and `registered_user` reset |

### 2.2 Organizations, logs, keys

| Table | Columns | Notes |
|---|---|---|
| `org` | `id` PK, `private` bool, `platform_access` bool [P] (true for private orgs created from a platform invitation; else from the latest `org_platform_access` platform-log entry, ADR-026, and without one from the switch `open_platform_access`, ADR-079), `platform_access_grant` bool NULL [P] (the latest `org_platform_access` of the org; migration 0023), `default_wg` uuid, `created_by`, `created_at`, `log_head_seq` int, `log_head_hash` text, `rotation_pending` jsonb (list of `{scope,scope_id}`) | |
| `membership_entry` [AO] | (`org_id`,`seq`) PK, `entry_text` text (canonical JSON of the entry), `entry_hash` text UNIQUE, `type`, `by_id`, `subject_id` NULL, `received_at` | appended under `SELECT … FOR UPDATE` of `org` row |
| `platform_entry` [AO] | `seq` PK, `entry_text`, `entry_hash` UNIQUE, `type`, `subject_id` NULL, `received_at` | |
| `org_member` [P] | (`org_id`,`principal_id`) PK, `kind` (`user`/`device`; an agent of the log never gets a row, ADR-082), `role` CHECK in (`admin`,`member`,`device`), `joined_seq`, `removed_seq` NULL, `consented` bool default true | `consented` false for a user that entered by `admit` (ADR-060): the org's devices are not eligible for its `CK` |
| `wg` [P] | `id` PK, `org_id`, `is_default` bool, `created_seq`, `archived_seq` NULL | |
| `wg_access` [P] | (`wg_id`,`user_id`) PK, `access` CHECK in (`read`,`write`), `granted_seq`, `revoked_seq` NULL, `inherited` boolean (only with `read`; KOR-34) | the effective access: default wg rows created for every member; `inherited` rows give every admin `read` on every other workgroup (ADR-058); credential eligibility ignores inherited rows |
| `wg_device` [P] | (`wg_id`,`device_id`) PK, `assigned_seq`, `unassigned_seq` NULL | effective assignments: explicit and through the organization (ADR-075) |
| `org_device` [P] **(new, ADR-075)** | (`org_id`,`device_id`) PK, `assigned_seq`, `unassigned_seq` NULL | assignments to the whole organization |
| `wg_placement` **(new, ADR-075)** | `wg_id` PK, `device_id`, `vm_id` NULL, `set_by`, `set_at` | the workgroup's default device and VM for new tasks |
| `platform_role` [P] | (`user_id`,`role`) PK, `granted_seq`, `revoked_seq` NULL | role `inviter`, `operator` (ADR-079) or `support` (ADR-096) |
| `support_person` [P] | `user_id` PK, `active` (in the support roster of the verified platform log), `name` NULL (1–64, set by the operator), `changed_at` | ADR-096, hub migration 0038; rebuilt from the platform log, the name kept |
| `support_release` [P] | `wg_id` PK, `org_id`, `access` CHECK in (`read`,`write`), `granted_seq`, `granted_at` | the active `support_grant` per workgroup (ADR-096), a projection of the membership log |
| `support_access` | (`wg_id`,`user_id`,`granted_seq`) PK, `first_at` | the first access of a support person under a release (the notification `support.access` once) |
| `platform_policy` [P] | `name` PK (`open_platform_access`, `open_platform_invites`), `on` bool, `seq` | the switches of the platform log (ADR-079, migration 0023); a switch without a row is off |
| `scope_epoch` | (`scope`,`scope_id`,`epoch`) PK, `created_at`, `created_seq` NULL | scope ∈ `org`,`wg`,`cred`; current = max epoch |
| `key_wrap` | (`scope`,`scope_id`,`epoch`,`recipient_id`) PK, `wrap` bytea, `wrapped_by`, `sig` bytea, `created_at` | insert-only by convention; `org`/`wg` wraps are deleted when the recipient is removed from the org, `cred` wraps are kept (the browser rotates `CK` away from holders it does not entitle, `06-web.md` §11 job 5, KOR-04) |

### 2.3 Events

| Table | Columns | Notes |
|---|---|---|
| `scope_head` | `scope_id` PK, `scope` text, `seq` bigint | `UPDATE … SET seq = seq + 1 RETURNING seq` inside the insert transaction ⇒ gapless per scope |
| `event` [AO] | (`scope_id`,`scope_seq`) PK; `id` UNIQUE; `scope`, `org_id`, `wg_id` NULL, `task_id` NULL, `session_id` NULL, `author_id`, `author_kind` (`user`,`device`,`agent`: an own-identity agent, ADR-082), `kind`, `ref` text, `epoch`, `ts` (header), `lh` int, `header_text` text (canonical), `ct` bytea, `sig` bytea(64), `size` int, `origin_device_id` NULL, `origin_seq` bigint NULL, `received_at` | indexes: (`task_id`,`scope_seq`), (`session_id`,`kind`,`scope_seq`), (`session_id`,`scope_seq`) WHERE `session_id` IS NOT NULL (newest-first pages of a session, KOR-35), (`kind`,`ref`), (`received_at`) |
| `device_stream` | `device_id` PK, `last_origin_seq` bigint | gapless device outbox (§5) |

Retention: events are kept for the lifetime of the organization (no deletion in v1). Size limits from
`04-crypto.md` E8.

### 2.4 Tasks, sessions, VMs

| Table | Columns | Notes |
|---|---|---|
| `task` | `id` PK, `org_id`, `wg_id`, `owner_id`, `github_identity` NULL (KOR-47), `state` CHECK in (`scheduling`,`starting`,`working`,`idle`,`failed`,`closed`), `state_reason` NULL, `created_at`, `state_at`, `last_activity_at`, `closed_at` NULL, `placement_device_id` NULL, `placement_vm_id` NULL (the chosen device and VM, ADR-075 §6), `number` bigint (KOR-52: 1, 2, 3 … per organization, set by the trigger `task_number` from `task_counter`; UNIQUE (`org_id`,`number`)) | title only in `task.meta` events, the main agent's name only in `task.agent_name` events |
| `task_transition` [AO] | (`task_id`,`seq`) PK, `state`, `reason`, `at`, `by_id` | |
| `session` | `id` PK, `task_id`, `org_id`, `wg_id`, `owner_id` (task owner), `github_identity` NULL (KOR-47), `device_id` NULL, `vm_id` NULL, `parent_session_id` NULL, `origin_operation_id` NULL, `depth` int (0 = main), `state` (§2.4.1), `state_reason` NULL, `model_access` CHECK in (`subscription`,`api_key`,`platform`) NULL, `model_id` NULL, `agent_kind` default `claude-code`, `agent_version` NULL, `estimate_ms` int NULL, `created_at`, `ready_at` NULL, `first_working_at` NULL, `ended_at` NULL, `context_restore` CHECK in (`restored`,`fallback`) NULL, `context_reason` NULL (closed list `contracts/common.json#/$defs/context_reason`), `interrupted_at` NULL, `interruption_ended_at` NULL, `failover_of` NULL, `redeliver_event_id` NULL, `restore_object` NULL (KOR-58), `orchestrator_at` timestamptz NULL (KOR-57, hub migration 0032: the task owner's latest request to make the session the main orchestrator, `11-agent-access.md` §11) | `context_*`: how a main session continued the conversation (ADR-072 §7, KOR-33): `fallback` always with a fallback reason (`context_missing`, `context_invalid`, `context_unavailable`, `version_incompatible`, `restore_failed`); `restored` with no reason or `workspace_partial`/`workspace_omitted`/`golden_switch` (the session's VM was switched to a newer golden while it was paused, ADR-093 §4); null with no reason (checked by the constraint `session_context_reason`) |
| `task_attention` [P] | `task_id` PK, `org_id`, `wg_id`, `owner_id`, `category` CHECK in (`needs_decision`,`blocked`,`working`,`done`), `reason` (code), `ref_kind` CHECK in (`operation`,`session`) NULL, `ref_id` NULL, `since`, `seq` ≥ 1, `subagents_total`, `subagents_active` (subagent sessions not `stopped`/`failed`), `updated_at`; CHECK (`ref_kind` NULL) = (`ref_id` NULL); indexes (`org_id`,`category`,`since` DESC,`task_id` DESC), (`wg_id`,`category`,`since`), (`owner_id`,`category`,`wg_id`); `event` gets the partial index (`task_id`,`received_at` DESC) WHERE `kind` = `prompt` | projection, ADR-076 (KOR-39); derived from task, session and operation metadata; written only by `agent_hub.attention.store.refresh`; exported with the org |
| `session_transition` [AO] | (`session_id`,`seq`) PK, `state`, `reason`, `at` (device clock), `received_at`, `reported_by` | drives the countdown status list |
| `vm` | `id` text PK (`vm-<8 hex>`), `device_id`, `wg_id` NULL, `state` CHECK in (`pool`,`assigning`,`running`,`stopping`,`stopped`,`deleted`), `golden_version`, `cpus`, `memory_mib`, `disk_gib`, `created_at`, `state_at`, `upgrade` jsonb NULL, `replaced_by` NULL, `switch_requested_at` NULL, `switch_requested_by` NULL (hub migration 0035, KOR-70) | reported by runner; `upgrade` is the switch status of an outdated workgroup VM (`contracts/common.json#/$defs/vm_upgrade`, NULL once current), `replaced_by` the VM of the newest golden that replaced it; `switch_requested_*` a member with administration rights who asked for the switch now (until the VM is current or deleted, ADR-093) |

Model access with fallback (KOR-50, migration `0025_model_fallback`,
[ADR-081](../adr/081-model-access-fallback-and-dfx-access-on-own-hardware.md) §§4–5) adds `task.model_consent` bool
default false (a person allowed DFX access for the task while its own access is used up), `session.wait_until`
timestamptz NULL and `session.dfx_choice` bool default false (the wait of a session suspended with
`model_quota_exhausted`) and `task_attention.wait_until` timestamptz NULL (the time a `quota_wait` / `quota_choice`
record continues by itself).

Process analysis (KOR-53, [ADR-097](../adr/097-process-analysis.md), migration `0039_process_analysis`):
`task.analysis` jsonb NULL (`{scope: org|wg, since, until}` of an analysis task, plaintext metadata of the request);
indexes `session (wg_id, created_at DESC, id DESC)`, `operation (wg_id, created_at DESC, id DESC)` and `audit (wg_id,
seq DESC) WHERE wg_id IS NOT NULL` for the session reads of one workgroup's usage, operations and audit.

Device failure (KOR-58, ADR-087, migration `0031_device_failure`): `device.offline_since` (hub time the device went
offline; cleared by a connect or heartbeat); `session.interrupted_at` (an active session of a failed device; it
stays) and `interruption_ended_at` (its device was back before a move; interrupted while the first is newer),
`failover_of` (the lost session a successor continues), `redeliver_event_id` (the prompt a lost session hands to its
successor again), `restore_object` (the checkpoint pinned for a successor).

#### 2.4.1 Session states

`requested` → `vm_assigning` → `vm_ready` → `sandbox_ready` → `agent_starting` → `prompt_delivering` →
`working` ⇄ `idle` → (`suspended` ⇄ `resuming` → `working`) → `stopping` → `stopped`; any state → `failed`.
`ready_at` = first `prompt_delivering`; `first_working_at` = first `working` (= prompt accepted by the
UserPromptSubmit hook). Task state is derived: any session `working` ⇒ task `working`; main session `idle`
⇒ `idle`; main session before `working` ⇒ `starting` (`scheduling` while no device chosen); `failed` if the
main session failed and no retry is pending; `closed` only by user action.

#### 2.4.2 Context checkpoints and pull-request links (KOR-33, ADR-072)

| Table | Columns | Notes |
|---|---|---|
| `context_object` | `id` uuid PK (UUIDv7 of the author device), `org_id`, `wg_id`, `task_id` → `task` **on delete cascade**, `session_id` (a main session), `author_id` (device), `state` CHECK in (`uploading`,`complete`), `chunks`, `size` (sealed bytes stored so far), `epoch` NULL, `digest` NULL, `manifest_text` NULL, `sig` bytea NULL, `created_at`, `updated_at` (last chunk), `completed_at` NULL, `kind` CHECK in (`context`,`log`) default `context` (KOR-43: a log of a test run or work VM job, ADR-080 §3; listing, pruning by `AGENT_HUB_CONTEXT_KEEP`, the device state's `context` and the device downloads see only `context`; logs are pruned beyond `AGENT_HUB_LOG_KEEP` per task and have their own org quota) | `complete` ⇔ `completed_at` set, and then epoch, digest, manifest and signature are set. Only complete objects are listed or downloaded. Newest first = (`session.created_at` of its session, `id`) descending (`03-api.md` §4.6.3); beyond `AGENT_HUB_CONTEXT_KEEP` per task they are deleted at the next commit; uploads without a chunk for 6 h are deleted by the job `contexts.expire`. Quota sums count every object of the org |
| `context_chunk` | (`object_id` → `context_object` on delete cascade, `chunk_index`) PK, `ct` bytea (sealed chunk, `04-crypto.md` §4.11), `sha256` bytea | ciphertext only; inside the transactional backup |
| `browser_watch` (UNLOGGED, KOR-44) | (`session_id`, `instance` text) PK, `until` timestamptz, `kbps` integer | the watch lease of one hub process for a session's platform browser (13-work-vm.md §13.6): written when the process's first stream watches, renewed every 10 s (`until` = now + 30 s), deleted when its last watching stream ends; not backed up |
| `browser_live` (UNLOGGED, KOR-44) | `session_id` uuid PK, `browser_id` text, `seq` bigint, `frame` jsonb (sealed frame, `04-crypto.md` §4.13), `received_at` | the latest live frame of a watched session only (overwritten per frame, deleted with the last valid lease); ciphertext only; frequent updates of rows up to 512 KiB rely on autovacuum (unlogged, never in a backup) |
| `pr_link` | (`wg_id`,`epoch`,`tag`) PK, `task_id` → `task` on delete cascade, `session_id` NULL, `created_at`, `sealed` text NULL (KOR-48), `linked_by` uuid NULL (KOR-48); CHECK exactly one of `session_id`, `linked_by`; index `pr_link_list` (`wg_id`, `created_at` DESC, `epoch` DESC, `tag` byte order DESC) | opaque HMAC tag of a pull request (repository and number never reach the hub); the first task keeps a tag, except that the device's link replaces a person's (ADR-084 §1). `sealed`: the AEAD body naming the pull request (`04-crypto.md` §4.11a; 379 b64u characters, NULL for a link an older runner registered); `session_id`: the session whose device registered it after `github.create_pull_request`; `linked_by`: the user who linked it by its address (migration `0028_pr_index`). The only table matching the "no pull-request table" guard of ADR-014 |

#### 2.4.3 Shared sessions (KOR-46, ADR-083)

| Table / column | Content | Notes |
|---|---|---|
| `session.triggered_by` NULL | the person whose instruction created the session: the author of the prompt a main session was created for, the triggering person of the `subagent.request` of a child | ID only; null for rows before KOR-46 or when unknown |
| `operation.triggered_by` NULL | the person whose instruction the operation follows from (the runner's stamp of ADR-070, accepted only when null or the author of a prompt of the task) | `actor_id` stays the task owner; KOR-45 intersects the rights of both |
| `charge.triggered_by` NULL, `charge_person_daily` | the person of a consumption item (`usage`/`runtime`) and the daily sum per organization, person (null = unattributed) and kind | IDs and amounts only ([A4](../KONZEPT.md#a-4)); booking to the organization unchanged |
| `prompt_withdrawal` | `event_id` PK (a `prompt` event), `task_id`, `requested_by`, `requested_at`, `withdrawn_at` NULL | `withdrawn_at` set at once for a session without device, else when the device reports `session.prompt_withdrawn`; withdrawn prompts leave the routing |

#### 2.4.4 Main agents (KOR-52, ADR-095)

Migration `0037_main_agents`: `task_counter` (`org_id` PK, `last` bigint) and `task.number` (assigned by the BEFORE
INSERT trigger `task_number`, existing tasks numbered per organization by `created_at`, `id`); `session.agent_path`
text NULL (CHECK the form of `contracts/api/objects.json#/$defs/agent_path`, NULL exactly for depth 0; set by the
BEFORE INSERT trigger `session_agent_path`: depth 1 = the next ordinal among the task's depth-1 sessions, deeper =
the parent's path, a dot and the next ordinal among the parent's children; UNIQUE (`task_id`,`agent_path`); existing
children numbered by `created_at`, `id`); the partial index `task_open_owner` (`org_id`,`owner_id`,
`last_activity_at` DESC,`id` DESC) WHERE `state` <> `closed`; `audit.on_behalf_of` uuid NULL and the actor kinds
`main_agent`, `subagent`.

### 2.5 Operations

| Table | Columns | Notes |
|---|---|---|
| `operation` | `id` PK, `org_id`, `wg_id`, `task_id`, `session_id`, `device_id`, `actor_id` (task owner), `github_identity` NULL, `triggered_by` NULL (KOR-47), `type`, `state` CHECK in (`received`,`rejected`,`accepted`,`duplicate`,`denied`,`executing`,`succeeded`,`failed`,`expired`), `error_code` NULL, `retryable` bool, `dedupe_epoch` NULL, `dedupe_tag` NULL, `duplicate_of` NULL, `attempt` int, `params_event_id` NULL, `result_event_id` NULL, `created_at` (session request time), `decided_at`, `started_at`, `finished_at` | |
| `operation_transition` [AO] | (`operation_id`,`seq`) PK, `state`, `error_code`, `at`, `by_id` | |
| `dedupe_slot` | (`wg_id`,`epoch`,`tag`) PK, `operation_id`, `expires_at` | slot taken on `accepted`, released on `failed`/`rejected`/`expired` of that operation |

### 2.6 Whiteboards and notifications

| Table | Columns | Notes |
|---|---|---|
| `whiteboard_entry` [P] | `id` PK (= `ref`), `org_id`, `level` CHECK in (`org`,`workgroup`,`task`), `wg_id` NULL, `task_id` NULL, `author_id`, `created_event_id`, `latest_event_id`, `created_at`, `updated_at`, `deleted` bool, `notify` bool | projection of whiteboard events (history = the events) |
| `session_ping` | `id` PK, `session_id`, `kind` CHECK in (`whiteboard.entry`,`subagent.finished`,`operation.finished`), `level` NULL, `ref`, `created_at`, `delivered_at` NULL | durable content-free pings; undelivered rows are returned in `GET /v1/device/state`; set delivered by the outbox audit item `ping.delivered`; the hub writes only `whiteboard.entry` rows (runner pings live in the runner's `ping` table, KOR-07) |
| `notification` | `id` PK, `recipient_id`, `kind` CHECK in (`whiteboard.entry`,`task.idle`,`task.failed`,`task.attention`,`invite.accepted`,`rotation.required`,`agent.account_change`,`org.admitted`,`operator.request`,`device.needed`,`prompt.waiting_for_device`,`support.release`,`support.access`,`support.people`), `org_id` (NULL only for `agent.account_change` and `operator.request`, ADR-079), `wg_id` NULL, `task_id` NULL, `ref` NULL, `created_at`, `read_at` NULL | content-free |

### 2.7 Credentials and device requests

| Table | Columns | Notes |
|---|---|---|
| `credential` | `id` PK, `owner_id` (a user, or an organization for a company GitHub account), `owner_kind` CHECK in (`user`,`org`), `kind`, `version` int, `epoch` int, `header_text`, `ct`, `sig`, `created_at`, `updated_at`, `is_primary` bool (KOR-47) | at most one primary record per owner and kind (`UNIQUE (owner_id, kind) WHERE is_primary`), every record of a kind other than `github`, `claude_subscription` and `mailbox` primary; several `github` records per owner (GitHub identities, ADR-078), up to 40 `claude_subscription` records per user (KOR-69, hub migration 0033, ADR-090: further ones never primary) and up to 100 `mailbox` records per user (KOR-74, hub migration 0040, ADR-099: the first one primary, further ones never), company records only of kind `github` and never primary; **hard-deleted** on disconnect (only mutable content table) |
| `github_default` | (`scope` CHECK in (`org`,`wg`), `scope_id`) PK, `identity_id` → `credential.id` ON DELETE CASCADE, `set_by`, `set_at` | default GitHub identity of an organization or workgroup (KOR-47); only company records of that organization |
| `device_request` | `id` PK, `user_id`, `device_id`, `kind` CHECK in (`github.connect`,`claude.login`,`credential.check`,`mailbox.check`,`github.pull_status`), `state` CHECK in (`pending`,`delivered`,`answered`,`failed`,`expired`), `envelope` text, `org_id` NULL (a `github.connect` for a company account, KOR-47), `slot` NULL (the root's subject, KOR-73, migration 0041: replacement only within one slot), `created_at`, `expires_at` (+15 min) | |
| `device_response` | (`request_id`,`seq`) PK, `envelope` text, `created_at` | |

### 2.8 Metrics, usage, quotas

| Table | Columns | Notes |
|---|---|---|
| `metric_sample` | (`entity_kind`,`entity_id`,`ts`) PK; `entity_kind` CHECK in (`device`,`vm`,`session`); `device_id`; `cpu_ratio` real (1.0 = one core); `mem_used_bytes`, `mem_limit_bytes`, `disk_used_bytes`, `disk_limit_bytes`, `net_rx_bytes`, `net_tx_bytes` (counters), `uptime_seconds`, `reachable` bool, `state` text, `free_vm_slots` int NULL, `free_mem_bytes` bigint NULL | raw, kept 48 h |
| `metric_rollup` | same key with `ts` = 5-minute bucket; avg and max columns for gauges, last value for counters | kept 90 days; built every 5 min |
| `usage_counter` | `session_id` PK, `requests`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_write_tokens`, `thinking_tokens`, `cost_micro_usd` bigint, `cost_basis` CHECK in (`provider`,`list_price`,`subscription_equivalent`), `context_used_tokens`, `context_window_tokens`, `last_provider` text NULL, `updated_at` | |
| `quota_snapshot` | (`owner_id`,`window`) PK, `used_percent` real, `resets_at`, `severity` NULL, `fetched_at`, `device_id` | windows `five_hour`, `seven_day`, `weekly:<model>`; an `unavailable` report of the polling device deletes the owner's rows fetched at or before it (KOR-05) |
| `subscription_use` | `credential_id` PK → `credential` ON DELETE CASCADE, `owner_id`, `pool` bool, `orgs` jsonb (array of organization IDs), `set_at` | the plaintext mirror of a further subscription's sealed use (KOR-69, hub migration 0034, ADR-090 §3), sent by the owner's client; it only decides which devices hear of the subscription (`device/state` `subscriptions.shared`), the runner acts on the sealed use |
| `mailbox_use` | `credential_id` PK → `credential` ON DELETE CASCADE, `owner_id`, `wgs` jsonb (non-empty array of workgroup IDs), `set_at` | the plaintext mirror of a mailbox's sealed release (KOR-74, hub migration 0040, ADR-099 §3), sent by the owner's client; it only decides which mailboxes a device is answered for a workgroup (`GET /v1/device/mailboxes`), the runner acts on the sealed release |
| `subscription_quota` | `credential_id` PK → `credential` ON DELETE CASCADE, `owner_id`, `windows` jsonb NULL, `unreadable` bool (= `windows IS NULL`), `fetched_at`, `device_id` | the newest reading of each further Claude subscription (KOR-69, hub migration 0033, ADR-090 §4; the primary record keeps `quota_snapshot`) |
| `quota_limit` | `owner_id` PK, `at`, `until` NULL, `readable` bool, `device_id`, `fetched_at` | the latest observed limit of an owner's own subscription (`quota.limited`, ADR-081 §6, KOR-50) |
| `operator_quota` | `device_id` PK, `at`, `snapshot` jsonb (array), `received_at` | the newest `operator_quota` snapshot of a platform device (file names, states, windows; never a token) |
| `dfx_issuer` | `device_id` PK, `announced_at` | platform devices that announced a management key (`dfx_key_state`, ADR-081 §7) |
| `dfx_key` | `request_id` PK, `org_id`, `device_id`, `requested_at`, `issuer_device_id` NULL, `key_ref` NULL UNIQUE, `envelope` NULL (ciphertext to the org device), `issued_at` NULL, `booked_micro_usd` bigint ≥ 0, `booked_at` NULL, `revoked_at` NULL, `closed_at` NULL | one live key per org device and organization; the highest cumulative provider spend booked (`12-billing.md` §3) |
| `dfx_key_cost` | (`device_id`,`session_id`) PK, `org_id`, `cost_micro_usd` | provider cost the sessions of an org device reported on its key since the last booking (weights of the split) |
| `ui_health_daily` | (`day` date,`version`,`view`) PK; `reports`, `blocks`, `blocked_ms` bigint; `max_bucket` smallint | anonymous UI health counters of the web clients (`POST /v1/telemetry/ui`, ADR-086 §5, KOR-35): per UTC day, client version and view type (closed list `contracts/enums/ui-views.json`); no user, org, device, address or object ID; only the version the hub serves by name, others count as `other`; kept 90 days (job `telemetry.ui_health_purge`); not exported with an org (hub-wide, `08-monitoring.md` §6.1) |

### 2.9 Audit and read log

| Table | Columns | Notes |
|---|---|---|
| `audit` [AO] | `seq` bigint PK (gapless, assigned under advisory lock `742180101`), `at`, `actor_id` NULL, `actor_kind` CHECK in (`user`,`device`,`session`,`hub`,`operator`,`anonymous`,`main_agent`,`subagent`) (`session` is no longer written; `main_agent`/`subagent`: ADR-095 §4), `action` text, `result` CHECK in (`ok`,`denied`,`rejected`,`duplicate`,`failed`), `org_id`, `wg_id`, `task_id`, `session_id`, `device_id`, `target_kind`, `target_id`, `detail` jsonb, `event_id` NULL, `prev_hash` text, `hash` text, `agent_id` NULL (KOR-24: the agent whose request wrote the row; part of the hashed row only when set), `on_behalf_of` NULL (KOR-52: the person a `main_agent`/`subagent` actor acted for; part of the hashed row only when set) | `hash = sha256_hex("agent-audit-v1\n" ‖ seq ‖ "\n" ‖ canonical(row without hash fields) ‖ "\n" ‖ (prev_hash or "-"))` |
| `audit_checkpoint` [AO] | (`hub_key_id`,`seq`) PK, `hash`, `published_at`, `signature` | hourly, text `agent-audit-head-v1\n<key>\n<seq>\n<hash>\n<time>` signed with the hub key (format of `record/checkpoint.py`) |

`action` values (closed list, `contracts/enums/audit-actions.json`; the subset runners may send via the outbox is `contracts/enums/outbox-audit-actions.json`, `03-api.md` §6.1): authentication (`auth.register`, `auth.login`,
`auth.recovery` (detail `step`: `begin`, `finish`, `rotate`), `auth.passkey_add`, `auth.passkey_remove`, `auth.session_end`, `auth.session_renew` (rule E-40, ADR-102), `auth.refused`), invitations,
log appends (`log.<type>`), `event.append`, `task.create`, `task.close`, `session.<state>`, `session.control`,
`operation.<state>`, `credential.put`, `credential.delete`, `device_request.<state>`, `device.enroll`,
`device.certify`, `device.connect`, `device.disconnect`, `whiteboard.notify`, `ping.delivered`,
`hook.denied` (metadata only: session, tool name), `request.refused`, `request.write`, `audit.repeat` (ADR-072), `event.append_batch` (the session's own events of one session and scope, summarised by the audit job; `event_audit_cover` (`scope_id` PK, `covered_seq`) keeps its progress, migration 0012), `session.prompt_refused`, `model.provider_outside_allowlist`, `runner.inbound_rejected`, `runner.log_rollback`, `runner.log_fork`, `runner.alert`, context checkpoints (ADR-072: `session.context_saved {chunks,size,pruned}` and `session.context_refused {reason,scope,limit,used,requested}` written by the hub for a commit and for a chunk refused for a quota, `session.context_restored {object_id,workspace,stale}` and `session.context_fallback {reason}` sent by the runner), `platform.<entry type>`, `read.<resource>` (every authenticated read endpoint and
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
| `vm` | `id` PK (`vm-<8 hex>`), `lima_name` UNIQUE, `golden_version`, `wg_id` NULL, `org_id` NULL, `state`, `control_token_hash`, `cpus`, `memory_mib`, `created_at`, `assigned_at`, `last_session_at` | several live VMs per workgroup and device (ADR-075 §4; the former partial UNIQUE index on `wg_id` is dropped by runner migration 0018); a VM belongs to one workgroup for its life |
| `session` | `id` PK, `task_id`, `org_id`, `wg_id`, `vm_id`, `parent_id`, `depth`, `uid` int, `state`, `claude_session_id`, `model_access`, `model_id`, `credential_owner`, `created_at`, `ready_at`, `ended_at`, `last_prompt_at` | |
| `prompt_delivery` | `event_id` PK, `session_id`, `state` CHECK in (`pending`,`registered`,`typed`,`accepted`,`refused`,`failed`,`withdrawn`), `via_agent` bool (the hub reported the author's agent, KOR-46), `updated_at` | `withdrawn`: final, never handed to a guest (ADR-083 §3) |
| `operation` | `id` PK, `session_id`, `task_id`, `wg_id`, `type`, `state`, `params` jsonb (plaintext, local only), `result` jsonb, `error_code`, `dedupe_tag`, `attempt`, `created_at`, `updated_at` | |
| `guest_cursor` | `vm_id` PK, `journal_seq` | |
| `credential_cache` | (`owner_id`,`kind`) PK, `version`, `record` jsonb (encrypted record as stored at the hub) | decrypted only in memory; for `github` the owner's primary record |
| `subscription_cache` (KOR-69, runner migration 0032) | Claude subscription records by ID (encrypted as at the hub, with owner, primary mark and version) | the further subscriptions this device logs in, measures and re-seals (ADR-090) |
| `subscription_renewal` (KOR-71, runner migration 0033) | renewals of further subscriptions not yet confirmed by the hub: the sealed next record version and the sealed record it replaces (ciphertext only) | the device that measures an owner stores the provider's new tokens before it writes them (ADR-092 §2) |
| `github_identity_cache`, `github_selection`, `github_default` (KOR-47) | identity records by ID (encrypted as at the hub, with owner and version); the GitHub identity of each session and its task; the defaults of the organizations and workgroups the device is assigned to | the last answer of the hub is kept, so a restart without the hub never acts with another account (ADR-078 §3) |
| `device_request` | `id` PK, `kind`, `user_id`, `state`, `updated_at` | local progress of multi-step requests |
| `browser_session` (KOR-44, migration 0028) | `browser_id` PK (`br-<12 hex>`), `operation_id` NULL (implicit open), `session_id`, `task_id`, `org_id`, `wg_id`, `workvm_id` NULL, `state` CHECK in (`open`,`closed`), `seq`, `frame_seq`, `idle_seconds`, `url`, `ready_ms`, `implicit`, `opened_at`, `last_call_at`, `closed_at`, `close_reason` | one open row per session (unique partial index); a row stays open across a lost VM or a restart (13-work-vm.md §13.2); `runner.workvm` scope `browser` holds its VM |

`runner.workvm` also has the scope `login` (KOR-73, migration 0039, ADR-100 §3): the browser sandbox of one Claude login, `session_id` = the login request, task, organization and workgroup the nil UUID, teardown reason `login_ended`; it counts for no limit and is not billed.
| `ping` | `id` uuid PK, `session_id`, `kind` CHECK in (`whiteboard.entry`,`subagent.finished`,`operation.finished`), `level` NULL CHECK in (`org`,`workgroup`,`task`) (set exactly for `whiteboard.entry`), `ref`, `created_at`, `sent_at` NULL, `delivered_at` NULL | pings this device hands to a guest: the durable `pings` of `GET /v1/device/state` and the runner's own `subagent.finished`/`operation.finished` (05 §9); `sent_at` once the guest kept it (unsent rows are resent by reconcile), `delivered_at` once the guest journaled `ping.delivered`; rows of sessions ended an hour ago are deleted (RUN-14) |

Further runner tables added by later migrations (`profile_event`, `prompt_event`, `workgroup_settings`,
`operation_result`, `metric_pending`, `read_event`, `operator_binding` – the operator's token file a session is bound
to, file name only, ADR-052; KOR-50 / ADR-081: `access_limit` – used-up or blocked subscription accesses kept across
restarts, `model_pause` – sessions paused for model quota; FIX-02: `platform_prompt` – prompts the platform hands
over by itself with the acting user of the turn they continue, `turn_retry` – turns a model rate limit ended and their
continuation; `dfx_key` – the sealed envelope of an org device's DFX key
per organization, `dfx_key_issued` – the requests an issuing platform device answered; KOR-35: `usage_pending` – the
usage deltas of a session merged over 10 s before they become one outbox item `usage`, counts only, ADR-086; KOR-45:
`subagent_instruction` – the instructions of a child session in their order of creation with their acting user and
when each was handed over, accepted and its turn ended, no text, ADR-088 §1; KOR-70: `golden_switch` – a workgroup VM
replaced by one of a newer golden while sessions on it were paused, `golden_switch_hold` – since when a cause holds
an outdated VM, and the column `context_session.checkpoint_state` – what the newest checkpoint request ended with,
ADR-093; HOT-24: `golden_switch_attempt` – the switch's starts for fresh checkpoints and failed attempts,
`golden_switch_request` – the switch requests it took over, both kept across restarts, and the columns
`context_session.impossible_reason`/`impossible_turns` – a session that can never be checkpointed on its VM, ADR-093
addendum HOT-24) and added columns are listed in
[`../spec-deviations.md`](../spec-deviations.md#02-data-model).

## 5. Guest journal (SQLite, `/var/lib/agent-guest/journal.db`, owner root (the `agent-guest-control` process), 0600; the unprivileged `agent-guest-api` process writes only through `api.sock`)

| Table | Columns | Notes |
|---|---|---|
| `journal` | `seq` INTEGER PK AUTOINCREMENT, `ts` text, `session_id`, `kind`, `body` text (JSON, plaintext) | append-only by code; rows acked by the runner (`POST /v1/journal/ack`) and older than 24 h are pruned (at start and every 10 min) |
| `session` | `id` PK, `uid`, `token_sha256`, `state`, `workspace`, `claude_session_id`, `created_at` | token compared constant-time against the hash |
| `prompt_registry` | (`session_id`,`sha256`) PK, `registered_at`, `used_at` NULL, `speaker` NULL | `speaker`: the sender line the runner registered with the prompt, returned by `prompt.check` with the acceptance (ADR-083 §1) |
| `pending` | `id` PK, `session_id`, `kind` CHECK in (`operation`,`query`), `request` text, `state`, `answer` text NULL, `created_at` | MCP calls waiting for the runner; a row unanswered for 1 h is removed (at start and every 10 min) with `guest.error pending_expired` of its session (KOR-02) |
| `ping` | `id` PK, `session_id`, `kind` CHECK in (`whiteboard.entry`,`subagent.finished`,`operation.finished`), `level` NULL, `ref`, `created_at`, `noticed_at` NULL, `delivered_at` NULL | content-free notices; `noticed_at` when a hook answer carried the notice, `delivered_at` when `get_notifications` returned it (RUN-14) |

Journal kinds: `session.state`, `session.output`, `session.tool`, `session.hook`, `session.file`,
`session.terminal`, `session.tools_ready`, `usage`, `operation.request`, `platform.query`, `ping.delivered`, `platform.read`, `platform.refused`, `metrics`, `guest.error`.
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
