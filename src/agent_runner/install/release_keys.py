"""The public release keys an installation trusts (ADR-074 sections 1 and 2): base64url Ed25519 public keys.

Public data, no secret: the release operator adds a key in a reviewed commit (`runbooks/own-device-release.md`),
removing one revokes it. Empty until the first key is published: `device add` then refuses (`release_key_missing`).
"""

KEYS: tuple[str, ...] = ()
