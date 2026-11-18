"""Sign-in with passkeys and the certified session key (PASSKEY-PROTOCOL.md), its pure part.

Part A of the change of section 16 step 3: every rule of that step that is a pure function of its
inputs. No module of this package reads a clock, the environment or a file, opens a database or
registers a route; every time is an input. The hub does not use this package yet.

- `errors`: the failure values the specification names and the refusal that carries one.
- `encoding`: the encodings and constants of section 2, the JSON rule and the three checks of a
  CBOR item, the fingerprint, the session key id and both signature encodings.
- `relying_party`: the origin and the relying-party id of section 3.
- `webauthn`: verifying a passkey response (section 4) and a registration (section 5.5, R4 to R13).
- `certificate`: the statement (section 6.2) and `verify_certificate` (section 6.4).
- `command`: the command text (section 8.1) and the pure checks C2 to C4 of section 8.2.
"""
