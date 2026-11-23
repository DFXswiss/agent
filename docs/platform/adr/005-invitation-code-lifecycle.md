# ADR-005: Invitation code lifecycle (KONZEPT §1 proposals)

Status: Accepted (2026-11-21)

## Context
KONZEPT §1 lists as "proposals, not yet decided": one-time codes, expiry times, revocation/unpairing. Spike D showed that the code never needs to reach the hub (only `invite_id` derived by HKDF) and that a malicious hub cannot substitute keys (`inv_sig`).

## Decision
All three proposals are adopted:
1. **Single use.** A code is consumed by the first valid `accept` (hub marks `used_at`; clients accept only one `accept` per `invite_id`, rule L7). Platform invitations are consumed in the `POST /v1/orgs` genesis transaction.
2. **Expiry.** Defaults: user invitations 72 h, device invitations 24 h; selectable up to 30 days. The hub refuses expired invitations (`invite_expired`); the lookup endpoint answers `invite_unknown` for unknown/used/revoked/expired codes alike.
3. **Revocation and unpairing.** Admins revoke open invitations (`invite_revoke` log entry; package deleted). Members are removed and devices unpaired with `remove` / `device_unassign`, always in one atomic batch with `rotate` of every affected scope and new wraps (`04-crypto.md` §4.7, §5.10). Platform devices are decertified in the platform log; affected orgs get `rotation_pending`.
- Codes: 128 bit, base32 `XXXXX-XXXXX-XXXXX-XXXXX-XXXXXX`, user links carry the code only in the URL fragment.

## Consequences
- An intercepted code is usable only once and only until expiry; the first redeemer wins and is visible in the member list and log.
- Removal costs one rotation per affected scope; content already read remains with the removed party (documented).

## Alternatives considered
- Reusable team codes: no accountability, unbounded exposure.
- Short codes with PAKE: unnecessary for 30-character codes typed once (Spike D).

## Deviation from DESIGN.md
None beyond ADR-004.
