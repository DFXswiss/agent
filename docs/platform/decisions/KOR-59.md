# KOR-59 – Own device without a terminal: decisions

Architecture: [ADR-074](../adr/074-own-device-without-terminal.md). Concept: [§1 point 4](../KONZEPT.md#k-1-b4),
[§3 point 3](../KONZEPT.md#k-3-b3), [idea 35](../KONZEPT.md#k-idee-35), [E-03](../KONZEPT.md#e-03),
[E-04](../KONZEPT.md#e-04), [E-05](../KONZEPT.md#e-05), [E-11](../KONZEPT.md#e-11), [E-19](../KONZEPT.md#e-19),
[A7](../KONZEPT.md#a-7).

1. **The runner runs as the Mac's owner.** The design started with a hidden service account. In a fresh macOS 27.2
   guest no root context could delete it again (`dscl -delete`, `sysadminctl -deleteUser`, also from an installer
   script, from a LaunchDaemon and with an administrator's password: `eDSPermissionError`). A removal must leave
   nothing, so no account is created; the owner is taken from the request file (agent way), the user at the screen
   (Installer.app) or the record of an earlier installation. Only UID ≥ 501. Isolation between the runner and the
   owner's account is weaker than with a dedicated account; tasks still run only in VMs with their sandbox.
2. **Claude Code is downloaded, not bundled** (no right to redistribute it): only the native macOS binary of the npm
   tarball pinned in the signed manifest, checked by SHA-256. No Node.js on the host.
3. **One channel for a code**: the root-only spool. Never a file name (the code opens the organization's keys and a
   file name ends up in `install.log`, backups and forwarded files), never an argv.
4. **Trust**: release keys are public data of the repository (`install/release_keys.py`, empty until the release
   operator adds one); the agent way trusts the keys of its own tool installation, an update those of the installed
   version, the download way Apple's Gatekeeper (offered on the page only for a notarised release). Without a key
   `device add` refuses (`release_unavailable`, `release_key_missing`); a test build may add its own key
   (`--trust-build-key`), a release build must already be listed.
5. **Feed expiry ≤ 120 days**, so a hub cannot freeze devices on an old release; the operator re-signs.
6. **Version = commit time** (`YYYY.MM.DD.HHMM`, UTC): reproducible and ordered.
7. **Own-device defaults**: `POOL_SIZE=0` (a personal Mac keeps its memory; the clone path stays under ten seconds),
   memory reserve a quarter of the memory (4–32 GiB), `LOGIN_AUTOMATION=off`, `VM_PREFIX=dfx-` (the owner's own Lima
   instances are never touched).
8. **Assignment**: `--wg` (repeatable), else the default workgroup of the only (or the named) organization. Giving the
   device to the whole organization in one step comes with KOR-41; until then the owner assigns more workgroups with
   `device assign`.
9. **Self-removal only after a verified `remove`** of the device in every org whose log it holds (`device_removed`
   in `sync_state`, shown by `status`); a `key_revoked` refusal of the hub alone never wipes a device.
10. **Updates**: window 02:00–05:00 local time, or after seven days of waiting (a Mac that sleeps at night); kept only
    with a new hub connection within 10 minutes; the attempt is counted before the way back; two failures mark the
    version bad; a pending update is saved before `installer` and finished by the next maintenance run.
11. **Time Machine**: the runner's home and its VM directories are excluded by the owner (sticky exclusion); a
    fixed-path exclusion by root needs Full Disk Access and did not take effect in the guest.
12. **Hub download area**: an optional directory (`AGENT_HUB_DOWNLOADS_DIR`), served anonymously with three name
    patterns; the read-log check of discovered hub routes now covers `/v1/` routes (every other route must be
    anonymous and outside the schema).
13. **Enrollment failures keep the installation**: `postinstall` never fails because of a code; `state.json` names
    the reason and `device add` reports it (`install_failed` with the reason).
14. **Evidence split** (work package "Nachweise"): a macOS guest has no nested virtualization, so the installation,
    the second installation and the removal are proven in a fresh macOS guest; the task on the device, the dialog and
    the download way are steps on a real Mac (`manual-checks/own-device.md`); the E2E scenario couples a runner through
    `device add` with the administrator step replaced (no root on the test devices).
15. **Code review (fresh context) – fixed:** the code reaches `enroll` on standard input (root writes nothing below
    the owner's home); the way back of an update runs only after a taken snapshot, a failure before it counts no
    attempt; a resumed update needs a hub connection newer than the one saved before it; the administrator script stops
    on every failed check, takes the owner before the copy and refuses root-owned or hard-linked request files;
    `device remove` unpairs before the removal dialog (a cancelled dialog is finished by the maintenance daemon); an
    installation of another owner is refused (`device_installed`, `other_owner`); `uninstall` checks the payload's
    parents; `~/.lima` is never followed; the reproducibility claim is limited to unsigned builds; E2E-56 checks the
    download link and claims only the stand-in's answer for the removal.
16. **Code review – left, with reason:** `device add` waits on `state.json` by time stamp, not a per-attempt nonce
    (a stale result needs a maintenance run in the same second; the result names the enrollment either way); the
    package is held in memory while downloaded (at most 2 GiB, one download per release); a build from `--source`
    takes the commit SHA from its caller (release builds use `--checkout`, which reads it from git).
