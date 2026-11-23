# ADR-012: Operations, formal checks and idempotency (KONZEPT §11, §14.5)

Status: Accepted (2026-11-21)

## Context
- KONZEPT §10/§11: only explicitly defined actions; deterministic, formal (not content) checks; every operation recorded with ID, task/session, type, parameters, timestamp, status and result; duplicate detection by deterministic rules (e.g. the same PR comment twice); idempotency key optional; retry rules per type; rejected and deduplicated requests audited.
- Zero-knowledge hub (ADR-003): the hub cannot read parameters. Spike B prototype hashed parameters with non-canonical JSON and accepted extra parameters.
- Legacy pattern: `request_reservation` (fingerprint, lease, marker recovery) in `agent-core`.

## Decision
- **Register-then-execute**: session → guest (schema) → runner (formal checks on plaintext, dedupe tag) → hub (`POST /v1/device/operations`, checks H1–H6) → runner executes only after `accepted` → encrypted result + transition. Every request, including locally rejected ones, gets a hub record and audit row.
- **Dedupe tag** = HMAC-SHA256 keyed with `dk_e = HKDF(WK_e, "dedupe")` over canonical `{type, fields}` (`04-crypto.md` §4.10); the hub compares tags only. Per-type fields and windows in `03-api.md` §9 (e.g. comment: repo, number, sha256(body), 24 h; PR: repo, head, base, 7 days + open-PR check at GitHub; subagent: 10 min).
- **Idempotency key** adopted as the optional mechanism: if given, fields = `{idempotency_key, task}`.
- Duplicate = slot held by an operation in `accepted|executing|succeeded` within the window; the duplicate returns the original result. Failed/rejected/expired operations release the slot.
- **Retries** inside one operation for retryable errors: 3 attempts (2 s, 8 s, 30 s, honouring `Retry-After`).
- Parameter schemas with `additionalProperties:false`, booleans never accepted as integers.
- No operation type executes code (no `run_tests`, no build) in v1.

## Consequences
- Cross-device duplicate protection without plaintext at the hub.
- A key rotation inside a window allows one duplicate (documented).
- Operations need the hub online (expire after 10 min in `received`).

## Alternatives considered
Hub-side parameter hashing (needs plaintext), device-local dedupe only (misses other devices), unrestricted execution operations (Spike B: models tried them as exec side channel).

## Deviation from DESIGN.md
Partly: DESIGN §19.3 intent/executor idempotency is kept in spirit; the hub deciding operations is covered by ADR-002.
