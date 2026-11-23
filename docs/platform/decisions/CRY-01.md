# CRY-01 – Python reference crypto, formats, vector generator

Decisions taken where `04-crypto.md` sections 1-4, 8, 10 were silent or ambiguous.

1. **Generator and discovery.** The generator logic is `agent_hub.crypto.vectors` (so the coverage source
   `agent_hub` measures it); `contracts/tools/make_vectors.py` is a thin caller. Sections are discovered from
   `agent_hub/<area>/vectors.py` and `contracts/tools/vectors_*.py` (both with `SECTIONS`), so CRY-02 adds one file.
   CRY-02's text names `contracts/tools/vectors_log.py`; that works, but `agent_hub/log/vectors.py` keeps the section
   code under the 100 % gate and is recommended. Files no section produces are left alone (e.g. a hand-written
   contract amendment such as `title.json`).
2. **One code per fault class.** Malformed encodings anywhere (b64u with padding or non-canonical bits, wrong byte
   length, wrong key set, wrong type) are `header_invalid`; a missing or malformed *signature* field is
   `signature_invalid`. Section 8 has no separate "format" code.
3. **Check order** is fixed (structure → header key set → `non_ascii` → field formats → `ct` → size → signature →
   HPKE/AEAD) and written down in `contracts/vectors/README.md`, so that all three implementations return the same
   code for the same input.
4. **Canonical JSON** checks every string and key for 0x20-0x7E instead of scanning the output for `\u` (Spike D's
   check refused a literal backslash followed by `u`). Integers are limited to ±(2^53-1) (exact in JavaScript);
   floats and larger integers are `header_invalid`.
5. **Body JSON** allows floats (bytes inside ciphertext need not be identical across implementations, only
   parseable) but refuses NaN/Infinity and lone surrogates (`header_invalid`). Vectors contain no floats.
6. **IDs in headers** (event, credential, request) must be lowercase UUIDv7 with the RFC 4122 variant. `ref` is `""`
   or `[A-Za-z0-9._:/-]{1,200}` because read targets need not be UUIDs. Wrap `info`, bundle AAD and the recovery
   proof only require non-empty printable ASCII, which keeps the Spike D vectors reproducible and allows the `cred`
   scope (`scope_id` = user).
7. **Event headers** check `scope ∈ {org, wg}` and `scope_id == wg` (wg scope) / `scope_id == org` (org scope);
   `kind` is checked for its form only (dotted lowercase, ≤ 64). The kind/scope/author matrix (E3) and rights are
   CRY-02. Request kinds are also checked for form only.
8. **Sizes.** E8 applies to the decoded `ct` including the GCM tag (≤ 256 KiB; `session.terminal` ≤ 64 KiB), on
   seal and on verify. Credential records and device request envelopes have no limit in the specification; they are
   bounded by the 1 MiB signed-request body limit of `03-api.md` section 1 (`size_exceeded`).
9. **Invitation package.** A package that does not decrypt with the code is `invite_code_invalid` (section 5.1
   step 2); a decrypted package with a wrong structure is `header_invalid`. The structure is checked exactly
   (key sets of package, `head`, `keys[]`, `hub_key`; `keys` non-empty, `scope ∈ {org, wg}`, `hub_key.id` matching
   `[a-z0-9-]{1,64}` like `HubSigner` key IDs).
10. **Code texts.** After removing `-`/spaces and uppercasing: alphabet first (`invite_code_invalid`), then length
    (`invite_code_length`, for recovery codes `recovery_code_length`), then zero trailing bits
    (`invite_code_invalid`). A recovery code with a bad character is `invite_code_invalid` – section 8 has no
    recovery-specific alphabet code.
11. **Dedupe field sets** (section 4.10 names only the normalisation): field names follow the operation parameters
    (`repo`, `ref`, `session`, `branch`, `parent_sha`, `tree_sha`, `commit_sha`, `head`, `base`, `number`, `body`,
    `prompt`), the table is in `agent_hub/crypto/dedupe.py`. SHAs are lowercased. `subagent.request` hashes
    `purpose ‖ 0x00 ‖ prompt` (the plain concatenation of `03-api.md` section 9 is ambiguous: "ab"+"c" = "a"+"bc").
    Branch/ref names and idempotency keys that are not printable ASCII become `sha256:<hex>` (canonical JSON is
    ASCII-only; git allows non-ASCII branch names). Types without dedupe fields are `header_invalid`.
12. **HPKE** is built from pyca primitives (HMAC-SHA256, X25519, AES-GCM) because pyca's `hpke.Suite` cannot take a
    fixed ephemeral key; the tests check both directions against `hpke.Suite` and the vectors open with it. A
    small-order `enc` (all-zero shared secret) is `decrypt_failed`.
13. **P-256** session signatures are raw `r‖s`; vectors use RFC 6979 deterministic signing (pyca
    `deterministic_signing`) so that the file is stable; a high `s` verifies, as in `agent_core.passkey`.
14. **Runner storage key** derivation (`kdf.storage_key`) and its vector are included here, so CRY-03 needs no
    contract amendment for it.
15. **Errors.** `CryptoError(code)` accepts only codes of section 8 (crypto and log lists), so CRY-02 raises the same
    class. The message is the code; `fail()` suppresses the exception context so no input leaks through chained
    tracebacks.
16. **Enrollment.** `sign_enrollment` refuses a body whose `pub.sig` is not the signing device key; the text leaves
    out `enroll_sig` and `inv_sig` whether or not they are present.
17. **Salt and nonce are drawn by the sealer.** `seal_event`/`seal_credential` take the header *without* `salt`
    and `nonce` and draw both on every call (a header that already contains them is `header_invalid`), so an edited
    body re-sealed under a retried event ID never reuses key and nonce (review finding). Deterministic values are
    possible only through the explicit test-vector parameter `fixed_salt_nonce`.
18. **No exception context on refusals.** `fail()` is never called inside an `except` block (a JSON decode error
    holds the decrypted plaintext as `doc`); a test checks `__context__ is None` for every refusal path.
19. **Org-scope events** may name a workgroup in `wg` only for kind `wg.profile` (04 section 4.3, checked in E1).
20. **Code parsing** checks the base32 alphabet before uppercasing (`str.upper()` maps `ſ`/`ı` to `S`/`I`); times
    accept ASCII digits only; non-string inputs, empty user IDs and non-Unicode text are refusals with a code, never a
    Python `TypeError`/`AttributeError`.
21. **Canonical precedence.** `non_ascii` is checked over the whole value before the type checks, so a value with
    both faults yields `non_ascii` in every implementation.
22. **Wrap signature over b64u text.** 04 section 4.2 signs `canonical({…, wrap})` with the wrap as b64u text, while
    section 1 says signatures over ciphertexts cover raw bytes. The reference follows 4.2 literally (the more
    specific rule); strict b64u makes the text a unique encoding, so there is no malleability. Noted in the vector
    README.
23. **Spike D interop.** Reproduced byte for byte: PRF input, identities, `KEK_i`, `KEK_rec`, both bundles, the three
    HPKE wraps, `invite_id`, `package_key`, `pk_inv`, the code text and the package sealing (body JSON equals
    canonical JSON for ASCII content). The spike event's key derivation, AAD and signature construction reproduce
    as well; its header is refused (`header_invalid`) because the key set changed (section 4.3).
