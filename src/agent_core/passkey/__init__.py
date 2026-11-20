"""Sign-in with passkeys and the certified session key (PASSKEY-PROTOCOL.md).

The pure part (part A of the change of section 16 step 3): every rule of that step that is a pure
function of its inputs. These modules read no clock, no environment and no file, open no database
and register no route; every time is an input.

- `errors`: the failure values the specification names and the refusal that carries one.
- `encoding`: the encodings and constants of section 2, the JSON rule and the three checks of a
  CBOR item, the fingerprint, the session key id and both signature encodings.
- `relying_party`: the origin and the relying-party id of section 3.
- `webauthn`: verifying a passkey response (section 4) and a registration (section 5.5, R4 to R13).
- `certificate`: the statement (section 6.2) and `verify_certificate` (section 6.4).
- `command`: the command text (section 8.1) and the pure checks C2 to C4 of section 8.2.

The hub side (part B): the storage, the routes and the operator commands, which the hub registers
from `agent_core.app` and `agent_core.__main__`. The website (part C) is not here.

- `storage`: the tables of section 12 and every read and write on them.
- `invites`: the fields of a new invite (I1), one invite of the list (section 5.2) and the texts
  for the keys-file pull request (section 5.6).
- `http`: the routes of sections 5.4 to 6.3 that carry no registered key, and the routes under
  `/api/key/` of sections 5.1, 5.2, 7.2, 7.3 and 8.2.
- `operator`: `invite-bootstrap` and `invite-list` (section 5.3).
"""
