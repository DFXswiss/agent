# Decisions of ATS-03 (Ed25519 boundary vectors, audit item B-11)

1. **Small-order public keys are refused before the library.** What: `agent_hub.crypto.keys.ed25519_verify` and
   the TypeScript core `ed25519Verify` (`web/src/core/crypto/keys.ts`) refuse a 32-byte key whose encoding, with the
   sign bit (top bit of byte 31) cleared, is one of the 7 entries of the libsodium blacklist (the 8 torsion points
   plus the non-canonical y = p and y = p + 1). Why: OpenSSL (pyca `cryptography`, and WebCrypto implementations built
   on it) accepts `R = key, S = 0` under such a key for every message, so a small-order identity, device, hub or
   platform-root key would make every signature forgeable. The refusal is `signature_invalid`, the same code and path
   as a wrong signature, so callers and the error contract of `04-crypto.md` §8 are unchanged.
2. **Non-canonical `S` is refused explicitly.** What: both implementations refuse a signature whose little-endian
   `S` (bytes 32..63) is at least the group order `L`. Why: the libraries already refuse it; the explicit check is
   defence in depth and makes Python and TypeScript independent of library versions. Node 26 WebCrypto already
   rejects the identity-key forgery in the Vitest runs; browsers differ, so the check stays in the TypeScript core.
3. **Every Ed25519 verification of `agent_hub` goes through the hardened function.** Where: the signature gate
   (`agent_hub.auth.keys.ed25519_verifier`: device keys, the platform root key, enrolment proof of possession), the
   audit checkpoint check (`agent_hub.audit.checkpoint.checkpoint_signature_valid`) and the operator CLI's check of
   hub response signatures (`agent_hub.platform.cli.Operator`, which now pins the raw 32-byte hub key). The crypto
   formats (`signed`, `log`, `invite`, ...) already used `ed25519_verify`. `ed25519_verifier` keeps its eager
   `ValueError` for a malformed key. The legacy package `agent_core` is outside the platform code and unchanged.
4. **R is not checked.** Only the key and `S` are checked, as the audit item asks; a small-order `R` under a
   prime-order key cannot produce a forgery without the private key.
5. **Contract vectors follow separately.** The negative vectors (`keys.json` `invalid`) are added in a second, additive
   contract change, after the copy of the reference crypto in the agent repository has the same checks, so that the
   contract sync into agent stays green.
