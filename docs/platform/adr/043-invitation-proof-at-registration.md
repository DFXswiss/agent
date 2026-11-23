# ADR-043: Registration through an invitation requires a proof of the code (`inv_sig`)

Status: Accepted (2026-11-22)

## Context
`register/begin {invite_id}` and `register/finish` (03-api.md §4.1, 04-crypto.md §5.1) bound a new user to an open
invitation by its `invite_id` alone. The `invite_id` is not secret to everyone but the invitee:

- a platform user invitation of the operator stands as `user_invite {invite_id, expires}` in the platform log
  (04-crypto.md §4.8), which every signed-in user may read (`GET /v1/platform/log`, right `user`);
- inviters see the IDs of all platform user invitations (`GET /v1/platform/invites`);
- an org invitation stands as `invite {invite_id, pk_inv, …}` in the membership log, readable by every member.

Any of these readers could register through an open invitation before the invitee did. For a platform invitation that
is a full takeover: the registered user may found a private org with platform access (§5.3), and the invitee is left
with `invite_unknown` (AUD-01 A-02, HUB-04 decision 31). For an org invitation the `accept` entry still needs `inv_sig`
under `pk_inv`, so a member could only block the invitee until the 24-hour orphan job frees the invitation.

The code already yields an invitation signing key (`sk_inv = Ed25519(HKDF(code, P‖"invite-sign"))`, §3); org
invitations and platform device invitations publish its public key `pk_inv` and use it to prove possession of the code
(`accept.inv_sig`, enrollment `inv_sig`). Platform user invitations did not carry it.

## Decision
1. **Every invitation the hub registers users through has a `pk_inv`.** The platform-log entry `user_invite` gets the
   field `pk_inv` (32-byte Ed25519 public key, b64u), like `device_invite`; the operator CLI
   (`agent-hub platform invite-user`) writes it. An inviter's `POST /v1/platform/invites` carries `pk_inv` (a key that
   is no valid Ed25519 point is `400 params_invalid`). The hub keeps the key of platform invitations in
   `invite.pk_inv` (migration 0007); for an org invitation it reads `pk_inv` from the stored `invite` entry.
2. **`register/finish` of a first passkey requires `inv_sig`**:
   `inv_sig = Ed25519_{sk_inv}(P ‖ "invite-register\0" ‖ user_id ‖ "\0" ‖ challenge)` over the raw 32-byte challenge
   of the registration ceremony and the user ID `register/begin` allocated. The hub verifies it under the invitation's
   `pk_inv` before it stores anything; a wrong or foreign proof is `401 signature_invalid` and consumes the ceremony
   (one attempt per challenge, like every other refusal after `take_ceremony`). An invitation without a known key
   (a platform user invitation created before this ADR) is `404 invite_unknown`. The rule is the same for platform and
   org invitations, so the browser has one registration path.
3. **`register/begin` stays bound to `invite_id`.** It allocates a user ID and a challenge and binds nothing; only
   `finish` binds the invitation (`registered_user`), and only with the proof. A proof at `begin` would need a second
   round trip without adding protection.
4. The new label `invite-register\0` joins the signature labels of 04-crypto.md §3. Vectors: `contracts/vectors/
   invite.json` (`register`, negatives `register_*`) and every platform-log vector file (the `user_invite` entries
   carry `pk_inv`); all three implementations (hub reference, runner copy, web core) pass them.

## Consequences
- Knowing an `invite_id` no longer allows registering; the invitee alone (the holder of the code) can.
- The platform-log format changes (`user_invite.pk_inv` is required): logs written before this change no longer
  verify. The platform runs only on localhost test hubs so far; there is no deployed log to migrate.
- The runner's copy of the reference crypto (`agent_runner.crypto`, ADR-027 §2) is refreshed with the same change.
- Documentation (03-api.md §4.1/§4.2, 04-crypto.md §3/§4.8/§5.1) is to be aligned by DOC-01.

## Alternatives considered
- `H(invite_id)` instead of `invite_id` in the platform log: closes only the platform-log leak, not the inviter listing
  or the membership log, and still binds the registration to a value that is not a proof of the code.
- Proof at `register/begin` over a hub nonce: an extra round trip; the binding happens at `finish` anyway.
- Hiding the platform log from ordinary users: the log is the root of trust that browsers verify (ADR-023, ADR-026).

## Deviation from DESIGN.md
None beyond ADR-004.
