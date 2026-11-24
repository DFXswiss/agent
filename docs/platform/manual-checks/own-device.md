# Manual check: own device without a terminal (KOR-59, ADR-074)

Two parts. The first runs on a test device without a person; the second needs a real Mac and a person, because a
macOS guest has no nested virtualization (no VM, so no task, inside the guest) and no person to answer the system
dialog.

## 1. Installation probe in a fresh macOS guest (automated)

On a test device with Lima ≥ 2.3: a macOS guest from Apple's restore image (`template:macos-27`, the image's
SHA-256 as in the template), stopped once as an untouched base, cloned for every probe. Build a test package whose hub
is a loopback address (`--trust-build-key`, a throw-away release key), write the root script with
`python -c 'from agent_runner.install import admin; print(admin.install_script(), end="")'` and run
`scripts/macos-package/vm-probe.sh <clone> <package> <script> <code>`.

Expected: `The install was successful.`; `state.json` with the guest's user as `owner` and the enrollment result (a
code the hub does not know: `failed` with its reason; the installation stays); an empty spool; the payload owned by
root; the runner's home 0700 owned by the user; the maintenance daemon loaded with last exit code 0 and a `maintained`
line in its log; Claude Code for the host downloaded and linked; a second installation `The upgrade was successful.`;
the removal `ok` with empty `leftovers`; and the final comparison `NOTHING LEFT` (users, groups, LaunchDaemons,
receipts, `/Library/Application Support`, the user's home and `.lima`, launchd services). The KOR-59 report has the
measured run.

## 2. On a real Mac (one person, about 20 minutes)

Use a Mac that is not a production device, logged in as an administrator, with a test organization on a test hub
that serves a release (runbook `own-device-release.md`).

| # | Step | Expected |
|---|---|---|
| 1 | The person's agent runs `agent-platform device add --this-computer` (the account's `sudo -n true` fails, so the dialog way is taken) | the agent says the dialog sentence; **one** macOS dialog naming DFX; after it the command returns `device_id`, the workgroup, the notice and `admin: "dialog"` |
| 2 | Devices page of the organization | the Mac is listed as own device, online, in the workgroup |
| 3 | A task in that workgroup | it starts on the Mac (first start includes building the golden image) and answers |
| 4 | System Settings → General → Login Items | `dfx-agent-runner` listed; switching it off makes `state.json` show `disabled_in_login_items: true` and the agent open that pane on its next `device add` |
| 5 | The agent runs `agent-platform device remove --this-computer` | one dialog; `leftovers` empty; the Mac is gone from the devices page; `~/.local/share/dfx-agent-runner`, `~/.lima/dfx-*`, `/Library/Application Support/DFXAgentRunner`, both plists and `pkgutil --pkgs` entry gone |
| 6 | Without an agent: devices page → Add device → Download for Mac; open the package | Installer.app with one password dialog, then a dialog asking for the code; after pasting, step 2 holds |
| 7 | Devices page → Unpair | within 15 minutes the Mac removed its installation (as in step 5) |
| 8 | Without a dialog (E-27): over ssh to the same Mac, an account whose `sudo -n true` succeeds (e.g. after `sudo -v`, or a `NOPASSWD` rule), nobody needed at the screen; its agent runs `agent-platform device add --this-computer`, then restarts the Mac without logging in, then `device remove --this-computer` | no dialog; `admin` is `sudo` in both results; `owner` is that account; after the restart the device is online again without anybody logged in; `leftovers` empty |
