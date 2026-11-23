# ADR-044: Session tokens are bound to the UID of the connecting process

Status: Accepted (2026-11-22). Numbered ADR-043 in the agent repository until DOC-01; the decisions of ATS-02 and
`atk/M2.md` written before then cite that number. ADR-043 is the invitation proof of KOR-01, accepted in parallel.

## Context
Every session of a VM authenticates to the guest API on `127.0.0.1:7700` (`/mcp`, `/hooks/<Event>`, `/model/…`) with
its bearer token (05-runner.md §4.3, §4.4, §10.2). The token is compared in constant time against the stored hashes
(§4.6 no. 1), but nothing else tied it to its session: any process holding a token could use it, failed checks were
neither limited nor journaled. ATK-36 (07-testing.md §2.3) requires brute force and foreign tokens on all three
surfaces to be "constant-time, rate-limited, journaled". All sessions share the loopback address, so the address
cannot tell callers apart, and limiting failures globally would let one session lock out every other one.

## Decision
- The API process looks up the owner of the client socket of each connection in the kernel's TCP table
  (`/proc/net/tcp`, configured by `AGENT_GUEST_PEER_TABLE` in `agent-guest-api.service`; `agent_guest/peer.py`) and
  passes that UID with the token hash to the control process (`session.resolve {token_sha256, peer_uid}`). Binding on
  and no entry found means UID -1: fail-closed.
- The control process accepts a token only when its session's user is the connecting UID. Every failed check
  (missing, unknown, inactive or foreign token) is counted per connecting UID (`agent_guest/authguard.py`): up to 10
  per 60 s are answered `401` and journaled `guest.error auth_failed` with the session of that UID; then
  `auth_rate_limited` is journaled once and further failures are answered `429` (`Retry-After`) without records until
  the window has room. A valid token of the connecting UID is never limited.
- Contracts: additive codes `auth_failed`, `auth_rate_limited` (`guest/journal/record.json`) and `rate_limited`
  (`guest/error.response.json`), `contracts/changes/ATS-02.md` in agent-core.

## Consequences
A token read by another session is useless; guessing from one session can never reach another session's token, and
it costs at most 10 journal records per minute. A misbehaving session limits only its own failed attempts. The
binding needs Linux; tests and other systems leave the table unset and keep the token check alone.

## Alternatives considered
Global failure limit (one session locks out all); per-connection limits (a new connection resets them); unix sockets
with `SO_PEERCRED` (Claude Code reaches MCP, hooks and the gateway only over HTTP URLs); tokens alone (status quo:
no binding, no limit, no journal).

## Deviation from DESIGN.md
None beyond ADR-001.
