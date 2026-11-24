# ADR-074: An own Mac becomes an execution device without a terminal

Status: Accepted (2026-11-24)

## Context
- The concept lets users connect their own hardware, for example a Mac Studio
  ([§3 point 3](../KONZEPT.md#k-3-b3)), coupled with an invitation code and authenticated by a device key without SSH
  ([§1 point 4](../KONZEPT.md#k-1-b4)), and given exclusively to one workgroup or to the whole organization
  ([idea 35](../KONZEPT.md#k-idee-35)).
- The owner's rules: the user only creates a passkey, the user's AI agent sets up everything else, and nothing needs a
  terminal ([E-03](../KONZEPT.md#e-03), "ich weiss nicht was ein terminal ist"); whatever the web client can do, an
  agent can do too ([E-04](../KONZEPT.md#e-04)); a missing precondition is started, not explained
  ([E-05](../KONZEPT.md#e-05)); no approvals that are not technically required ([E-11](../KONZEPT.md#e-11)); a fix
  closes the whole class of a fault ([E-19](../KONZEPT.md#e-19)); the start needs no configuration
  ([basic principle](../KONZEPT.md#k-gp)).
- Until now a device was added with a checkout, a virtual environment, `pip install`, `agent-runner enroll` and
  `sudo agent-runner service install` (`runbooks/runner-installation.md`): a terminal with administrator rights, a
  dedicated account created by hand, Lima, PostgreSQL and Claude Code installed by hand.
- Some steps need root on macOS (a LaunchDaemon, files outside a user's home, the package receipt). Without a
  terminal macOS offers two ways to get root: Installer.app opening a package, and the authorization dialog of
  `osascript … with administrator privileges`. Each shows one system dialog.
- A design review with a fresh context (KOR-59) found the first draft's trust roots, the code in a file name, a
  package swap between check and install, a self-removal on the hub's word alone, the shipping of Claude Code and the
  late notarisation wrong; this record is the corrected design.

## Decision

### 1. One versioned runner package with a signed manifest
- Every release is one installer package `DFX-Agent-Runner-<version>.pkg` (identifier `swiss.dfx.agent-runner`;
  version `YYYY.MM.DD.HHMM` of the commit time in UTC), built by `agent_runner.install.build` (`src/agent_runner/install/build.py`) from a commit and the lock
  file `scripts/macos-package/lock.json`, which pins every bundled third-party input by URL and SHA-256: a relocatable
  CPython 3.12, the runner's Python wheels (`scripts/macos-package/requirements.lock`, hash-checked), Lima and PostgreSQL.
- **Claude Code is not bundled** (its licence does not let a third party redistribute it). The signed manifest pins
  the npm tarball of the native macOS binary by URL and SHA-256; the installation downloads it from the npm registry,
  checks the digest and puts only the binary next to the payload (`host/`), as the golden build does inside the VM.
  No Node.js is needed on the host.
- The payload `/Library/Application Support/DFXAgentRunner/versions/<version>/` is owned by `root:wheel` and writable
  by nobody else; the helper refuses to run when any directory above it is not owned by root or is writable by
  others. `current` is a link to the active version, switched by renaming a new link into place.
- `release.json` lists every payload file with its SHA-256, the commit, the version, the hub origin and the platform
  root key the build is for; `release.json.sig` is an Ed25519 signature over its exact bytes by a **release key**.
  The public release keys are data of the repository (`src/agent_runner/install/release_keys.py`); the release
  operator holds the private key. A new key is added by a release signed with an existing key; removing a key from
  the list revokes it.
- **Code signing is a build stage before the manifest.** With a Developer ID identity, `build.py` signs every Mach-O
  file with the hardened runtime and a timestamp (entitlements in `scripts/macos-package/entitlements/`: virtualization for
  Lima, disabled library validation for the Python interpreter), then writes the manifest, then builds and signs the
  package; notarisation and stapling follow (`runbooks/own-device-release.md`). Without an identity the same build
  produces an unsigned package (tests, and until DFX has a Developer ID).
- Reproducible (unsigned builds; a signed build adds time-stamped signatures, so its check compares the unsigned
  stage of two builds): two builds of one commit and lock produce the same `release.json` (normalised modes and times, byte
  code with unchecked hashes and the installed path, each file compiled by a fresh interpreter that reads no byte
  code, no console scripts); the launcher runs `python -I -B`, so nothing is ever written into the payload. Measured:
  two builds byte-identical, 210 MB package, 45 s per build. Amended by HOT-19: one `compileall -j 0` run was not
  enough, its byte code depended on what each worker had compiled before (`decisions/HOT-19.md`).

### 2. Trust roots
| Way | What decides that the code is genuine |
|---|---|
| Agent (`device add`) | the release keys of the agent's own `agent-platform` installation verify `latest.json` (version, package digest, expiry); the root step checks the digest again on a root-only copy |
| Update | the release keys of the version already installed verify `latest.json` and the package digest, never keys of the new package |
| Download from the device screen | Apple: Gatekeeper checks the Developer ID signature and notarisation of the downloaded package; the web client and the hub that serve it are trusted as for the web client itself (build hash on the security page). Inside, `postinstall` verifies `release.json` (integrity). The device screen offers the download only for a release whose `latest.json` says `notarized: true` (any release in test mode). |

The hub stores and serves the release files; it cannot make a device run other code on the agent and update ways.
`latest.json` carries an expiry (at most 120 days); an expired feed stops updates and is shown as such.

### 3. One privileged step, one enrollment channel, the owner's account
**The runner runs as the Mac's owner, not as a new account.** The first design created a hidden service account.
In a fresh macOS 27.2 guest no root context could delete that account again: `dscl . -delete` and
`sysadminctl -deleteUser` end with `eDSPermissionError` from an installer script, from a LaunchDaemon and with an
administrator's password (measured, KOR-59 report); macOS only allows it to processes with Full Disk Access. A
removal would leave an account behind, which the work package rules out. The runner therefore runs as the person who
installed it, whom an own device belongs to anyway: the agent way records the owner of the request file, Installer.app
takes the user logged in at the screen, an update keeps the recorded owner. Only a regular account (UID ≥ 501) can be
the owner. The runner's home is `~/.local/share/dfx-agent-runner` (0700), its VMs are `~/.lima/dfx-*`
(`AGENT_RUNNER_VM_PREFIX=dfx-`, so the owner's own Lima instances are never touched).

Everything that needs root is in the package's `postinstall` (`libexec/root-helper postinstall`):
- `current`, one previous version kept for the way back, older ones removed;
- Claude Code for the host (above);
- the LaunchDaemon `swiss.dfx.agent-runner.maintenance` (root, every 15 minutes, §6) and, once enrolled, the runner's
  LaunchDaemon `swiss.dfx.agent-runner` (as the owner, settings in §4), both started through a launcher with a
  recognisable name (`libexec/dfx-agent-runner`);
- the runner's home, created **by the owner** (`sudo -u <owner> mkdir`), excluded from Time Machine by the owner
  (sticky exclusion); the parents it had to create are recorded and removed again with the installation;
- the enrollment from the **spool** `/Library/Application Support/DFXAgentRunner/spool/enroll.request` (root, 0600),
  which is the only channel for a code; the code reaches `agent-runner enroll --code-file -` on its standard input,
  never in an argv or a file below the owner's home; a failed enrollment keeps the installation and names
  the reason in the state;
- the public state file `state.json` (0644: version, owner, enrolled, device ID, daemon state, golden state, update
  state, enrollment result; no secret, no content).

Root never creates, removes or executes anything below the owner's home; it runs the owner's commands with
`sudo -u <owner>` and reads only their JSON output.

The two ways only differ in how the code reaches the spool:
- **Agent** (`agent-platform device add`): the agent writes the code into a 0600 request file in a private directory
  and runs one `osascript` with `with administrator privileges` and a prompt naming DFX. Its script text is a constant
  (`agent_runner.install.admin`); the package path, the expected digest and the request path are passed as `argv` and
  quoted with `quoted form of`. As root it makes a root-only directory, copies the package (`cp -P`) and the request
  into it, checks the digest of the copy, moves the request and the name of its owner into the spool and runs
  `installer -pkg <copy> -target /`. The agent says in one sentence what the dialog means: "macOS asks once for your
  password: this installs the DFX execution service on this Mac." Installing again is idempotent.
- **Download** (device screen): the screen shows the code once and a "Download for Mac" link. Installer.app shows its
  own dialog; `postinstall` (not a command-line install) asks the logged-in owner for the code in a dialog of the
  owner's session (`launchctl asuser`), writes it to the spool and enrolls. Cancelling leaves an installed, unenrolled
  device; opening the package again asks again. The code is never part of a file name.

### 4. `agent-platform device add [--org | --wg ID …] [--this-computer]`
On the computer the agent runs on: checks (macOS ≥ 14, Apple silicon, virtualization, memory ≥ 16 GiB, free space ≥
50 GiB; a failure that the user can fix is started, not explained (E-05): an old macOS opens Software Update, a full
disk opens the storage settings; the rest is one closed code and one sentence), fetches and verifies `latest.json`
and the package, creates the device invitation (KOR-42 `device invite`: `--wg` for workgroups, else the default
workgroup of `--org` or of the only organization; the whole organization in one step comes with KOR-41), runs the privileged step and waits on `state.json` for the enrollment. It reports the
state (`active`; golden `ready` or `building`), the assignment and, as A7 asks for own hardware too
([A7](../KONZEPT.md#a-7), [§5 point 4](../KONZEPT.md#k-5-b4)): "Whoever administers this Mac can read the content
of every task this device runs for the organization – also of workgroups you are not a member of – including what
is kept on the Mac after a task ran."

Resource defaults of an own device (ordinary runner settings in its plist): no warm pool (`POOL_SIZE=0`, the clone
path stays below the ten seconds of [§3 point 6](../KONZEPT.md#k-3-b6)), memory reserve a quarter of the physical
memory (4–32 GiB), mailbox login automation off (as every org device).

### 5. Removal without a terminal
- `agent-platform device remove --this-computer`: unpairs the device in the org log (`remove` with the rotations, as
  the device screen does), then one dialog runs the helper's `uninstall`: both daemons unloaded, the runner's
  `uninstall` as the owner (VMs `dfx-*`, goldens, store, identity), the runner's home and the parents the installation
  created removed by the owner, plists, payload, spool and package receipt removed. The helper then lists what is left
  (paths including the owner's runner home and `~/.lima/dfx-*`, launchd services, the receipt); the command prints the
  list, which must be empty. Measured in a fresh macOS guest: install, reinstall and removal leave no difference in
  users, groups, LaunchDaemons, receipts, `/Library/Application Support`, the owner's home and launchd services.
- The device screen's "unpair" is enough without an agent: the runner applies the verified `remove` of its own
  device and records it (`device_removed`); the maintenance daemon then removes the installation with the same steps,
  logs the list to the unified log (`logger -t dfx-agent-runner`) and unloads itself from a detached process at the
  very end. A `key_revoked` answer of the hub alone never removes anything (the hub must not be able to wipe
  devices); the device then only stops and shows it.

### 6. Updates with a way back
The maintenance daemon (root, payload code only) runs every 15 minutes:
1. writes `state.json`; reports a daemon disabled in Login Items as `disabled_in_login_items` (the agent then opens
   that pane, E-05);
2. completes a removal (§5) after a verified `remove`;
3. ensures a golden of the active payload's pins (`agent-runner image build` as the owner; the previous
   golden stays until the new one is ready, VMs move as in 05-runner §3.5);
4. at most once an hour reads `latest.json`; a newer, unexpired version not marked bad is installed when no session
   is working and either it is inside the update window (local time, default 02:00–05:00) or the release has waited
   seven days (a Mac that sleeps at night): download into a root-only directory, digest, `installer -pkg`. Before the
   switch the store and plists are kept (an APFS clone of the store, made by the owner). The new version must run and
   report a hub connection within 10 minutes; otherwise the store and plists come back from the clone, `current` goes
   back, and the attempt counts (counted before the way back runs). Without a hub connection nothing was pushed, so the
   restored store agrees with the hub. A version that failed twice is marked bad and never tried again; one success
   clears the count. A maintenance run that ended while it waited finishes the update in the next run (kept when the
   runner is connected, else the way back).

### 7. The download area of the hub
`GET /downloads/runner/{latest.json | latest.json.sig | DFX-Agent-Runner-<version>.pkg}` serves files of an
optional directory (`AGENT_HUB_DOWNLOADS_DIR`) anonymously, with strict names; without the setting the paths answer
`404 not_found`. The release operator places a release there; the hub never signs or changes it.

### 8. Security summary
- Supply chain: pinned inputs, signed manifest and feed, keys outside the hub, code signing before the manifest,
  Claude Code fetched from its registry with a pinned digest.
- Root: runs only root-owned payload code (`python -I -B`, absolute paths, argv lists); never creates, removes,
  imports, executes or follows links below the owner's home; reads only the JSON of `agent-runner … --json` as data.
  The one shell script (the `osascript` step) is a constant with quoted arguments.
- The runner's account: the owner's, without new rights; the daemon cannot change any file root executes. A runner
  that is taken over acts with the owner's rights on this Mac (not with root's); tasks still run only inside the VMs
  and their sandbox (05-runner §4). This is weaker than a dedicated account and the price of a complete removal on
  macOS 27; a dedicated account comes back when the package can be signed with a Developer ID and granted Full Disk
  Access for its removal (follow-up).
- Enrollment code: 24 h, single use, only in 0600 files (request, spool, the hand-over file in the runner's home),
  removed after use, never in an argv, a log or a file name.
- Plaintext: the Mac holds its device key and the keys of every workgroup it serves; its administrators can read
  their content, also afterwards (sentence in §4); the runner's home and VMs are excluded from Time Machine, earlier
  backups are the owner's. The hub learns nothing new.

## Consequences
- A user's agent connects the user's Mac with one command and the user sees one system dialog; without an agent the
  same works with a download, the system dialog and a code dialog. Removal and updates need no terminal.
- Devices run releases signed by the release operator; a broken release goes back by itself.
- Until a public release key is in `release_keys.py`, `device add` refuses with `release_key_missing` and updates do
  not happen. Until DFX has a Developer ID, the download way is closed outside test mode (an unsigned package needs
  System Settings → "Open Anyway" since macOS 15); the agent way is not affected (`installer` does not consult
  Gatekeeper), and macOS names the background item after the launcher instead of a team.
- A Mac that sleeps is offline for the platform; tasks wait for it like for any busy device (KOR-41).
- Platform devices of DFX keep their administrator installation (runbook); they may use the package too.

## Alternatives considered
- **A dedicated hidden service account**: the stronger isolation, but macOS 27 refuses to delete it again without Full
  Disk Access (measured), so a removal would leave it behind. Rejected for now (§3).
- **A LaunchAgent in the user's session without root**: no dialog, but the runner would stop at logout and could not
  be updated with a way back by anything it cannot change itself. Rejected.
- **SMAppService with a helper inside a signed app**: needs approval in System Settings (clicks instead of one
  dialog) and a Developer ID. Possible later.
- **Code in the download's file name**: the code opens the keys of the organization; a file name ends up in
  `install.log`, backups and forwarded files. Rejected.
- **Device-initiated pairing confirmed in the web**: no code to paste, but a new protocol for org devices. Later.
- **Updates through the hub's signature**: the hub would control root code on every own device. Rejected.

## Deviation from DESIGN.md
None beyond ADR-001.
