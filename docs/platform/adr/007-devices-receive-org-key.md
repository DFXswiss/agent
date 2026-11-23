# ADR-007: Execution devices receive the org key

Status: Accepted (2026-11-21)

## Context
Spike D recommended that devices receive only workgroup keys, never the org key, to limit exposure. KONZEPT §8 lets sessions read members, workgroups and whiteboards within their rights; §12 defines an org-level whiteboard. Org-level names (workgroups, members, devices) and the org whiteboard are encrypted under OK.

## Decision
A device assigned to at least one workgroup of an organization receives the current OK of that organization (in the device invitation package, in `device_assign` batches and via catch-up). It never receives WK of workgroups it is not assigned to.

## Consequences
- Sessions can read the org whiteboard, workgroup names and member names (ADR-024).
- A compromised device exposes org-level content (org profile, names, org whiteboard) of every org it serves, in addition to its workgroups' content. Mitigation: unassignment rotates OK as well (`04-crypto.md` §5.10); per-workgroup device assignment is recommended for sensitive orgs.

## Alternatives considered
- Spike D design (no OK on devices) with a browser relay of org data: sessions could not read org-level data while no browser is online.
- Copying org-level data into each workgroup scope: duplicate writes, inconsistent history.

## Deviation from DESIGN.md
None.
