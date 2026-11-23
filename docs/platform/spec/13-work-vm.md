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
  (`WORKVM_VOLUME_QUOTA_GIB`) so work VMs can never fill the device's main volume. `_config/` of each Lima home is
  root-owned and read-only (no `override.yaml` by a compromised host agent).
- The runner runs `limactl` for a slot only as `sudo -n -u <account> /usr/local/libexec/agent-runner-workvm-limactl
  …`: the root-owned wrapper refuses every other account and sets `HOME`, `LIMA_HOME` and `PATH` itself;
  `/etc/sudoers.d/agent-runner-workvm` allows exactly that wrapper as the slot accounts and, as root, the five
  read-only `pfctl` commands of the boundary check.
- Each slot has a pinned Lima SSH port `WORKVM_SSH_PORT_BASE + slot` (default base 47200, `ssh.localPort` in the
  instance). A work VM of a slot always lives in that slot's Lima home; a slot holds at most one work VM.

### 6.2 Host packet filter (pf anchor `agent.workvm`)
`agent-runner workvm install` (root) writes the fail-closed anchor `/etc/pf.anchors/agent.workvm` (loaded at boot
through an `anchor "agent.workvm"` and a `load anchor` line in `/etc/pf.conf`), the template
`agent.workvm.up` with the placeholder `@EGRESS@`, the pipe file, the root-owned watcher `agent.workvm.watch` and
the LaunchDaemon `swiss.dfx.agent-runner.pf` (`KeepAlive`) that runs it. Per slot account (UID `U`, SSH port `P`),
with `<workvm_blocked>` the table of this-network, loopback, private, CGNAT, link-local, multicast, documentation,
benchmark and reserved ranges (IPv4 and IPv6) and `utunN` the Cloudflare client's tunnel interface:
```
pass  out quick on lo0 proto tcp from 127.0.0.1 to 127.0.0.1 port P user U flags S/SA keep state
block return out quick on lo0 proto { tcp udp } user U
block return out quick proto { tcp udp } from any to <workvm_blocked> user U
block return out quick proto tcp from any to any port 25 user U
pass  out quick on utunN proto { tcp udp } from any to any user U keep state \
      (max 4096, max-src-conn 2048, max-src-conn-rate 2000/10)          # only while the client is connected
block return out quick user U
```
plus a dummynet pipe per slot (500 Mbit/s each direction; configured, not yet fed by rules – decisions #20). After
the slot rules the anchor passes ICMP echo of the device only on `utunN` and drops it on every other interface but
lo0 (pf's `user` does not match ICMP); the pass rules keep their states `if-bound`, so a flow never continues on
the physical interface after a disconnect. There is no global rate cap shared by all VMs and no
destination logging. The anchor only matches the slot accounts: the device's own traffic (runner, hub, remote
access) is never affected. The host's own addresses are reached over lo0 and therefore refused address-independently;
private ranges leave through the physical interface (the client's split-tunnel excludes), where nothing passes.
`agent-runner workvm firewall check` (and the runner, through `sudo -n` with exactly the read commands the sudoers
file allows) requires pf enabled, the anchor rules equal to the rendered text for the live egress interface or for
none, the anchor referenced from the main ruleset before any `pass quick`, no `set skip on lo0`, and the table.

### 6.3 Egress through the device's Cloudflare client (addendum 3)
- The operator installs and enrolls the Cloudflare One client on the device (managed preferences, service token,
  `service_mode warp`, `auto_connect`); the platform never handles its credential. Device traffic and everything the
  slot accounts send leaves with Cloudflare's shared egress addresses, never the site's address. No dedicated
  address, no Mesh node, no gateway VM.
- The watcher runs every 5 s: `warp-cli status` contains `Status update: Connected` and `route -n get <resolver>`
  names a `utunN` → load the anchor with the pass rule on that interface; otherwise load the fail-closed anchor. It
  loads on every change and every minute (a flushed anchor comes back); a failed load is retried in the next round.
  When the client disconnects, the routes return to the physical interface and the pass rule stops matching at once:
  fail-closed does not depend on the watcher's timing.
- The work VMs resolve through Cloudflare's public resolvers (`.dns` of the clone), through the same tunnel; the host
  resolver is off (it would ask the site's resolver when the client is down).
- `images/workvm/egress-check.sh --account <slot> --site-ip <site address> [--vm NAME] [--disconnect-test]` (root,
  operator) checks on the device: client connected, egress rule loaded, the slot leaves with a Cloudflare address
  (Cloudflare trace, ipify IPv4/IPv6, HTTP/3), never the site address; port 25, the LAN gateway, the host's LAN
  address and the host loopback are blocked; with `--disconnect-test` the slot has no internet while the client is
  disconnected and gets it back after reconnecting.

### 6.4 Fail-closed and test mode
- Outside `TEST_MODE`, `workvm.request` succeeds only when `firewall check` passes; otherwise `workvm_unavailable`.
  The daemon task checks every minute and tears down every live VM when the check fails (`guard_pass`).
  Without the client the check still passes (fail-closed anchor) and the VM runs without internet. A failing check
  while work VMs run stops them (teardown, reason `failed`) – the host filter is the boundary to the device and the other
  VMs, not only to the internet.
- `TEST_MODE` (hermetic tests and the build device): slots may be the runner's own account (`WORKVM_SLOTS_SELF=1`),
  no pf, internet through Lima's user network directly – refused by the runner outside `TEST_MODE`.

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
  active state any more), `task_closed`, `failed` (start failure, unhealthy helper), `restart` (the runner started and
  found it: no resume, KONZEPT idea 44). Runs still `running` at teardown are recorded with state `lost`.
- Implicit request: `workvm_exec` and `workvm_browse` of a session without a VM create a standard task VM first
  (same limits, queue and boundary checks as `workvm.request`; recorded by the `workvm.state` `created` event with
  `implicit: true`). The tool waits within its `wait_seconds`; a queued request answers `unavailable` with "capacity
  queue" text and keeps the place.
- Disk: allocated disk of all work VMs ≤ the volume quota; a request that does not fit waits like RAM.
- Warm pool (stage b, built): `WORKVM_POOL_SIZE` (default 1, 0…16) started, unassigned **standard** clones of the
  current image in `runner.workvm_pool` (`starting` → `ready`). A standard request with an empty queue takes the oldest
  `ready` entry: one transaction deletes the entry and inserts the `runner.workvm` row with the same Lima name and
  slot (`running`), then one helper call (`list`, 5 s) confirms it; an unhealthy entry is deleted (`failed`) and the
  request continues on the cold path. Runtime starts at the assignment (pool time is device overhead, not attributed).
  The daemon task refills at once after an assignment and every minute: never while a request waits, never when less
  than two standard VMs of RAM are free (the last one stays for a cold request), never without a ready image and a
  passing boundary. Entries of an older image are deleted. A waiting request that finds no slot or RAM deletes the
  oldest pool entry first. Boundary failure and restart delete the pool. Larger sizes are always cloned on demand.
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
- Event `workvm.state` (same scope, author and right): `{workvm_id, state: "created"|"deleted", scope, size, cpus,
  memory_mib, disk_gib, implicit, reason (deleted only), started_at, ended_at, runtime_ms}`.
- Both kinds are new in `enums/event-kinds.json` (hub before runner).

## 9. Harness
RULES.md says: the session never executes anything in its own sandbox; for everything that needs execution it has a
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
