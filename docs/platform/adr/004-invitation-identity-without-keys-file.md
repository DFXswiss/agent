# ADR-004: Identity by invitation code, passkey and membership log

Status: Accepted (2026-11-21)

## Context
- KONZEPT §1: users, customers and devices join only by invitation code; users set up a passkey; devices authenticate with a device key "similar to SSH, without SSH"; §2: start without configuration.
- Legacy: passkeys only for staff, each key added by a PR to `keys.yaml`, baked into the image, plus a GitHub account with the same handle (`PASSKEY-PROTOCOL.md`); devices pair by GitHub OAuth and receive an HMAC token.
- The passkey verification code in `agent_core.passkey` (webauthn, encoding, certificate) is pure and was reused unchanged in Spike D.

## Decision
- Identities are created by redeeming invitation codes (`04-crypto.md` §5.1–5.9). Users authenticate with passkeys (UV required) and per-login P-256 session keys (`agent-session-v1`); devices with Ed25519 signed requests (`agent-request-v1`).
- Who belongs where is defined by the per-org signed membership log and the platform log (ADR-023), stored in the database, verified by hub and clients. No `keys.yaml`, `teams.yaml`, PR or redeploy; no GitHub account required.
- Customers ("Kunden") are users invited with a platform invitation by holders of the platform role `inviter`; they receive a private organization. Org admins invite into their org.
- Reuse: only `agent_core.passkey.{errors,encoding,webauthn,certificate,relying_party}` and `agent_core.signing` helpers; legacy storage, routes and roster coupling are not used.

## Consequences
- Onboarding is self-service and immediate; the inviter need not be online.
- Rights changes take effect immediately (no deploy).
- RP ID equals the public host; it must be fixed before the first real passkey (domain change invalidates passkeys and PRF keys).

## Alternatives considered
- Keys file per PR (DESIGN §24.2): contradicts "ohne Konfiguration" and excludes customers without GitHub.
- GitHub OAuth login: third-party identity, plaintext token at the hub.

## Deviation from DESIGN.md
Yes: §5 and §7 (GitHub OAuth pairing, device token), §24.2 (keys file changed only by PR, no mailbox/code decides identity), §25.3 (device keys in keys file), §25.15 (joining by PR).
