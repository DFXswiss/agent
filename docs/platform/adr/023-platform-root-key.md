# ADR-023: Offline platform root key

Status: Accepted (2026-11-21)

## Context
Private organizations get DFX platform devices assigned automatically (KONZEPT §3); the browser then wraps workgroup keys for that device. If the hub could vouch for device keys, a compromised hub could register a rogue device and receive keys. Customers are invited by DFX staff (ADR-004), which needs a platform-level role.

## Decision
- An Ed25519 **platform root key** is created offline with `agent-hub platform init` and kept by operators outside the hub.
- It signs the **platform log** (`04-crypto.md` §4.8): `root`, `device_invite`, `device_certify`, `device_decertify`, `role_grant`/`role_revoke` (role `inviter`), `user_invite` (bootstrap platform user invitations, `agent-hub platform invite-user`), `org_platform_access` (ADR-026).
- The operator CLI talks to the hub with root-signed requests (`agent-request-v1`, key ID `platform-root`).
- The root public key is compiled into the web build and configured in hub and runner; mismatch with `/v1/config` is fatal (`platform_root_mismatch`).
- Platform device enrollment is two-step: device enrolls with an invitation code (`pending_certification`), the operator certifies with `agent-hub platform certify-pending`.
- The hub enforces the platform log but cannot extend it.

## Consequences
- A compromised hub cannot inject platform devices or inviters.
- Operator CLI steps are needed to add platform devices and inviters; loss of the root key blocks new certifications (re-keying needs a new build; documented in the runbook).

## Alternatives considered
Hub-held platform key (single point of compromise), certification by each org admin (not possible for private orgs of new customers).

## Deviation from DESIGN.md
None beyond ADR-004.
