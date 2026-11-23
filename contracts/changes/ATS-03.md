# ATS-03: Ed25519 boundary vectors

Additive (ADR-027 §3; AUD-01 B-11): three negative vectors in `vectors/keys.json` `invalid`, op `ed25519_verify`,
error `signature_invalid`:

- `ed25519_noncanonical_s` - a valid signature with `S + L` (non-canonical `S`);
- `ed25519_small_order_key` - public key = the identity point, `R` = identity, `S = 0` (verifies for any message in
  a cofactorless check without the small-order refusal);
- `ed25519_small_order_key_order8` - a public key of order 8 with the same signature.

The reference (`agent_hub.crypto.keys.ed25519_verify`), the web core and the runner's copy refuse all three (ATS-03).
