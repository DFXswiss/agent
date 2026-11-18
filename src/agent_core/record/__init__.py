"""The team record (RECORD-PROTOCOL.md).

Change 1 of RECORD-PROTOCOL.md section 16: the event text, the event hash, the chain of a key and
the record hash (section 4, `event`), the form of a connection string (section 15, `connection`),
the schema (section 11.3) and the command `python -m agent_core.record migrate` (section 11.4).

Change 3, its pure part, without database and HTTP: the objects (section 5, `objects`), the state
(`state`), the reducer (section 6, `reducer`), visibility (section 3, `visibility`), the release
rule (section 9, `release`), checkpoints (section 12.2, `checkpoint`), the fold and verification
(sections 6.1 and 12.1, `verify`) and the export file (section 12.3, `export`).

Change 3, its storage part, without HTTP: the record in PostgreSQL (sections 11.4 to 11.6, 12.1 and
12.2, `storage`) and the commands `export`, `verify` and `restore` (section 12.3, `commands`).

Change 3, the record in the hub: the start of section 11.4, the state `record_unverified`, the
periodic verification and the checkpoint attempts of one hub process (`runtime`), and the endpoints
under `/api/key/record/` with their request line (sections 7, 8 and 13, `endpoints`). The release
query of `GET /api/key/release` computes its answer by the rule of section 9 (`release`).
"""
