# HUB-01 – Hub database: migrations, roles, append-only, audit chain, migrate CLI

Decisions taken where `02-data-model.md` sections 1, 2 and 6 were silent or open.

1. **Migration files are composed by the driver.** `0001_hub.sql` names the database and the writer role as
   `{database}` and `{writer}`; `agent_hub.db.migrate.compose` binds them as identifiers with `psycopg.sql`, so every
   literal brace in a migration is doubled (`{{64}}`). The checksum is the SHA-256 of the file bytes before
   composition. Later migrations take the next number (ADR-027 §5) and grant their own new tables to `{writer}`.
2. **One transaction per run.** All pending files and their `hub.schema_version` rows are applied in the transaction
   that holds the advisory lock `742180100`; a failing file rolls back every pending file of the run.
3. **Output.** `applied <n>` / `unchanged <n>` name the schema version after the run (as the legacy `migrated
   <version>`). Failure steps: `config` (variables missing or malformed, same role for owner and writer, different
   databases, libpq < 16), `files` (package files not a gapless `NNNN_<name>.sql` series or not UTF-8), `owner`
   (superuser, a role that does not own the database – this is how the writer role is refused when its credentials
   are given as owner –, connection failure), `lock` (lock timeout, SQLSTATE 55P03, also during a statement),
   `checksum` (= `migration_checksum_mismatch`: an applied version is unknown to the code or its name or checksum
   differ), `migrate` (a statement failed or the writer role is missing). Success goes to stdout, failure to stderr.
4. **Configuration.** `migrate` reads `AGENT_HUB_OWNER_DATABASE` and `AGENT_HUB_DATABASE` (it needs the writer role
   for the grants) in the strict connection-string form of the reused `agent_core.record.connection`
   (`postgresql://<role>:<64 hex>@<host>[:<port>]/<db>?sslmode=…`). HUB-02's `config.py` may reuse the same parser.
5. **Nullability.** Columns without `NULL` in section 2 are `NOT NULL`, except where the value cannot exist yet or
   has no principal: `operation.decided_at/started_at/finished_at`; `task_transition.reason/by_id`,
   `session_transition.reason/reported_by`, `operation_transition.by_id` (hub-derived transitions); `invite.created_by`
   (platform invitations from the operator CLI are root-signed, no principal); the metric columns of `metric_sample`
   and the IDs/targets of `audit` (nullable in `contracts/api/objects.json` and `org-audit.response.json`).
6. **`metric_rollup` columns** (section 2.8 only says "avg and max for gauges, last value for counters"):
   `samples`; `<gauge>_avg` and `<gauge>_max` for `cpu_ratio`, `mem_used_bytes`, `mem_limit_bytes`,
   `disk_used_bytes`, `disk_limit_bytes`, `free_vm_slots`, `free_mem_bytes`; last value for the counters
   `net_rx_bytes`, `net_tx_bytes`, `uptime_seconds` and the status `reachable`, `state`. `ts` is a multiple of 300 s.
7. **Rights.** Writer: `SELECT, INSERT` on the eight [AO] tables, `SELECT, INSERT, UPDATE, DELETE` on the others,
   **only `SELECT` on `hub.schema_version`** – a deliberate tightening of section 1.4 (the writer must not fake or
   remove an applied migration); later packages must not "repair" it. PUBLIC has no right on
   the database, the schema, any table or `hub.refuse_change()`.
8. **Constraints beyond section 2.** Foreign keys to `principal` where section 2 draws them (`user_identity`,
   `device`) plus, added by this package, `user_settings` (settings exist only for a principal); no others, so that
   later packages and DR-01 restore are free in insert order. `invite.registered_user` is UNIQUE (added: one
   registration belongs to exactly one invitation). Added CHECKs mirror the
   contracts and invariants: key and hash lengths, `session.` key IDs, `vm-<8 hex>`, depth ⇔ parent, closed ⇔
   `closed_at`, `duplicate` ⇔ `duplicate_of`, whiteboard level ⇔ IDs, device kind `org` ⇔ `owner_org`, one default
   workgroup per org (partial unique index), audit `at`/`published_at` in whole milliseconds, `prev_hash` NULL exactly
   at `seq = 1`. Indexes for the newest-first lists of section 1.5 (`created_at DESC, id DESC`).
9. **`quota_snapshot."window"`** keeps the specified name; it is a reserved word and is always quoted.
10. **Audit hash.** "canonical(row without hash fields)" is the canonical JSON (04-crypto §1) of exactly the API row of
    `contracts/api/org-audit.response.json` (keys `seq` … `event_id`, IDs as lower-case text, `at` as
    `YYYY-MM-DDTHH:MM:SS.mmmZ`); `prev_hash` is stored NULL for `seq = 1` and hashed as `-`. `append_audit` normalises
    `at` to UTC milliseconds. `detail` is validated structurally (flat object, keys `[a-z][a-z0-9_]{0,63}`, at most 32,
    values printable ASCII ≤ 256, safe integers, booleans, null) and keys that would name plaintext (`title`,
    `prompt`, `body`, `text`, `message`, `url`, `path`, `repo`, `branch`, …) are refused; per-action validation against
    `contracts/audit/` follows when CTR-02 is merged. `action` must be in the closed list; because `contracts/` is not
    part of the installed package, the list is copied to `agent_hub/audit/actions.py` and a test compares it with
    `contracts/enums/audit-actions.json` (a contract change of the list must regenerate it). IDs must be UUIDv7.
    `append_audit` refuses REPEATABLE READ/SERIALIZABLE transactions (a snapshot older than the lock would read a
    stale head). `verify_rows` reports a row that cannot even be hashed as broken instead of raising;
    `verify_chain` with a `to_seq` beyond the head reports the missing tail as broken.
11. **Checkpoints.** Text `agent-audit-head-v1\n<key>\n<seq>\n<hash>\n<published_at>` with `published_at` in the
    millisecond API format (the verifier rebuilds the text from the `org-audit-verify` response); Ed25519 with the
    reused `HubSigner`, signature stored as 64 raw bytes. `write_checkpoint` writes nothing for an empty audit or when
    the newest checkpoint of the key already covers the head; a concurrent duplicate is dropped (`ON CONFLICT DO
    NOTHING`). Scheduling stays with HUB-16.
12. **Pool sessions** run `SET ROLE NONE`, `search_path = pg_catalog, hub, pg_temp` and `TIME ZONE 'UTC'`; a returned
    connection is reset (`RESET ALL` + the same statements) and checked before it is lent again.
13. **CLI registration convention** (until HUB-02 adds the discovery): `register(subparsers)` adds a subparser and sets
    `handler`, a callable `(argparse.Namespace) -> int` exit code. `python -m agent_hub.db migrate` is the interim entry.
14. **Package data.** `pyproject.toml` ships `agent_hub/db/migrations/*.sql` (separate commit), so a non-editable install
    can migrate.
15. **Test database URLs** use `sslmode=disable` for loopback hosts and `require` otherwise (legacy rule).
16. **PostgreSQL 16.** The suite also ran on PostgreSQL 16 on the runner device (`remote.sh … --pg`); the remote copy has
    no `.git`, so only `test_legacy_untouched` cannot run there (it needs `git merge-base`).
