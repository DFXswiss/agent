# ADR-006: Passkey-based key management (KONZEPT §14.1)

Status: Accepted (2026-11-21)

## Context
- KONZEPT §5: a passkey login does not automatically yield an encryption key; derivation, sharing and device distribution are open (§14.1).
- Spike D: PRF works with the Chromium virtual authenticator (32 stable bytes per credential, also at `create()`); exported passkeys lose PRF; HPKE implementation byte-identical to pyca `hpke.Suite`; 21 key-flow tests incl. a malicious hub; 35 pytest + 9 Playwright tests at 100 % coverage.

## Decision
The hierarchy and formats of `04-crypto.md`:
- User identity = X25519 (`kem`) + Ed25519 (`sig`) in an **identity bundle**, stored at the hub only encrypted, one slot per passkey (`KEK_i` from PRF) and one **mandatory recovery slot** (128-bit recovery code, confirmed by the user at registration, replaced after every use).
- Without PRF: recovery-code unlock plus an optional non-extractable IndexedDB device key; detection per login (`prf_missing`).
- Scope keys with epochs: org key (OK), workgroup key (WK), per-user credential key (CK); random, never derived from each other; distributed as HPKE wraps (RFC 9180, info binds scope/scope_id/epoch/recipient), wraps signed by the wrapper.
- Events: AES-256-GCM with a per-event HKDF key (random 32-byte salt), canonical header as AAD, Ed25519 author signature, `lh` (log head) for historical write-right checks.
- Membership is a signed, hash-chained per-org log verified by every client against the founder key; rollback/fork detection via stored heads and `lh`.

## Consequences
- One user gesture logs in, certifies the session key and unlocks the identity.
- Loss of all passkeys and the recovery code loses the personal identity; org data survive through other holders; a single-member private org is lost (UI warns).
- Open risk: PRF support on real platforms (iCloud Keychain, Google Password Manager, Windows Hello, 1Password, Bitwarden, security keys) is not verified; a manual device matrix is part of M3. The more platforms lack PRF, the more users depend on the recovery code.

## Alternatives considered
- Password-derived keys: weak, extra secret.
- Server-held escrow keys: violates §5.
- MLS (RFC 9420): unnecessary at expected group sizes.

## Deviation from DESIGN.md
None beyond ADR-003/004.
