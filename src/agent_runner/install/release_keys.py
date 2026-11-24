"""The public release keys an installation trusts (ADR-074 sections 1 and 2): base64url Ed25519 public keys.

Public data, no secret: the release operator adds a key in a reviewed commit (`runbooks/own-device-release.md`),
removing one revokes it. Without any key `device add` refuses (`release_key_missing`). The list stays on one line:
the package build reads it from this file (`build.KEYS_LINE`).
"""

KEYS: tuple[str, ...] = ("8AJLm3FUBZ5LTMxP8Ener4JeDhFYkCbV0lm_FRtXNjo",)
