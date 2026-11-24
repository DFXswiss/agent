# 11 Work VM on request (ADR-059)

Decision of the client of 2026-11-23 (ADR-059). A session requests its own isolated Ubuntu VM through a platform
operation and may do anything inside it; the VM is tight towards everything but the internet and holds no
credential. This section is binding for the runner (`agent`), the guest, the work image, the contracts and the hub
and web (`agent-core`). Stages: (a) request, commands, files, visibility in chat and terminal; (b) warm pool, start
time, lifetime, capacity, monitoring; (c) browser and web search; (d) web views, documentation, clean-up.

## 1. Terms
- **Work VM**: a Lima instance `<prefix>wvm-<8 hex>` of the **work image**, owned by one task (scope `task`) or one
  session (scope `session`). ID `workvm_id` = `wvm-<8 hex>` (random, independent of the Lima name).
- **Work account**: the macOS account that owns and runs every work VM (`AGENT_RUNNER_WORKVM_USER`). Its Lima home is
  `AGENT_RUNNER_WORKVM_LIMA_HOME`.
- **Job**: one command started with `workvm_exec`; ID `job_id` = `j-<12 hex>`.
- **Mirror**: `/workspace` in the VM, the copy of the session workspace (`work/` of the session).

## 2. Operations (03-api.md §9, ADR-012)

| Type | Params | Result | Dedupe |
|---|---|---|---|
| `workvm.request` | `{size?: "standard"\|"large"\|"xlarge", scope?: "task"\|"session"}` | `{workvm_id, state, scope, size, cpus, memory_mib, disk_gib, created, ready_ms}` | none (idempotent: an existing VM of the scope is returned with `created:false`) |
| `workvm.release` | `{scope?: "task"\|"session"}` | `{released, workvm_id?}` | none (idempotent: nothing to release is `released:false`) |
| `test.run` (KOR-43, §12) | `{kind, command, cwd?, timeout_seconds?, size?}` | `{run_id, state, exit_code, duration_ms, ready_ms, summary, log, tail, reason}` | none at the hub; the runner joins a run of the same session on the same workspace while it runs (§12) |
| `browser.open` / `browser.close` (KOR-44, §13) | `{url?, idle_seconds?}` / `{}` | `{browser_id, created, state, workvm_id, ready_ms, idle_seconds, url, title}` / `{closed, browser_id, actions, reason}` | none (idempotent per session, §13.1) |

- Sizes: `standard` 4 CPU / 8192 MiB / 50 GiB (default), `large` 8 / 16384 / 100, `xlarge` 16 / 32768 / 200. A size
  above `AGENT_RUNNER_WORKVM_MAX_SIZE` (default `large`) is `limit_exceeded`. An existing VM keeps its size.
- Scope `task` (default for the main session): the VM of the task. Scope `session`: a VM of the requesting session
  only; a subagent always gets scope `session` (its request's `scope` is ignored), because a VM's `/workspace`
  mirrors one workspace. Commands of a main session use its own `session` VM when it has one, else the task VM;
  commands of a subagent use only its own VM.
- Limits (formal checks, `limit_exceeded`): at most `WORKVM_MAX_PER_TASK` (default 4) VMs per task including session
  VMs, `WORKVM_MAX_PER_ORG` (default 16) per organization, `WORKVM_MAX_PER_OWNER` (default 8) per task owner, on this
  device.
- Device without the work boundary (§6) or without a ready work image: `workvm_unavailable` (new code,
  `retryable:false`).
- Capacity (§7): when the VM does not fit now the executor waits in a FIFO queue up to
  `WORKVM_QUEUE_SECONDS` (default 900 s; the MCP call answers `pending` after 20 s, the `operation.finished` ping
  follows); then `timeout` (`retryable:true`).
- `workvm.release` deletes the VM (`limactl delete -f`), its jobs and its mirror.
- Audit: the operation record itself (`op.registered`, transitions) – request, release; the runner adds the hub
  transition reason `workvm.deleted` when it deletes a VM on its own (task closed, orphan).

## 3. Tools in the session (03-api.md §8.1)

Five MCP tools of the `platform` server. The guest forwards each call as journal record `workvm.call
{pending_id, tool, arguments}` (pending kind `workvm`), waits for the runner's answer up to the call's
`wait_seconds` + 5 s (at most 33 s, below the `/mcp` route limit) and returns it. They are not reads: no
`platform.read` event, no `read.session.*` audit; every call is visible in the chat as the `session.tool` event of the
transcript (input and result) and in the read-only terminal (Claude Code's own rendering).

| Tool | Input | Output |
|---|---|---|
| `workvm_exec` | `{command, cwd?, env?, stdin?, timeout_seconds?, wait_seconds?, sync?}` | job |
| `workvm_output` | `{job_id, offset?, limit?, wait_seconds?}` | job |
| `workvm_kill` | `{job_id, signal?}` | job |
| `workvm_status` | `{}` | `{workvm, jobs}` |
| `workvm_sync` | `{direction, path?}` | `{direction, path, files, bytes, deleted, skipped}` |
| `workvm_browse` | `{url, action?, selector?, text?, wait_ms?, path?}` | `{url, final_url, status, title, text, screenshot?, job_id}` |

- `workvm_browse` drives the preinstalled Chromium (Playwright) through the helper `/usr/local/bin/workvm-browse` in
  the VM: `action` `read` (default: open `url`, wait for load and `wait_ms`, return the visible text ≤ 32768
  characters), `screenshot` (full page PNG, brought into the workspace at `path`, default
  `.workvm/screenshots/<job_id>.png`, so the session can look at it with `Read`), `click` (`selector`, then read) and
  `fill` (`selector`, `text`, then read). `url` is http(s), ≤ 4096 characters; any address the VM can reach. Each call
  is a job (`kind: "browse"`) and kept like any run. For anything beyond these four actions the session writes its
  own Playwright script and runs it with `workvm_exec`.

- `command`: 1–65536 characters, run as `/bin/bash -lc <command>` as root, `HOME=/root`, working directory `cwd`
  (absolute, default `/workspace`), plus `env` (list of ≤ 64 `{name, value}`, names `^[A-Za-z_][A-Za-z0-9_]{0,127}$`,
  values ≤ 32768 characters). `stdin` (≤ 1 MiB text) is given to the command, else stdin is empty. stdout and stderr
  go interleaved into one output file (it grows on the VM's own disk).
- `timeout_seconds` 1–604800 (absent: no limit), `wait_seconds` 0–30 (default 30), `sync` `"to_vm"` (default) or
  `"none"`.
- Job object: `{job_id, command (first 256 characters), state: "running"|"exited"|"killed"|"timed_out", exit_code
  (integer or null), output, output_start, output_bytes, duration_ms, synced?}`. `output` is UTF-8 (invalid bytes and
  NUL become U+FFFD) of at most 32768 bytes: for `workvm_exec` and `workvm_output` without `offset` the tail of the
  output, else the bytes from `offset` (at most `limit`, default 32768). `output_start` is the byte offset of the
  returned text, `output_bytes` the total so far.
- Background work: `wait_seconds: 0` returns at once; processes started in the background (`&`, `nohup`, `setsid`)
  survive the end of the job (`KillMode=process`); `systemctl` services are real services of the VM.
- `workvm_kill`: `signal` `TERM` (default), `INT`, `HUP` or `KILL` to every process of the job.
- `workvm_status`: `workvm` null or `{workvm_id, state, scope, size, cpus, memory_mib, disk_gib, created_at,
  last_used_at}` and the last 50 jobs `{job_id, command, state, exit_code, started_at, duration_ms}`.
- `workvm_sync`: `to_vm` mirrors the workspace (or `path` below it) into `/workspace`: changed files only (SHA-256
  manifest of the last mirror of this session), deletions applied, files the VM created and the workspace never had
  are kept (build outputs, `node_modules`). `from_vm` needs `path` (a directory of the workspace): the files of
  `/workspace/<path>` in the VM – without `.git` directories, git-ignored files (`.gitignore` rules, also without a
  repository), links, special files and the protected names of 05 §5.3 – are merged into the workspace's `path`:
  the runner exports the workspace's `path`, overlays the VM's files, applies deletions only for files the mirror had
  pushed and the VM removed, keeps every workspace file that was excluded on the VM side, re-packs one canonical tar
  on the host and imports it (`replace=true`). The answer counts added/changed (`files`), `deleted` and `skipped`.
  Limits of the guest import (512 MiB, 100 000 entries) apply.
- Errors (`tool_error`): no VM → `not_found` ("No work VM: request one with workvm.request."); VM starting or
  stopped → the call starts it and waits (counts against `wait_seconds`; still starting → `unavailable`); unknown job
  → `not_found`; invalid input → `params_invalid`; runner without work VM support → `unavailable`.
- A stopped VM (idle) starts automatically on the next `workvm_exec`, `workvm_output` or `workvm_sync`.

## 4. Work image (`images/workvm.yaml`, `images/workvm/`)
- Lima template: `vmType: vz`, `arch: aarch64`, the pinned Ubuntu 24.04 cloud image of `images/versions.json`,
  `mounts: []`, `containerd {system:false,user:false}`, `ssh {overVsock: true, forwardAgent: false, forwardX11:
  false, loadDotSSHPubKeys: false}`, `portForwards: [{guestIP: "0.0.0.0", proto: any, ignore: true}, {guestIP:
  "127.0.0.1", proto: any, ignore: true}]`, no `param`, no credential.
- Provisioning (`images/workvm/provision.sh`, once at image build, guard `/var/lib/workvm-image.done`): sshd on vsock
  port 22 (`sshd-vsock.socket`), `build-essential git curl wget ca-certificates unzip zip jq python3 python3-venv
  python3-pip pipx docker.io docker-compose-v2 sqlite3 ripgrep fd-find tmux htop`, Node LTS from the pinned tarball,
  Playwright with Chromium and its system dependencies (stage c), the helper `/usr/local/sbin/workvm-job` (§5),
  `/workspace` (root 0755). Nothing is restricted for root.
- Build: `agent-runner workvm image build` (work account) → `limactl create` + `start` → provision → smoke (`docker
  run --rm hello-world` skipped offline, `python3 --version`, `node --version`, `workvm-job selftest`) → stop →
  rename `<prefix>wimg-<yyyymmdd>-<6 hex>`; record `runner.workvm_image` `ready`. Clones are APFS clones.

## 5. Jobs inside the VM (`workvm-job`)
The runner runs every command over Lima's SSH (vsock) as `sudo /usr/local/sbin/workvm-job …`:
- `start ID CWD TIMEOUT` (command and env as JSON on stdin) → `systemd-run --unit=workvm-job-ID --collect
  -p KillMode=process [-p RuntimeMaxSec=TIMEOUT] … workvm-job _run ID`; `_run` runs `bash -lc` in the job's process
  group, writes `/var/lib/workvm/jobs/ID/{output,state.json}`.
- `wait ID OFFSET LIMIT WAIT` → one JSON line `{state, exit_code, output_bytes, output_start, duration_ms}` followed by
  the raw output bytes (blocks until new output, the end of the job or `WAIT`).
- `kill ID SIGNAL`, `list`.
The VM is untrusted: the runner caps every answer it reads (header line ≤ 4 KiB, body ≤ `LIMIT`), validates the header
against a closed schema and treats any other answer as `unavailable`.

## 6. The outside boundary (threat model `docs/platform/security/work-vm-threat-model.md`)
### 6.1 Slot accounts
- `AGENT_RUNNER_WORKVM_SLOTS` (default 0 = no work VMs; production e.g. 24) macOS accounts `<prefix>wvm00` …,
  created by `agent-runner workvm install` (root): no login shell, hidden, own home `/var/agent-workvm/<account>`
  (0700) holding the Lima home `lima/` (short socket paths), on the APFS volume `AgentWorkVM` with a quota
  (`WORKVM_VOLUME_QUOTA_GIB`) so work VMs can never fill the device's main volume. `_config/` of each Lima home
  belongs to the slot account (0700) like in any Lima home: Lima writes `_config/networks.yaml` at the first start
  (decision HOT-18, replacing the root-owned `_config` of KOR-29 M3).
- The runner runs `limactl` for a slot only as `sudo -n -u <account> /usr/local/libexec/agent-runner-workvm-limactl
  …`: the root-owned wrapper refuses every other account and sets `HOME`, `LIMA_HOME` and `PATH` itself;
  `/etc/sudoers.d/agent-runner-workvm` allows exactly that wrapper as the slot accounts and, as root, the five
  read-only `pfctl` commands of the boundary check.
- Each slot has a pinned Lima SSH port `WORKVM_SSH_PORT_BASE + slot` (default base 47200, `ssh.localPort` in the
  instance). A work VM of a slot always lives in that slot's Lima home; a slot holds at most one work VM.

### 6.2 Host packet filter (pf anchor `com.apple/000.agent.workvm`)
The main pf ruleset is **never loaded or changed** by the platform: the device's Cloudflare client hooks its own
anchor `com.cloudflare.warp_svc` into the running main ruleset, and loading `/etc/pf.conf` would drop it (operations
finding 1, decisions KOR-29 #22). The work VM rules live in a child of Apple's anchor, `com.apple/000.agent.workvm`: the
macOS main ruleset evaluates `anchor "com.apple/*"`, i.e. every child of `com.apple`, before the client's anchor, and
a child is loaded with `pfctl -a com.apple/000.agent.workvm -f FILE` alone. `/etc/pf.conf` stays untouched.
`agent-runner workvm install` (root) writes the fail-closed anchor `/etc/pf.anchors/agent.workvm`, the template
`agent.workvm.up` with the placeholder `@EGRESS@`, the root-owned watcher `agent.workvm.watch` and the LaunchDaemon
`swiss.dfx.agent-runner.pf` (`KeepAlive`) that runs it, and loads the fail-closed anchor. Per slot account (UID `U`,
SSH port `P`), with `<workvm_blocked>` the table of this-network, loopback, private, CGNAT, link-local, multicast,
documentation, benchmark and reserved ranges (IPv4 and IPv6) and `utunN` the Cloudflare client's tunnel interface:
```
pass  out quick on lo0 proto tcp from 127.0.0.1 to 127.0.0.1 port P user U flags S/SA keep state
pass  out quick on lo0 proto tcp from 127.0.0.1 port P to 127.0.0.1 user U no state
block return out quick on lo0 proto { tcp udp } user U
block return out quick proto { tcp udp } from any to <workvm_blocked> user U
block return out quick proto tcp from any to any port 25 user U
pass  out quick on utunN proto { tcp udp } from any to any user U keep state \
      (if-bound, max 4096, max-src-conn 2048, max-src-conn-rate 2000/10)   # only while the client is connected
block return out quick user U
```
Every rule names a slot account: the device's own traffic (runner, hub, remote access) and the client's own rules are
never affected, and there are no device-wide rules (ICMP of the device is governed by the client's default deny).
Because our anchor is evaluated first and decides every TCP and UDP packet of a slot account with a `quick` rule, the
client's `pass out quick … to <private network>` never applies to them. ICMP is not matched by pf's `user`, and the
client passes ICMP to the private networks; the boundary for ICMP is that no slot process sends it: Lima's user
network (gvisor-tap-vsock) does not forward a VM's ICMP out of its virtual network (it may fake echo replies).
`egress-check.sh --vm` proves it per device (`vm_lan_icmp`, with a documentation address to detect faked replies). The anchor is named `000.agent.workvm`: pf evaluates
the children of `com.apple/*` in name order, and ours must be the first (Apple ships `100.…`, `200.…`, `250.…`). The pass rules keep their states `if-bound`,
so a flow never continues on the physical interface after a disconnect. No bandwidth shaping yet (decisions #20), no
global rate cap shared by all VMs, no destination logging. The host's own addresses are reached over lo0 and therefore
refused address-independently; private ranges are in the table.
- `agent-runner workvm install --plan` prints every step (files with mode and hash, commands) and whether the hook is
  present, changes nothing. `install` refuses a device whose running main ruleset has no `anchor "com.apple/*"` in
  front of the client's anchor and every `pass quick` (`workvm_firewall_unhooked`).
- `agent-runner workvm uninstall [--keep-volume]` stops the watcher, flushes the anchor, removes every file of
  `install` (and the lines an earlier installer put into `/etc/pf.conf`, file only), stops the slot accounts'
  processes, deletes the accounts, the group, the wrapper, the sudoers rule and (unless kept) the volume, and then
  checks that nothing is left (`result=ok`, exit 0).
- `agent-runner workvm firewall check` (and the runner, through `sudo -n` with exactly the read commands the sudoers
  file allows) requires pf enabled, the anchor rules equal to the rendered text for the live egress interface or for
  none, the hook as the first `anchor` line with no `pass quick` before it (`not_hooked` otherwise), our anchor as
  the first child of `com.apple` (`not_first`), no `set skip on lo0`, and the table.

### 6.3 Egress through the device's Cloudflare client (addendum 3)
- The operator installs and enrolls the Cloudflare One client on the device (managed preferences, service token,
  `service_mode warp`, `auto_connect`); the platform never handles its credential. Device traffic and everything the
  slot accounts send leaves with Cloudflare's shared egress addresses, never the site's address. No dedicated
  address, no Mesh node, no gateway VM.
- The watcher runs every 2 s and checks that the boundary is **effective**: pf enabled (`pfctl -E` when it is not -
  never `-d`), the hook `anchor "com.apple/*"` as the first `anchor` line of the running main ruleset with no
  `pass quick` before it, the anchor's rules loaded, and our anchor the first child of `com.apple` (`unordered`
  otherwise, handled like a missing hook). Without an effective hook (a component replaced the main ruleset) it kills
  every process of the slot accounts at once - their VMs with them - writes `unhooked` to
  `/var/run/agent.workvm.state` and repeats that every round until the hook is back; it never loads the main ruleset
  itself. With the hook: `/usr/local/bin/warp-cli --accept-tos status` contains `Status update: Connected` and
  `route -n get <resolver>` names a `utunN` → load the anchor with the pass rule on that interface; otherwise load the
  fail-closed anchor; on every change, when the anchor is empty, and every minute; state `ok <utunN or ->`. When the
  client disconnects or updates itself, the routes return to the physical interface and the pass rule stops matching
  at once (fail-closed does not depend on the watcher); after the reconnection the watcher opens again by itself.
- Rehearsal: `images/workvm/host-rehearsal.sh` (root, a test device with the client) runs plan, install of one slot,
  the checks, a disconnect/reconnect and a restart of the client's service, and the uninstall, compares the state
  with the state before, and uninstalls by itself after a deadline when the operator's session breaks.
- The work VMs resolve through Cloudflare's public resolvers (`.dns` of the clone), through the same tunnel; the host
  resolver is off (it would ask the site's resolver when the client is down).
- `images/workvm/egress-check.sh --account <slot> --site-ip <site address> [--vm NAME] [--disconnect-test]` (root,
  operator) checks on the device: client connected, egress rule loaded, the slot leaves with a Cloudflare address
  (Cloudflare trace, ipify IPv4/IPv6, HTTP/3), never the site address; port 25, the LAN gateway, the host's LAN
  address and the host loopback are blocked; with `--disconnect-test` the slot has no internet while the client is
  disconnected and gets it back after reconnecting. It also checks pf enabled, the hook in front of the client's
  anchor, the client's anchor still hooked, and from inside a VM a 50 MB transfer (the tunnel's MTU is 1300).

### 6.4 Fail-closed and test mode
- Outside `TEST_MODE`, `workvm.request` succeeds only when `firewall check` passes; otherwise `workvm_unavailable`.
  The daemon task checks every minute and tears down every live VM when the check fails (`guard_pass`).
  Without the client the check still passes (fail-closed anchor) and the VM runs without internet. A failing check
  while work VMs run stops them (teardown, reason `failed`) – the host filter is the boundary to the device and the other
  VMs, not only to the internet.
- `TEST_MODE` (hermetic tests and the build device): slots may be the runner's own account (`WORKVM_SLOTS_SELF=1`),
  no pf, internet through Lima's user network directly – refused by the runner outside `TEST_MODE`.

### 6.6 Known limit of the per-account filter
Known limit of the per-account filter (rehearsal of 2026-11-24, decisions #34/#35): pf matches a slot by the
account of the packet's socket and does not attribute every packet; a packet it does not attribute matches none of
the anchor's rules and passes. A continuous prober reached, intermittently and in every client state, a listening
port of the device over its loopback and over its site address. A program in a work VM can therefore reach, with
single connections, the device's loopback services, the site network and - while the Cloudflare client is
disconnected - the internet with the site's address. The operator decided on 2026-11-24 to run work VMs on this
filter all the same; a boundary by interface is left to a later package.

### 6.5 Template settings
`mounts: []`, no port forwards (`portForwards` ignore all; `guestIPMustBeZero: false`), `ssh.overVsock: true`,
`ssh.loadDotSSHPubKeys: false`, `forwardAgent: false`, `propagateProxyEnv: false`, `hostResolver.enabled: false`,
`dns:` Cloudflare's two public resolvers (set per clone), no `param`; the clone gets fresh SSH host keys and
machine-id at first boot (cloud-init new instance-id).
## 7. Capacity, lifetime, restart (temporary VMs, ADR-059 decisions 8 and 11)
- RAM budget of 05 §3.4 shared: a work VM holds `memory_mib` while `starting|running|deleting`.
- Queue: requests wait FIFO per device. An organization may hold at most `WORKVM_ORG_RAM_SHARE` (default 50 %) of
  the work VM RAM while others wait.
- **Teardown** (`limactl delete -f`, row `deleted`, `workvm.state` `deleted` with reason and runtime): `released`
  (`workvm.release`), `idle` (no call and no running job for `WORKVM_IDLE_SECONDS`, default 1800), `session_ended`
  (scope `session`: its session reached a final state or `stopped`; scope `task`: no session of the task is in an
  active state any more), `task_closed` (the device state names the session's task closed, `task_closed: true`: the
  reconcile step `workvm` deletes every VM of the task at once, KOR-43; when the stop hint ends the task's session
  first, its VMs go with the session, `task_closed` when the runner already knew the task closed, else `session_ended`), `failed` (start failure, unhealthy helper),
  `restart` (the runner started and found it: no resume, KONZEPT idea 44), `run_ended` (the VM of one test run, §12),
  `credit_exhausted` (a run VM whose run the credit ended), `browser_closed`, `browser_idle` and `vm_lost` (browser VMs,
  §13.2), `session_paused` (a stopped VM that stood
  `WORKVM_IDLE_SECONDS` or gave its slot to a waiting request). Runs still `running` at teardown are recorded with
  state `lost`. The teardown is shielded from cancellation and waits (at most 300 s) for the log captures of the VM.
  When Lima fails to delete, the row stays `deleting` (its slot is not given back) and the daemon task's sweep deletes
  it again every minute; the sweep also deletes own `wvm-` instances without a live row or pool entry.
- **Paused sessions** (KOR-43, ADR-080 §4): a session the guest reports `suspended` stops the VMs it works in
  (`limactl stop`, state `stopped`, `workvm.state` `stopped` with reason `session_paused`) – its session VM, and the
  task VM when no other session of the task is active – unless a job still runs in it. On a platform device the
  reconcile step `workvm` stops every running work VM of an organization whose credit is used up (reason
  `credit_exhausted`). A stopped VM holds no RAM and costs no runtime; the next call or `workvm.request` of the session
  starts it again (state `created` again; without credit: `credit_exhausted` / `unavailable`), or deletes it and gets a
  new one when its RAM does not fit. It is deleted after `WORKVM_IDLE_SECONDS` stopped.
- Implicit request: `workvm_exec` and `workvm_browse` of a session without a VM create a standard task VM first
  (same limits, queue and boundary checks as `workvm.request`; announced by the `workvm.state` `starting` event, so
  the panel counts down, and recorded by the `created` event with `implicit: true`).
- Credit (KOR-43): on a platform device an organization whose credit is used up gets no new work VM or run VM
  (`credit_exhausted`). The tool waits within its `wait_seconds`; a queued request answers `unavailable` with "capacity
  queue" text and keeps the place.
- Disk (built): with `WORKVM_DISK_QUOTA_GIB` set (to the `--quota-gib` of the installation; 0 = not checked), the
  allocated disk of all live work VMs plus 50 GiB per pool entry plus the request must stay within it; a request
  that does not fit waits like RAM, and a ready pool entry gives its disk back first; the pool starts an entry only
  when two standard disks still fit.
- The organization RAM share is not built (decisions #31): one FIFO queue per device; the per-task, -organization
  and -owner VM counts bound one organization.
- Warm pool (stage b, built): `WORKVM_POOL_SIZE` (default 1, 0…16) started, unassigned **standard** clones of the
  current image in `runner.workvm_pool` (`starting` → `ready`). A standard request with an empty queue takes the oldest
  `ready` entry: one transaction deletes the entry and inserts the `runner.workvm` row with the same Lima name and
  slot (`running`), then one helper call (`list`, 5 s) confirms it; an unhealthy entry is deleted (`failed`) and the
  request continues on the cold path. Runtime starts at the assignment (pool time is device overhead, not attributed).
  The daemon task refills at once after an assignment and every minute: never while a request waits, never when less
  than two standard VMs of RAM are free (the last one stays for a cold request), never without a ready image and a
  passing boundary. Entries of an older image are deleted. A waiting standard request at the head of the queue takes a ready
  entry; a request that finds no slot or RAM deletes the oldest ready entry first, but only when the RAM of all ready
  entries would make it fit. Boundary failure and restart delete the pool. Larger sizes are always cloned on demand.
  Log lines `runner.workvm pool_ready slot= start_ms=` (cold start time of the device) and `created … pooled=0|1`.
- Scale: one device holds `floor((RAM − reserve − workgroup VMs) / 8 GiB)` standard work VMs (512 GiB, 32 GiB reserve:
  60); more parallel agents need more execution devices (ADR-059 decision 11).

## 8. Data and events
- Runner migration 0012: `runner.workvm (id PK, lima_name UNIQUE, slot, scope, task_id, session_id NULL, org_id,
  wg_id, owner_id, size, cpus, memory_mib, disk_gib, image, state, created_at, last_used_at, stopped_at, deleted_at,
  started_at, ended_at, delete_reason)`; unique partial indexes for the live task VM, session VM and slot;
  `runner.workvm_mirror`; `runner.workvm_image`. Runtime = `ended_at − started_at` (seconds, to the millisecond).
  No destination log.
- Event `workvm.run` (scope `wg`, author the device, right `device_assigned_wg_session`, header `session` and `task`
  set): `{workvm_id, job_id, kind: "exec"|"browse", command (≤ 4096 characters), state: "running"|"exited"|"killed"|
  "timed_out"|"lost", exit_code, started_at, ended_at (null while running), duration_ms, output (tail ≤ 65536
  characters), output_bytes}`; written when the job starts and when the runner sees it final (a call, the idle pass
  or the teardown).
- Event `workvm.state` (same scope, author and right): `{workvm_id, state: "starting"|"created"|"stopped"|"deleted",
  scope, size, cpus, memory_mib, disk_gib, implicit, reason (deleted and stopped), started_at, ended_at, runtime_ms}`;
  `starting` announces an implicit start, `stopped` a paused session's VM (KOR-43). Run VMs write none (§12).
- `workvm.run` of a job whose output is longer than its tail (64 KiB) carries `log` `{object_id, bytes, stored_bytes,
  truncated}`: the runner keeps the complete output (at most `LOG_MAX_MIB`, its last part) as an encrypted log object
  (§12.3) and writes the final event once it is stored.
- Both kinds are new in `enums/event-kinds.json` (hub before runner).

## 9. Harness
RULES.md says (KOR-43): tests, lint and formatting checks go through `test.run` (a fresh VM with a copy of the
workspace; bring files changed in the work VM back with `workvm_sync from_vm` first); everything else runs in the work
VM without asking. Further: the session never executes anything in its own sandbox; for everything that needs execution it has a
work VM – request it with `workvm.request`, then use the `workvm_*` tools; root, internet, installing, services,
browser are allowed there without asking; it must never say it cannot execute something; results come back with
`workvm_sync from_vm` and leave through the GitHub operations; the VM holds no platform credentials; what the
session puts there itself is its own business (addendum 1).

## 10. Web (agent-core)
- Operation cards and i18n for `workvm.request` and `workvm.release` (stage a).
- Chat items for `workvm.run` (command, state, exit code, duration, output with "show all") and `workvm.state`
  (created/deleted, size, runtime) in the session's timeline; the session header shows the work VM runtime of the
  session and of the task (sum of `deleted` events plus the running VM).
- Session panel "Work VM" (stage d, `WorkvmPanel`, between the session header and its tabs, also on phones): the
  state of the session's VM – `none`, `starting` (a `workvm.request` operation row of the session is open and no VM
  of the session runs: countdown to the 10 s target with ring and digits, "delayed" after it), `running`, `deleted`
  with its reason –, size (CPU, RAM, disk), the runtime of this VM (ticking every second while it runs), a note on
  how it ends, and the session's runs (latest `workvm.run` per job, newest first, with state, exit code, duration and
  the stored output tail) in a collapsed list. An implicit request (`workvm_exec` without a VM) has no operation
  row, so it shows no countdown; the panel switches to `running` with the `created` event.
- No release button (decision UI-1): operations are requested only by the session's device
  (`POST /v1/device/operations`); a user-initiated `workvm.release` needs a hub route, a right and a runner path for
  user-origin requests. Until then the user ends the VM by stopping the session (`session_ended`). Live CPU, RAM,
  disk and network of the VM belong to the monitoring view (section 7, device → VM → session).
- Monitoring (stage b, KOR-29 decisions MON-1…3): the runner samples every `starting`/`running` work VM each round as
  entity kind `workvm` with `session_id` (its own session, else the requesting session); the hub stores it with its
  device and session and lists it in `GET /v1/monitoring/overview` (`workvms`, for the readers of the session's
  workgroup, until 15 minutes after its last sample), in the series and the live `metrics` frames; the web shows it
  below its session (device → VM → session → work VM: CPU, RAM, disk, network, runtime, charts).

- Test runs (KOR-43): the chat item of `test.run` (state, kind, command, summary "Tests: 3 failed, 12 passed", exit code,
  duration, start time, tail) with "Open log"; the operation card of `test.run`; `workvm.run` items with `log` open the
  full log; the work VM panel lists the session's runs. The log viewer fetches chunks on demand (`GET
  /v1/tasks/{task}/logs/{object}` and `.../chunks/{index}`), opens them with the workgroup key, shows a window of lines
  and downloads the whole log as a file; it never renders the whole log at once. A `starting` event counts down like an
  open `workvm.request`; `stopped` is shown as "stopped – starts again on the next use".

## 11. Tests
- Unit/integration (100 % lines and branches): contracts, guest tools, runner area `workvm` with real PostgreSQL and
  a fake `limactl` plus a fake `workvm-job` (`tests/platform/runner/workvm/`), pf rendering and check, harness.
- Real VM (manual run on the build device, `tests/platform/runner/workvm/real_workvm.py`): image build, request,
  exec as root with internet, background service, sync both ways, idle stop and restart, release, start time.
- Attack suite (`docs/platform/atk/WVM.md`): from inside a work VM – host loopback (the host address of Lima's user network (`host.lima.internal`, every port), including the
  other slots' SSH ports), the host's own addresses, LAN, other VMs, the link-local metadata address, gvisor's
  user-network gateway services (gateway and host address, port 80), mail port 25, a search for credentials **placed by the platform**
  (environment, files, `/mnt/lima-cidata`; the session's own are its business), resource exhaustion (fork bomb, disk
  fill bounded by the volume quota), request abuse (size, count, foreign task), egress address = Cloudflare, never the
  site address.

## 12. Test runs (KOR-43, ADR-080)
Concept idea 27, interpretations A6, A8, A9: every test run is its own operation in its own throw-away VM; result, log
and exit code are kept outside the VM. The work VM stays unrestricted (E-08).

### 12.1 Operation `test.run`
- Params: `kind` (`lint`, `format`, `unit`, `integration`, `security`, `custom`), `command` (1–65536 characters, run
  as `bash -lc` as root like `workvm_exec`), `cwd` (absolute, default `/workspace`), `timeout_seconds` (1–86400,
  default 3600), `size` (`standard`, default, `large`, `xlarge`; capped by `WORKVM_MAX_SIZE`).
- Result: `run_id` (`tr-<12 hex>`), `state` (`passed` = exit code 0, `failed` = another exit code, `timed_out`,
  `cancelled`, `error`), `exit_code`, `duration_ms`, `ready_ms` (request to a running VM), `summary` (`{passed,
  failed, skipped, errors}` from the last recognised summary line of the tail: pytest, vitest/jest, go test, cargo
  test, mocha, ESLint/ruff problems; else null), `log` (`log_ref` or null without output), `tail` (last ≤ 8192
  characters), `reason` (`credit_exhausted`, `restart`, `start_failed`, `transfer_failed`, `vm_lost`, `cancelled`, or
  null).
- The operation fails only when nothing ran: `credit_exhausted`, `limit_exceeded` (size cap; the organization and
  owner VM limits – a run VM does not count against the per-task limit), `workvm_unavailable`, `timeout` of the
  capacity queue, `upstream_error` (workspace not readable, VM did not start; retryable). Once the command started the
  operation succeeds with the run's state.
- Duplicates (§11 of the concept): a request of the same session with the same `kind`, `command`, `cwd`, `size`,
  `timeout_seconds` and workspace digest (SHA-256 over the export manifest's paths and hashes) while a run is queued,
  starting or running joins it (same `run_id` and result). A finished run is never joined.

### 12.2 The run VM
- A `runner.workvm` row of scope `run` (`session_id` = the requesting session): warm pool, queue, slot, RAM, disk,
  boundary (§6, including the known limit §6.6), monitoring (device → VM → session) and billing as any work VM.
- The workspace export is mirrored into `/workspace` (all files of the export). The command installs what it needs;
  the image carries the toolchains. No dependency cache across runs yet.
- The runner polls the job every 5 s. The time limit is enforced on the runner's clock (the job is killed with `KILL`,
  `timed_out`), additionally by `RuntimeMaxSec` in the VM. A session that ends (`session_ended`), a task that closes
  (`task_closed`) and credit that runs out (`credit_exhausted`) end the run `cancelled` within one poll.
- Always, in a `finally` shielded from cancellation: the tail is read, the complete output stored as a log (§12.3,
  at most 300 s), the VM deleted (`run_ended`, or the cancel reason), then the row and the final event written.
- `runner.test_run` (migration 0022) keeps every run (`running` until it ended, then its result). After a restart every
  run still `running` ends `error` (`restart`) with its final event; the VM goes with the work VM reconciliation and
  the pipeline fails the operation found `executing` (`timeout`): a run is never executed twice.
- Log line `runner.testrun ready run_id= ready_ms= workvm_id=` (the start time of a run VM, A9) and `runner.testrun
  ended run_id= state= exit_code= output_bytes= logged=`.

### 12.3 Logs
- The output is read from the VM as raw bytes (pieces of 1 MiB, `workvm-job wait` with offset) and sealed into the
  context object cache as an object of kind `log` (04-crypto.md §4.12): chunks of 256 KiB uncompressed output under
  `k_log` of the current epoch, manifest with `ref` = the run or job ID, signed by the device. `LOG_MAX_MIB` (default
  256) per log; a longer output keeps its last part (`truncated`).
- Upload with the context upload loop (`PUT /v1/device/contexts/{object}/chunks/{index}` with `kind: "log"`, then the
  commit); a log may belong to a subagent session. A log the hub expired is uploaded again under the same ID; a log
  the hub refuses for good leaves the cache; a log leaves the cache once the hub holds it and is never evicted before.
- The hub keeps `AGENT_HUB_LOG_KEEP` (default 500) complete logs per task and `AGENT_HUB_LOG_ORG_BYTES` (default 100
  GiB) of logs per organization apart from the checkpoints. Members with `task:read` read a log with `GET
  /v1/tasks/{task}/logs/{object}` and `.../chunks/{index}` and open it in the browser or `agent-platform test log`.
  The hub sees sizes, never content.

### 12.4 Event `test.run`
Scope `wg`, author the device, right `device_assigned_wg_session`, header `session` and `task` set: `{run_id,
operation_id, kind, command (≤ 4096), cwd, size, state, exit_code, started_at, ended_at, duration_ms, ready_ms,
summary, log, tail, reason}`, written when the run starts (`running`) and when it ends.

### 12.5 Tests
`tests/platform/runner/workvm/test_testrun.py` and `test_lifecycle.py` (fake Lima, fake helper, real PostgreSQL),
`tests/platform/runner/contexts/test_logs.py`, the hub's log routes and pruning, the crypto vectors, the web log
reader and viewer, and the hermetic E2E scenario of test runs (red run → change → green run → pull request; a log over
1 MiB; VMs gone after failure and time limit; restart; duplicate; credit; closing the task).

## 13. Browser sessions (KOR-44, ADR-085)
Concept idea 28, idea 44, interpretation A6: the browser is requested through the platform; a temporary Ubuntu VM with
Chromium, driven through controlled tools, every action recorded, the picture live for watchers, the VM removed after
use. The work VM and its free browser (`workvm_browse`) stay as they are and unwatched (E-09).

### 13.1 Operations
| Type | Params | Result | Dedupe |
|---|---|---|---|
| `browser.open` | `{url?: http(s) ≤ 4096, idle_seconds?: 60–3600}` | `{browser_id, created, state: "open", workvm_id, ready_ms, idle_seconds, url, title}` | none (idempotent: the open browser session of the session is returned with `created:false`; a `url` is opened in it) |
| `browser.close` | `{}` | `{closed, browser_id (null when none), actions, reason}` | none (idempotent: nothing open is `closed:false`) |

- `browser_id` = `br-<12 hex>`. One open browser session per session; a subagent gets its own (its session).
- `idle_seconds` default `BROWSER_IDLE_SECONDS` (600). `ready_ms`: request to a browser that answers.
- The operation fails only when no browser came up: `credit_exhausted`, `limit_exceeded` (organization and owner VM
  limits; a browser VM does not count against the per-task limit), `workvm_unavailable`, `timeout` (capacity queue),
  `upstream_error` (VM or browser did not start; retryable). A `url` that does not load still opens the browser.
- `reason` of `browser.close`: `browser_closed`, or the reason it had already ended with (then `closed:false`).

### 13.2 The browser VM
- A `runner.workvm` row of scope `browser` (runner migration 0028; `session_id` = the requesting session): pool,
  queue, slot, RAM, disk, boundary (§6 including §6.6), monitoring and billing as a run VM (§12.2), size `standard`.
- `runner.browser_session (browser_id PK, operation_id, session_id, task_id, org_id, wg_id, workvm_id, state open|
  closed, seq, idle_seconds, url, ready_ms, opened_at, last_call_at, closed_at, close_reason)`; at most one `open` row
  per session (unique partial index).
- Teardown reasons (new): `browser_closed` (`browser.close`), `browser_idle` (no browser call for `idle_seconds`),
  `vm_lost` (the VM stopped answering; a new one follows on the next call); and the existing `session_ended`,
  `task_closed`, `credit_exhausted`, `restart`, `failed`. The session's end, the task's close and the credit stop
  close the browser session with that reason; a restart closes every open browser session (`restart`, no resume). The
  VM is deleted in a `finally` shielded from cancellation; a failed deletion stays `deleting` for the sweep.
- Failure (idea 44): a call whose helper does not answer (or whose VM is gone) deletes the VM (`vm_lost`), writes
  `browser.state` `restarted`, obtains a fresh VM, and runs the call there; its answer carries the `notice` "The browser
  VM failed and was started again; open pages, cookies and sign-ins are gone." `seq` continues.
- Log lines `runner.browser opened browser_id= ready_ms= workvm_id=`, `runner.browser restarted browser_id=`,
  `runner.browser closed browser_id= reason= actions=`, `runner.browser live browser_id= frames= bytes=`.

### 13.3 Helper in the VM (`/usr/local/bin/workvm-browser`, work image)
Node with the image's Playwright; the runner runs it over Lima's SSH as `sudo /usr/local/bin/workvm-browser …`:
- `serve` – the daemon (transient unit `workvm-browser`, started by `call` when absent): one persistent Chromium
  context (headless, viewport 1280×800, profile `/var/lib/workvm-browser/profile`, downloads
  `/var/lib/workvm-browser/downloads`), dialogs accepted and reported, listening on `/run/workvm-browser.sock` (root,
  0600).
- `call` – reads one JSON request `{tool, arguments}` on stdin, prints one JSON line (the answer of §13.4 without
  `browser_id`/`seq`, plus `image` and `file`); at most 30 s.
- `stream FPS QUALITY WIDTH` – prints the screencast of the active tab (Chromium `Page.startScreencast`, a frame only
  when the page changed): per frame one header line `{"bytes":N,"width":W,"height":H}` and N JPEG bytes; ends with
  stdin.
- `selftest` – starts the daemon, opens `about:blank`, prints `{"ok":true}`.
The runner caps every answer (header line ≤ 4 KiB, call answer ≤ 4 MiB, frame ≤ 512 KiB), validates it against a
closed schema and treats anything else as a failed helper (`vm_lost`).

### 13.4 Tools (03-api.md §8.1)
Forwarded like the work VM tools (§3: journal `workvm.call`, no read audit, visible as `session.tool`). Each needs an
open browser session (else tool error `not_found`: "No browser open: request one with request_operation type
browser.open."). `target` is a `ref` of the last snapshot (`e12`) or a Playwright selector (1–1024 characters).

| Tool | Input |
|---|---|
| `browser_navigate` | `{url?, history?: "back"\|"forward"\|"reload", query?: 1–512, wait_until?: "load"\|"domcontentloaded"\|"networkidle"}` – exactly one of `url`, `history`, `query` |
| `browser_snapshot` | `{}` |
| `browser_click` | `{target, double?: bool, button?: "left"\|"right"\|"middle"}` |
| `browser_type` | `{target, text: 0–65536, submit?: bool, clear?: bool (default true)}` |
| `browser_select` | `{target, values: 1–64 strings ≤ 1024}` |
| `browser_press` | `{key: 1–64}` (`Enter`, `Control+A`, …) |
| `browser_scroll` | `{target?, dy?: −100000…100000 (default 600)}` (with `target`: into view) |
| `browser_wait` | `{text?: 1–1024, gone?: 1–1024, ms?: 0–30000, timeout_ms?: 0–30000 (default 10000)}` – at least one of `text`, `gone`, `ms` |
| `browser_tabs` | `{action: "list"\|"new"\|"select"\|"close", index?: 0–63, url?}` |
| `browser_download` | `{target?, url?, path?: workspace path}` – exactly one of `target`, `url`; default path `.browser/downloads/<file name>` |
| `browser_screenshot` | `{path?: workspace path, full_page?: bool}` – PNG, default `.browser/screenshots/<browser_id>-<seq>.png` |

Answer of every tool: `{browser_id, seq, ok, error, notice, url, title, tab, tabs: [{index, url, title, active}],
snapshot, file}` – `error` and `notice` text or null; `snapshot` the accessibility snapshot of the page after the
action (lines `- <role> "<name>" [ref=eN]` with `value=`, `checked`, `href=` and headings and visible text, at most
24000 characters, `[…truncated]` at the end when cut); `file` `{path, name, bytes}` or null (download, screenshot).
A failed action is `ok:false` with `error` (not a tool error). `browser_navigate {query}` needs the switch
`BROWSER_SEARCH_URL` (a URL template with `{q}`; empty = off): while it is off the answer is `ok:false` with "Automated
search is not switched on: open a search page with url, like a person."

### 13.5 Events
- `browser.action` (scope `wg`, author the device, right `device_assigned_wg_session`, header `session` and `task`
  set), one per tool call: `{browser_id, seq, tool, action (≤ 64), target (≤ 1024, or null), text (≤ 1024, or null;
  `"••••"` for a password field), url (≤ 8192), title (≤ 1024), tab, ok, error (≤ 2048 or null), notice (≤ 1024 or
  null), at, duration_ms, image, image_omitted}`; `image` `{type: "image/jpeg", width, height, data (b64u, ≤ 160
  KiB decoded)}` or null; `image_omitted` `"too_large"`, `"unavailable"` or null. A **watched kind** (§13.6).
- `browser.state` (same scope, author, right): `{browser_id, operation_id, state: "starting"|"open"|"restarted"|
  "closed", workvm_id, reason, url, actions, ready_ms, idle_seconds, opened_at, closed_at}`; `starting` before the VM
  is obtained (countdown), `open` when the browser answers, `restarted` after a failure, `closed` with its reason.

### 13.6 Watched kinds and the live picture
- **Watched kinds** (one list at the hub, `agent_hub.events.watched`): `session.terminal` (frame `terminal`, stream
  parameter `terminal`) and `browser.action` (frame `browser_action`, parameter `browser`). A watched kind is stored
  like any event but returned by `GET /v1/scopes/{scope}/events` only when `kinds` lists it, never by device pulls,
  and sent live only to streams that watch the event's session with that parameter (replay included).
- **Watching**: `GET /v1/stream?browser=<session_id>` (repeatable, at most 3, `task:read` of the session, audit
  `read.stream.browser` once per stream) with `browser_kbps` 100–10000 (default 1000). Per hub process and watched
  session the lease `hub.browser_watch (session_id, instance, until, kbps)` is written when the first local stream
  starts watching and renewed every 10 s (`until` = now + 30 s); the first lease of a session sends the session's
  device the hint `browser {session_id}`, and the renewal sends it again every 10 s while the device has not
  posted. A stream that starts watching gets the stored latest frame at once (frame `browser_live`).
- **Frames**: `POST /v1/device/sessions/{session}/browser/live` (device signed; the session runs on the calling device
  in a workgroup it is assigned to, else `403 forbidden`) `{frame?}` → `{until, kbps}` (the latest lease and the
  largest `kbps` among valid leases) or `409 not_watched` (no valid lease; the runner stops streaming). A frame (≤ 512
  KiB ciphertext, 04-crypto.md §4.13) replaces `hub.browser_live` of the session (one row, unlogged) and is published
  as `browser_live {session_id, wg_id, frame}` through the bus as a reference (`{session_id, seq}`); every hub process
  reads the row and sends the frame to its streams watching that session. The hub keeps no other frame.
- **Runner**: on the hint it starts `workvm-browser stream` for the session's open browser session (none open: it
  ignores the hint), seals each frame, posts it when the byte budget allows (`kbps`, capped by
  `BROWSER_LIVE_MAX_KBPS`, at most 5 frames per second, JPEG quality 60, at most 1280 px wide), drops frames that do
  not fit the budget (the next change brings a new one) and posts an empty body when no frame came for 10 s. `409
  not_watched`, a closed browser session or a lost VM end the stream. No viewer: no stream, no frame, no bandwidth.

### 13.7 Web search (KOR-29 stage c)
Claude Code's own `WebSearch` (run by the model provider) is allowed for sessions whose model access is `api_key` or
`subscription` and stays denied for `platform`. A paid search interface and the automated query of a search engine
wait for the client's decision (F9); until then the switch `BROWSER_SEARCH_URL` is empty (§13.4).

### 13.8 Tests
`tests/platform/runner/workvm/test_browser*.py` (fake Lima, fake helper, real PostgreSQL), the helper's own test
(`tests/platform/images/test_workvm_browser.py` with a local page in Node), the hub's watched kinds, watch leases and
live route, the crypto vectors `contracts/vectors/browserframe.json`, the web record view (windowed, volume test) and
live view, `agent-platform browser`, and the hermetic E2E scenario E2E-66 (open a page of a local test server, sign in
through its form, download a file; the record and the pictures; the live picture on desktop and phone; VM gone after
`close` and after idle; a killed VM restarted on the next action with the record complete; zero-knowledge scan; a
foreign task's browser not watchable; the browser VM reaches neither the device nor the internal network).
