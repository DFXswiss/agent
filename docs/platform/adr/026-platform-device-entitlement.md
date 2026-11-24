# ADR-026: Entitlement to platform devices

Status: Accepted (2026-11-21); amended by ADR-078 (2026-11-24): an organization without an `org_platform_access` of
its own follows the root-signed switch `open_platform_access` (off until the owner decides; an explicit entry wins),
platform invitations come from every user while `open_platform_invites` is on, and a task waiting for platform access
asks the users with the platform role `operator`.

## Context
KONZEPT §3: by default DFX provides the execution servers; without own credentials and hardware the system uses
DFX servers and DFX model access (§4). Platform devices and the platform model budget are therefore a resource that
must be bound to an explicit grant. Without a rule, any invitation code or any existing account could create
unlimited private organizations and assign platform devices to them (finding of the contract review).

## Decision
- `POST /v1/passkeys/register/begin {invite_id}` binds the ceremony to the invitation; `register/finish` records
  `invite.registered_user`. One invitation registers at most one user. An org invitation can then be accepted only
  by that user (or by an existing user who never registered through it, `04-crypto.md` §5.2a). Registrations without
  a following `accept` or private genesis within 24 h are deleted by a hub job.
- A **private organization** can only be created with an unused platform invitation bound to the caller
  (`platform_invite_required`); one private org per platform invitation. Such orgs get `org.platform_access = true`.
- Any user may create **non-private organizations**; they get `platform_access = false` until the operator appends a
  root-signed platform-log entry `org_platform_access {org, granted}` (ADR-023). Revocation (`granted:false`) blocks
  new `device_assign` entries for platform devices; existing assignments stay until an admin unassigns them.
- The hub refuses `device_assign` with `platform_seq` for orgs without platform access (`platform_access_denied`);
  browsers show the reason on the device screen.
- Platform invitations come from users with the platform role `inviter` (`POST /v1/platform/invites`) or from the
  operator CLI (`user_invite`, bootstrap of the first users).

## Consequences
- Customers ("Kunden") start through a platform invitation and receive a private org with platform devices.
- Orgs created later by the same user use their own devices unless DFX grants platform access.
- The platform budget (`AGENT_HUB_PLATFORM_BUDGET_USD_PER_ORG_DAY`) applies per org with platform access.

## Alternatives considered
Platform access for every org (unbounded cost and capacity use); manual approval of every private org (breaks the
zero-configuration entry of KONZEPT Grundprinzip).

## Deviation from DESIGN.md
None beyond ADR-002/ADR-004.
