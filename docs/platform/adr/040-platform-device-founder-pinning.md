# ADR-040: Platform devices pin org founders on first use

Status: Accepted (2026-11-21)

## Context
04-crypto.md §6.1 L3 verifies a membership log against "the pinned founder key (from the package, from the org
creation or from local storage)". Browsers get it from the org creation or the invitation package, org devices from
the package (§5.8). Platform devices have no package (§4.8, §5.9): an admin assigns them with `device_assign` +
`platform_seq` and wraps keys for them, and they learn of the org only from `GET /v1/device/state`. The specification
names no source of the founder key for them, while §4.8 requires them to verify against the root key "before
trusting anything" and 00-overview.md §3 (B2) says the hub cannot inject.

## Decision
A platform device pins the founder key on first use: the genesis key of the first verified copy of an org. The copy
is accepted (and only then stored) when all of the following hold:

1. the whole copy verifies (L1-L8) against that genesis key and the device's verified platform log;
2. it makes this device a member through **its own current certification** (`device_assign` with `platform_seq` =
   the `device_certify` of this device's keys in the verified platform log);
3. the verified platform log gives the org platform access (ADR-026: a private org, or an `org_platform_access`
   entry granting it), the same rule the hub enforces at `device_assign`;
4. none of its workgroup IDs, its org ID or a principal ID with other keys collides with another verified copy
   (IDs are chosen by clients; a second copy must not claim a workgroup, the scope keys or the identity of a real
   org). A clashing entry of an existing copy is refused like any other invalid entry.

From then on L9 (the stored head must be extended) protects the copy, as for every other client.

## Consequences
- A compromised hub can present an invented private org (with its own founder) that assigns the device; the device
  then serves that org's tasks with its own keys and model access. It cannot read, forge or take over data of real
  orgs: their logs are pinned once seen, their IDs cannot be reused, and the platform log (root-signed) cannot be
  forged. A first-use window remains for a real org the device has never seen. This residual risk is documented in
  the threat model addendum below.
- Threat model addendum (04-crypto.md §9, "Active hub"): residual risk "invent an org that uses a platform device".

## Alternatives considered
Founder keys in the platform log (needs an operator step per org and a change of the hub's org creation), the
assigning admin's identity in the wrap metadata (still unanchored for the device), trusting the hub (contradicts B2).

## Deviation from DESIGN.md
None beyond ADR-023.
