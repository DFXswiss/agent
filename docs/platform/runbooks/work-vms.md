# Runbook: work VMs on a runner device

Work VMs (ADR-059, spec [13-work-vm.md](../spec/13-work-vm.md)) are isolated Ubuntu VMs a session requests to run
anything. Inside, everything is allowed. Outside, the runner device keeps them tight: slot accounts, a pf anchor and
egress only through the device's Cloudflare client. This runbook covers setting them up, operating them, handling
faults and removing them. Devices are named by role only.

## Known limit of the per-account filter

Known limit of the per-account filter (rehearsal of 2026-11-24, decisions #34/#35): pf matches a slot by the
account of the packet's socket and does not attribute every packet; a packet it does not attribute matches none of
the anchor's rules and passes. A continuous prober reached, intermittently and in every client state, a listening
port of the device over its loopback and over its site address. A program in a work VM can therefore reach, with
single connections, the device's loopback services, the site network and - while the Cloudflare client is
disconnected - the internet with the site's address. The operator decided on 2026-11-24 to run work VMs on this
filter all the same; a boundary by interface is left to a later package.

## 1. Preconditions
- Runner at a version with work VMs. Without the installation of §3 nothing changes: the device offers work VMs once
  their first slot account exists. There is no fixed number of work VMs any more (ADR-075 addendum 2): a former
  `AGENT_RUNNER_WORKVM_SLOTS`, `…_WORKVM_MAX_PER_TASK`/`_OWNER`/`_ORG`, `…_SESSIONS_PER_VM` or `…_MAX_VMS` in
  `runner.env` is ignored and named once at the start (`runner.config.ignored name=… reason=fixed_limit_removed`);
  remove it at the next change of the file.
- The hub is updated before the runner. An older hub refuses a work VM metrics sample, and with it the whole
  `metrics` item.
- A golden image with the work VM tools (`workvm_*`) and the harness section "Your work VM". Existing workgroup VMs
  keep their golden until they are replaced.
- The Cloudflare One client is installed and enrolled by the operator on the runner device, and
  `/usr/local/bin/warp-cli --accept-tos status` shows `Status update: Connected`. The platform never handles the
  client's credential or configuration.
- pf is managed by the client; the platform never loads the main ruleset (§3).

## 2. Rehearsal on a test device (once per client or macOS release)
Run as root on a test device that has the Cloudflare client and **no** work VM installation:
```sh
sudo AGENT_RUNNER=<path>/agent-runner images/workvm/host-rehearsal.sh \
  --runner-account <unprivileged account> --site-ip <public IPv4 of the site> --deadline-minutes 20
```
- The script installs one slot, runs every check, disconnects and reconnects the client, restarts the client's
  service, uninstalls, and compares the state with the state before.
- Each check prints a line `check=… result=pass|fail`; the script exits 0 only when every check passed.
- A timer uninstalls and reconnects the client by itself after the deadline, even if the operator's session breaks.
- Keep the log directory it prints.

## 3. Installation (root, runner device)
1. Plan; this changes nothing:
   ```sh
   sudo env <runner settings> \
     agent-runner workvm install --runner-account <runner> --quota-gib <q> --plan
   ```
   Expected: `main_ruleset=untouched pf_conf=untouched … hook=ok`, exit 0. With `hook=missing` (the running main
   ruleset has no `anchor "com.apple/*"` as its first anchor line) stop here: the installation is refused.
2. The same command without `--plan`. It creates:
   - the APFS volume of the slot homes (quota `q`);
   - the slot accounts `<prefix>wvm00 …` and their group: as many as the RAM above `MEMORY_RESERVE_MIB` could hold
     standard work VMs (ADR-059 addendum 4);
   - the `limactl` wrapper and the sudoers rule;
   - the anchor `com.apple/000.agent.workvm`, its template and the watcher's LaunchDaemon
     `swiss.dfx.agent-runner.pf`.

   The main ruleset and `/etc/pf.conf` are never changed.
3. Check:
   - `sudo pfctl -a com.apple -sA` lists `com.apple/000.agent.workvm` first;
   - `cat /var/run/agent.workvm.state` shows `ok utunN`;
   - `agent-runner workvm firewall check` shows `ok=yes internet=yes`.
4. Enable: set `AGENT_RUNNER_WORKVM_DISK_QUOTA_GIB=<q>` (the quota of step 2) in the runner's environment, then run
   `agent-runner service install` and restart the runner. A device installed with a fixed number of accounts (e.g.
   4): run `workvm install` again; it adds the missing accounts.
5. Build the work image as the runner account: `agent-runner workvm image build` (about 2 minutes). The warm pool
   fills within a minute (`runner.workvm pool_ready … start_ms=`).
6. Run the egress check and keep its output (addresses only):
   ```sh
   sudo images/workvm/egress-check.sh --account <prefix>wvm00 --site-ip <site IPv4> --disconnect-test
   ```

7. After a restart of the device (and once after the installation, as a baseline):
   `sudo images/workvm/after-reboot-check.sh --accounts <n> --prefix <prefix>`. It checks the volume mount, the
   homes and that each `lima/_config` belongs to its slot account (0700), the watcher, the anchor and the hook; exit 0
   only when all pass. A `lima_config_…` failure (an installation before HOT-18): run `workvm install` again. It gives
   `_config` to the slot accounts and creates a missing one. It refuses (`name=lima_config`) when `lima/` or `_config`
   is a symbolic link; remove the link by hand and run it again.

Sizing: a standard work VM holds 8 GiB RAM and up to 50 GiB disk, `large` 16 GiB/100 GiB. Work VMs share the RAM
budget with the workgroup VMs through the device's one admission (05-runner.md §3.8): sessions come first - a new
work VM does not start while a session waits on the device, and idle work VMs are stopped when a workgroup VM needs
their RAM; running work is never ended for it. The CPU bound for every new VM is `AGENT_RUNNER_CPU_LOAD_MAX_PERCENT`
(default 85; it replaces `WORKVM_CPU_LOAD_MAX_PERCENT`). The disk quota is a physical bound of the volume: choose it
for the disk the device can give work VMs (about 100 GiB each, stopped ones included).

## 4. Operation
- `agent-runner workvm status` shows the slots, the accounts and the boundary (`ok`, `internet`, problem codes).
- Work VMs are temporary. The runner tears a VM down:
  - when the session releases it;
  - after 10 minutes idle (`WORKVM_IDLE_SECONDS`) it is stopped: the slot is free, the disk stays, the next command
    starts it again; a stopped VM is deleted after a day (`WORKVM_KEEP_STOPPED_SECONDS`). Slots in use:
    `agent-runner status` (`runner.status.workvm`) and the metrics `agent_runner_workvm_slots{state}` and
    `agent_runner_workvm_requests_waiting` (ADR-059 addendum 4);
  - when the session ends or the task closes;
  - when it restarts (no resume);
  - and always on a boundary failure.

  Every run is a `workvm.run` event of the session (command, exit code, duration, output tail).
- Monitoring: device → VM → session → work VM (CPU, RAM, disk, network, runtime). With slot accounts, the host
  values may be empty without root (decision MON-3); size, state and runtime are always there.

## 5. Faults
| Symptom | Meaning | Action |
|---|---|---|
| Sessions answer "not switched on … on this execution device" | not installed (no first slot account) | §3 |
| Requests wait ("capacity queue") | RAM, CPU or disk short, or a session waits on the device (`agent-runner status`: `runner.status.workvm memory_free_mib cpu_load_pct disk_used_gib`) | by design; sessions come first |
| Sessions answer "not ready right now" | no ready image, or the boundary check fails | `workvm status`; `workvm image list` |
| `not_hooked` / state `unhooked` | something replaced the running main ruleset without Apple's `anchor "com.apple/*"` first | The watcher keeps the slot accounts stopped. Find what reloaded pf (the client's update, an admin), restore Apple's main ruleset, never with a hand-made rule. The watcher then opens by itself. |
| `not_first` / state `unordered` | another child of `com.apple` sorts before ours | rename or remove that child; report it |
| state `ok -` while the client shows connected | the route of the work VMs' resolver does not go through `utunN` | check the client's split tunnel; work VMs have no internet meanwhile (fail-closed) |
| `pf_disabled` | someone disabled pf | The watcher enables it within 2 s; find out who disabled it. |

## 6. Removal
```sh
sudo env <runner settings as installed> agent-runner workvm uninstall [--keep-volume]
```
- It stops every slot process, the watcher and its anchor, and removes every file of the installation, the accounts,
  the group, the wrapper, the sudoers rule and (unless `--keep-volume` is given) the volume.
- It then reports what is left: `result=ok` with exit 0, or `incomplete` with problem codes and exit 1.
- Stop the runner first, so it requests no new VM meanwhile; after the removal it offers no work VMs (no slot
  account).
- The client and its rules are not touched.
