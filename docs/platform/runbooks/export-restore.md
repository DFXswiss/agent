# Runbook: export, restore, projection rebuild, runner store wipe (DR-01)

Scope: disaster recovery of the hub database per organization and recovery of a runner device's store
([02 §1](../spec/02-data-model.md#1-principles) projections, [02 §3](../spec/02-data-model.md#3-event-log-format)
export format, [02 §4](../spec/02-data-model.md#4-runner-store-postgresql-schema-runner-local-cluster) runner identity,
WP-M3-05, E2E-33).

**Status:** the commands `agent-hub export`, `agent-hub restore` and `agent-hub rebuild-projections` are part of the
hub (`src/agent_hub/backup/`, DR-01, decisions `docs/platform/decisions/DR-01.md`). E2E-33
(`e2e/platform/specs/e2e-33-export-restore.spec.ts`) runs this procedure end to end and is the evidence
that it works; its runs are recorded in the milestone acceptance ([M2](../acceptance/M2.md) onwards).

## 1. What an export contains

JSON Lines `agent-hub-export-v1` of one organization: header line, membership-log entries, the platform-log
reference, wraps, events with `scope_seq` and `received_at`, credential metadata and records of the members,
task/session/operation metadata – **ciphertext and the metadata of 04 §7 only, never plaintext**. Besides the
org's own rows, **every export holds the hub-wide data complete**: the platform log, the platform roles, the platform
invitations and platform devices, the request nonces, the rollup cursor and the whole audit chain with its
checkpoints – including the audit rows and platform-log entries of every other organization (both are single hash chains; a part could neither be
verified nor continued, DR-01 decision 1). An org export is therefore an operator backup, not a data takeout: never
hand it to an organization's members. The file is sensitive (metadata of all orgs, member IDs): store it encrypted
with the same care and the same access as a database backup.

## 2. Export (regularly, and before every risky change)

`agent-hub export --org <org id> --out <file>` with the hub's configuration (writer role, hub signing key). The file is
written with mode 0600 under a temporary name and renamed when complete; an existing file is never overwritten.
Output `exported <rows>`. Exit codes: 0 ok, 1 refused, 2 configuration error, 3 database unreachable.
Export every organization (one file each) and keep a database dump of the whole `agent_hub` database beside it
(the dump is the primary backup; the export is the verified, per-org portable form).

## 3. Restore

Only into an **empty, migrated** database:

1. Stop the hub ([hub-operation.md](hub-operation.md): one process).
2. Create the database, run `agent-hub migrate`.
3. `AGENT_HUB_OWNER_DATABASE=<owner DSN> agent-hub restore --in <file> [--in <file> …]` with the hub configuration.
   The restore checks the chains, hashes and the audit chain before inserting; append-only tables are written with
   the owner role only during the restore. Output `restored <rows>` or `failed <step>`; a non-empty target database
   is refused.
4. `agent-hub rebuild-projections --check` – one JSON line; `differences` must be empty.
5. `agent-hub audit verify` – exit 0.
6. Start the hub. Browsers and runners reconnect with their stored cursors; a browser that saw a newer log than the
   restored one stops with `log_rollback` for that org – this is correct behaviour (data newer than the export are
   lost) and needs a decision of the org's admins, not a database edit.
7. Runners whose outbox ran ahead of the export: a runner prunes acknowledged outbox items after one hour. If the
   restored hub asks it for an item it no longer holds (`origin_seq_gap` with `expected` below its stored range), the
   runner confirms the hub's position (`GET /v1/device/state`), renumbers its remaining items to follow it and reports
   the loss (`runner.alert` `outbox_items_lost` with the count, as a log line and as an audit row); it keeps pushing.
   The lost items are the ones the export does not contain and the device no longer holds. No operator step is
   needed, but every such alert after a restore is an expected data loss to note in the incident record. While the
   hub's position is not confirmed the runner retries the push (log `gap_unconfirmed`); only a stored item that can
   no longer be decrypted during the renumbering stops the sync (`sync_fatal`, `outbox_corrupt`) – then wipe the
   store (§5). With an export older than an hour, runner events pushed after the export can be lost this way;
   restore from the newest export.

## 4. Projection rebuild (without restore)

`agent-hub rebuild-projections` recomputes every registered projection (`<area>/projections.py:REBUILDERS`) from its
append-only sources; `--check` only compares (exit 1 on differences). Use it after a suspected projection bug; it
never changes source rows.

## 5. Runner store wipe

The runner identity lives outside the database (`device.json`, `keys/device.key`), so the store can be rebuilt:

1. `agent-runner uninstall --keep-identity` (stops the service, deletes VMs and the store, keeps the identity), or
   delete only the store directory with the daemon stopped.
2. Install the service again ([runner-installation.md](runner-installation.md)). The runner fetches its state: the
   outbox continues at `last_origin_seq + 1` from `GET /v1/device/state`, prompts in `delivered_prompt_event_ids`
   are not delivered again, scope keys are fetched again as wraps, workgroup VMs are recreated.
3. `agent-runner doctor` – no `sync_fatal`. An `origin_seq_conflict` means the store was not empty or was restored
   from an old copy: never restore a runner store from a backup, wipe it instead.

Local data that only the runner held (operations' plaintext parameters, local journals) are gone after a wipe;
running sessions end and their tasks continue with a fresh session.
