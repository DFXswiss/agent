# Runbook: releasing the runner package of own devices (ADR-074)

Scope: the release key, building, signing, notarising and publishing `DFX-Agent-Runner-<version>.pkg`, and what an
own device does with it ([05 §16](../spec/05-runner.md#16-own-device-installation-without-a-terminal)).
Done by the release operator on an administrator's Mac with the repository checked out; never on a runner device.

## 1. The release key (once)

1. Create an Ed25519 key offline: 32 random bytes, written base64url without padding into a file of mode 0600 that
   never leaves the operator's keychain or safe:
   `python3 -c 'import os,base64;print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="))' > release.key`
2. Its public half: `python3 -c 'from agent_runner.install import release; import sys; from agent_runner.crypto.encoding import b64u_decode; print(release.public_key(b64u_decode(open(sys.argv[1]).read().strip(), 32)))' release.key`
3. Add the public half to `src/agent_runner/install/release_keys.py` (`KEYS: tuple[str, ...] = ("<b64u>",)`, on one
   line: the build reads it from there) in a reviewed commit. Without any key listed, `agent-platform device add`
   refuses with `release_unavailable` (`release_key_missing`) and own devices do not update. The first key is listed
   (KOR-66). A package must be built from a commit that already contains its key: the payload trusts only the keys of
   its own `release_keys.py` (`build_failed` `release_key_untrusted` otherwise).
4. Rotation: add the new public key, release, then remove the old key in a later release (signed with the new key).
   Removing a key revokes it for every device that runs a release without it.

## 2. Build

```sh
.venv/bin/python -m agent_runner.install.build --checkout . --commit <commit> --hub https://<hub origin> \
  --root-key <platform root public key> --release-key-file release.key --work <scratch> --out <out> \
  [--sign-identity "Developer ID Application: <team>"] [--installer-identity "Developer ID Installer: <team>"]
```

Without identities the package is unsigned (tests). With them every Mach-O file is signed with the hardened runtime
and a time stamp before the manifest is written (entitlements in `scripts/macos-package/entitlements/`), and the package is
signed. Build twice without identities into two scratch directories and compare `release.json` (`cmp`): equal
bytes are the reproducibility check (signatures carry time stamps, so signed builds are compared before signing).
The interpreter library is compiled file by file, each in a fresh interpreter that reads no byte code, so the byte code
does not depend on cores, order, directory or time (a few seconds per thousand files).

If the two `release.json` differ, publish neither build. List the files whose digests differ:

```sh
python3 -c 'import json,sys; a,b=(json.load(open(p))["files"] for p in sys.argv[1:]); [print(k) for k in sorted(a.keys()|b.keys()) if a.get(k)!=b.get(k)]' \
  <scratch1>/stage/release.json <scratch2>/stage/release.json
```

Keep both scratch directories and report the commit, the device and the listed paths as a build defect; the gate test
`test_byte_code_does_not_depend_on_what_the_compiling_process_compiled_before` covers the known cause (a `.pyc` that
depended on what the compiling process had compiled before). Publish only once two builds of the fixed commit agree.

## 3. Notarise (with a Developer ID)

`xcrun notarytool submit <out>/DFX-Agent-Runner-<version>.pkg --keychain-profile <profile> --wait`, then
`xcrun stapler staple <pkg>`. Stapling changes the package bytes: rebuild the feed afterwards with
`--notarized` (or re-run the build with `--notarized` and staple again) so `package_sha256` matches the stapled file.
The device screen offers the download only for a feed with `notarized: true` that has not expired.

Without a Developer ID (sections 2 and 4 only, `notarized: false`) the agent way installs and updates: `device add`
checks the feed's signature, expiry, minimum macOS and the package digest, never `notarized`; it writes the package
itself (no quarantine attribute) and the administrator step installs it with `installer -pkg`, which accepts an
unsigned package. Updates check the feed's signature and expiry and the package digest (not `notarized`) and install
the same way. Such a build re-signs nothing: the Mach-O files keep the signatures of their pinned upstream archives,
so `limactl` runs VMs only with the virtualization entitlement it ships with; a task on a test Mac shows it. Only the
download for people stays closed.

## 4. Publish

Copy `<out>/DFX-Agent-Runner-<version>.pkg`, `latest.json` and `latest.json.sig` into `<AGENT_HUB_DOWNLOADS_DIR>/runner/`
of the hub (package first, the feed last, so no device sees a feed whose package is missing). Check:
`curl -fsS https://<hub>/downloads/runner/latest.json`. Re-sign the feed (a new build of the same commit, or a feed-only
build) before its `expires` (at most 120 days): an expired feed stops all updates and shows `feed_expired`.

## 5. What devices do

Each own device checks the feed at most hourly, installs a newer release when no task runs (02:00–05:00 local time,
or after seven days of waiting), keeps it when its runner reports a hub connection within 10 minutes and otherwise goes
back to the previous version (twice failed: never tried again). `state.json` in
`/Library/Application Support/DFXAgentRunner/` shows the result on the device; `agent-platform device list` shows the
version through the hub.

## 6. Faults

| Symptom | Meaning | Action |
|---|---|---|
| `release_unavailable` `release_key_missing` | no public release key in the tool's installation | §1 |
| `release_unavailable` `release_signature_invalid` | feed signed with an unknown key or changed | re-sign with a listed key; check the hub's download directory |
| `feed_expired` in `state.json` | the feed was not re-signed in time | §4 |
| a version in `update.bad` | it failed twice on that device | fix and release a newer version |
| `install_failed` `owner_unknown` | the installer found no regular account to run as | open the package while logged in as the Mac's owner |
