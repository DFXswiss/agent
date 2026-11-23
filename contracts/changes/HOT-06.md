# HOT-06 – contract changes

ADR-027 section 3 (additive).

## `guest/journal/record.json#/$defs/body_session_file`: summary variant

A third `oneOf` branch `{path, op: "summary", files, deleted, bytes, paths, reason}` lets the guest's workspace
scanner report many changed files in one record instead of one record (with the full content as patch) per file:
a scan pass with more than the bulk bound of changed files (`reason: "bulk"`), or the part of a pass beyond the
session's per-window budget (`reason: "limit"`). `path` is the files' common directory (else the first path),
`paths` at most 20 of them (sorted), `bytes` the sum of the new sizes, `deleted` the number of deletions; no patch
and no digest. The event kind stays `session.file` (`events/session.file.json` references this body), so neither
the hub (ciphertext only) nor the runner (passes the body through) changes; a web client of the previous version
shows it as one "Changed `path`" line (unknown `op` falls back to `edit`). Fixtures: `valid/summary.json`,
`invalid/summary-with-patch.json`, `invalid/summary-without-paths.json`.

Platform imports (`files/import`, e.g. `github.checkout`) are no longer reported as `session.file` at all; that is
guest behaviour and needs no contract change.
