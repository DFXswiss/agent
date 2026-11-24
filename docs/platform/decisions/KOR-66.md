# KOR-66 – first public release key

1. The first public release key is listed in `install/release_keys.py`; the list stays on one line because the
   package build reads it with `build.KEYS_LINE`. A test checks the real file: every entry is an unpadded base64url
   Ed25519 public key of 32 bytes, none repeated, and the build can read the line.
2. No other test relied on an empty list: refusals for `release_key_missing` already pass an explicit empty list.
3. The agent way needs no Developer ID (`device add` and updates never read `notarized`; `installer -pkg` installs an
   unsigned package that the tool wrote itself, so it has no quarantine attribute). The runbook now says so and no
   longer mentions a "test mode" of the devices page, which the code does not have.
