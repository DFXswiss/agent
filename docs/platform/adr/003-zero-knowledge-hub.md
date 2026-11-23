# ADR-003: Zero-knowledge hub

Status: Accepted (2026-11-21)

## Context
- KONZEPT §5: confidential data are encrypted client-side; `agent-core` must not see confidential content in plaintext; decryption only by authorised users and execution devices; the execution device is trusted.
- The legacy hub stores every payload, terminal byte and even GitHub OAuth tokens in plaintext (`db.py`), and DESIGN.md builds on a readable replica.
- Spike D proved browser ↔ Python interoperability (WebCrypto ↔ pyca, byte-identical vectors, malicious-hub tests).

## Decision
- The hub stores ciphertext plus the exhaustive metadata list of `04-crypto.md` §7. Confidential = `00-overview.md` §3.
- Checks needing plaintext (operation parameters, repository rules, secret patterns) run on the runner; the hub checks only metadata, rights, signatures and opaque dedupe tags (`00-overview.md` §3.1).
- All outside-service calls on behalf of content (models, GitHub, mailbox) happen on the execution device.
- Operational counters (tokens, cost, context %, quotas, resource metrics) are hub-visible metadata by decision, needed for limits, monitoring and display (KONZEPT §6, §7).

## Consequences
- No server-side search, matching or rendering of content; lists show encrypted titles decrypted in the client.
- Restore of a client requires keys (passkey/PRF, recovery code or another key holder).
- Metadata and traffic patterns remain visible (documented in the threat model).
- Hub-served JavaScript is a residual risk (ADR-019).

## Alternatives considered
- Plaintext hub with TLS only (legacy): violates §5.
- Hub executes GitHub operations: requires plaintext parameters and tokens at the hub (Spike D §5 conflict).

## Deviation from DESIGN.md
Yes: §1 (teammates read via the hub), §2 hub role "full replica", §6 and §9 (server-side matchers/queries on payloads), §11 (SSE full replica), §13 (hub replicas as evidence), §24.2 (edge may read plaintext), §25.9 (session log held readable by the hub), §25.11 (device pages rendered from hub data).
