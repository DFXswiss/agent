# KOR-73 – decisions

Package: subscriptions logged in side by side, each with its own mailbox, each login in its own sandbox, registration.
Architecture: [ADR-100](../adr/100-parallel-subscription-logins.md). Rule: [E-33](../KONZEPT.md#e-33).

1. **Mailbox in the subscription record**, not a new credential kind (ADR-100 §1): the record already is the owner's
   sealed place; no new rotation path.
2. **Plaintext `slot` at the hub** beside the envelope (like `org_id`): the hub needs a key to decide replacement and
   the signed envelope format stays unchanged; the runner trusts only the decrypted `subscription_id`.
3. **`LOGIN_SANDBOX=vm` by default; `process` only explicitly** (design review M5): the process sandbox is a separate
   Chromium, not a VM; it stays for devices without work VMs and for the hermetic E2E topology, whose doubles a VM
   cannot reach. Login VMs are not billed and count for no organization/owner limit (no organization; minutes long);
   the capacity queue applies.
4. **The live picture of a check for humans travels as sealed responses of the login request** (not the frame route
   of ADR-085 §6): a login has no workgroup or session; 80 pictures at most keep a login below the hub's 100
   responses per request.
5. **Only the user's own identity may click** (design review H2): an agent's click is ignored, so no model answers a
   check for humans; `relay` is allowed for both. The agent tool refuses `--click` locally (`human_only`).
6. **`extended: true`** opts a client into the new responses (design review H1): older web clients and agent tools
   parse closed lists and would drop a response with an unknown stage or field.
7. **Consent is declared for the account holder** (design review H3): clients phrase each box as the holder's consent
   (unticked by default); the signed request records who declared it; the provider's terms are not judged.
8. **Mail code**: the legacy link poll takes only `https://` links and stays untouched; a small reader
   (`login/mailcode.py`, reusing the legacy helpers) finds the six-digit code. In the hermetic five-login test the
   pages are plain HTTP, so those logins sign in with the code.
9. **Five parallel logins are tested with the real browser helper** (one state directory and socket per login, as one
   VM per login) instead of five Lima VMs: the VM path itself (obtain, delete, no billing, no limits) is tested with the
   work-VM kit; a real-Lima run would need the boundary to reach the doubles.
10. **Real-page recognition is not live-tested** (package rule): the cues (Turnstile/hCaptcha/reCAPTCHA frames and
    titles, e-mail input with "Continue with email", code input, `/oauth/authorize` allow button, the callback code)
    come from the double, which was rebuilt to the real shapes, and from public knowledge of the provider's pages.
    The first live login in operation shows whether a cue needs adjusting.
11. **`AGENTS.md` at the repository root stays unchanged**: it is a legacy file (R-P.2, `test_legacy_untouched`). The
    agent guidance for the new commands lives in `docs/platform/agents.md` (and its copy for the web) and in the tool's
    `guide.md`; the parity rows are in `agent-parity.md`.
12. **The check for humans is a step for a person** (`agent-parity.md`, table "Without a person", kind `outside`): it
    belongs to the provider's sign-in page; without a person `subscriptions check --relay` gives the automation up.
