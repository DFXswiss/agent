# ADR-046: Every hub column is classified against the list of what the hub sees

Status: Accepted (2026-11-22)

## Context
- `04-crypto.md` section 7 lists exhaustively what the hub sees in plaintext; everything confidential (`00-overview.md`
  section 3) reaches it only as ciphertext (ADR-003). E2E-09 has to show "no deviation from section 7" for the real
  database (SCN-09).
- The schema of `02-data-model.md` section 2 has 410 columns in 48 tables. Most fall literally under a row of
  section 7; some hold metadata that section 7 names only by its purpose (scheduling, authentication, routing) or not at
  all (the hub's own migration bookkeeping).

## Decision
- `e2e/platform/checks/hub_columns.py` maps every column onto one row of section 7 (`ids`, `logs`, `auth`, `events`,
  `tasks`, `operations`, `metrics`, `notifications`, `audit`, `credentials`, `settings`), onto `ciphertext` (sealed
  under keys the hub never holds) or onto `internal`. E2E-09 compares the live schema with the map in both directions,
  the SQL type with the content type, and every text-like metadata value (strings and JSON object keys) with a token
  pattern without white space or non-ASCII letters; a unit test keeps the map equal to the migrations, so a new column
  needs a classification before it can merge.
- Columns that section 7 does not name literally, with their row:
  - `device.os`, `device.arch`, `device.agent_version`, `device.capacity`, `vm.cpus`, `vm.memory_mib`, `vm.disk_gib`
    → **metrics**: capacity and platform facts of an execution device, the scheduler's input (`03-api.md`
    section 4.6.2) and shown on the device page; no user content.
  - `session.agent_kind`, `session.agent_version`, `session.estimate_ms`, `vm.golden_version` → **tasks** (session
    state: which agent software runs, the countdown estimate).
  - `org.private`, `org.platform_access`, `org.rotation_pending`, `wg.is_default` → **logs** (projections of the signed
    membership and platform logs).
  - `device_request.*` / `device_response.*` routing fields (kind, state, IDs, times) → **operations** (a user-initiated
    platform action with type and state); the envelopes → **ciphertext** (HPKE to the device or the user).
  - `ceremony.*`, `request_nonce.*`, `browser_session.statement`, `passkey.transports`, `user_identity.recovery_id`
    → **auth** (challenges, replay nonces, the signed session statement, authenticator transports, the identifier
    derived from the recovery code - never the code).
  - `whiteboard_entry.level`, `whiteboard_entry.deleted` and the event references → **events** (header facts of the
    entry's events); `whiteboard_entry.notify` → **notifications**.
  - `schema_version.*` → **internal**: names and checksums of the hub's own migration files; no data about users,
    content or devices, so outside the scope of section 7.
- Values that fail the token check are counted, never copied into the report (the report is an artefact).

## Consequences
- Section 7 stays the binding list; this ADR records how its rows cover the concrete schema. A later edit of section 7
  may name these columns literally; until then a column that fits no row (or only `internal`) is a deviation that
  E2E-09 and the unit test report.
- The token check excludes free text with spaces or non-ASCII letters from metadata columns; a single ASCII word would
  pass, so the canary scan of E2E-09 remains the check for content.

## Alternatives considered
- Amending `04-crypto.md` section 7 in this package: the specification is owned by the planning documents, not by a
  scenario package; the classification is recorded here instead and can be folded in by a later specification update.
- A schema review by hand: not repeatable; a new column could slip in unclassified.

## Deviation from DESIGN.md
None beyond ADR-003.
