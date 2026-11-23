# 05 Runner: host daemon, VMs, sandbox, harness, hooks, sessions, model access, operations

> As built: where the implementation refines or deviates from this document, see the register
> [`spec-deviations.md`](../spec-deviations.md#05-runner) (DOC-01).

Status: binding. Components RUN (`agent`: `src/agent_runner/`), GST (`src/agent_guest/`) and SBX (`images/`,
`src/agent_guest/sandbox/`). Contracts referenced: `03-api.md` §6–§9, data `02-data-model.md` §4–§5, crypto
`04-crypto.md`. Decisions: ADR-008, -009, -010, -011, -012, -013, -015, -016, -017, -020, -021, -022, -024, -025.
Measured basis: Spike A (VM start), Spike B (sandbox), Spike C (model access). Where a spike prototype and this
document differ, this document wins; §4.6 lists the spike defects that MUST NOT be carried over.

## 1. Host daemon `agent-runner`

### 1.1 Process and platform

- Python 3.12, one asyncio process, dependencies: `httpx`, `websockets`, `psycopg[binary]` 3, `cryptography` ≥ 50;
  optional extra `agent-runner[login]` adds Playwright for Python (§11.3). macOS arm64 only (KONZEPT §3).
- Runs as an unprivileged dedicated OS user that owns the Lima instances (`$LIMA_HOME` default of that user).
- Service: system LaunchDaemon `/Library/LaunchDaemons/swiss.dfx.agent-runner.plist` with `UserName=<runner
  user>`, `RunAtLoad`, `KeepAlive`, `EnvironmentVariables {AGENT_RUNNER_HOME, PATH}`, logs to
  `$AGENT_RUNNER_HOME/log/runner.{out,err}.log`. Chosen over a LaunchAgent because execution devices are
  headless servers without a GUI login. `agent-runner service install|uninstall` writes/removes the plist and
  runs `launchctl bootout`/`bootstrap system` (requires `sudo`); under pytest the plist is written into the test
  home and launchctl is skipped (pattern of `agent_cli.daemon`).
- Internal supervisor: the daemon runs named tasks (`sync`, `ws`, `reconcile`, `vm-pool`, `vm-idle`, `guestlink`
  per VM, `executor`, `metrics`, `quota`, `requests`, `catchup`, `retention` – every 10 min, batches of 5000 rows,
  §2.3). A task that raises is restarted with backoff
  1 s → 30 s; more than 10 restarts in 60 s of the same task terminates the daemon with exit 1 (launchd restarts
  it). SIGTERM: stop accepting work, mark running sessions as they are (VMs keep running), flush outbox, exit 0
  within 10 s.

### 1.2 CLI

| Command | Purpose |
|---|---|
| `agent-runner enroll --hub URL --code CODE [--platform]` | §2.1 |
| `agent-runner run` | foreground daemon (development, tests, launchd target) |
| `agent-runner status [--json]` | identity, hub connection, outbox backlog, VMs, sessions, pool, capacity |
| `agent-runner doctor` | checks: Lima ≥ 2.3, vz available, golden ready, Landlock ABI in golden (from build record), PostgreSQL, hub reachability and pinned key, textfile dir writable, clock skew < 5 s against hub time |
| `agent-runner image build [--claude-version V]` / `image list` / `image retire VERSION` | §3.1 |
| `agent-runner pool status` | pool VMs and refill state |
| `agent-runner service install` / `service uninstall` | §1.1 |
| `agent-runner uninstall [--keep-identity]` | stop service, delete VMs created by the runner, delete store; identity only without the flag |

Output: one line per fact with fixed prefixes (`runner.status …`), JSON with `--json`; never secrets, tokens or
decrypted content (repository rule). Exit codes: 0 ok, 1 failure, 2 usage.

### 1.3 Configuration (`AGENT_RUNNER_*`, fail-closed)

Loaded once at start; a missing required or invalid value exits with `config_invalid <NAME>` (no value echoed).

| Name | Default | Notes |
|---|---|---|
| `HOME` | `~/.local/share/agent-runner` | 0700 |
| `HUB_URL` | required | `https://` (tests: `http://127.0.0.1…` allowed only with `AGENT_RUNNER_TEST_MODE=1`) |
| `PLATFORM_ROOT_KEY` | required | b64u Ed25519 public key |
| `VM_CPUS` / `VM_MEMORY_MIB` / `VM_DISK_GIB` | 4 / 8192 / 20 | per workgroup VM |
| `POOL_SIZE` | 2 | 0 disables the pool (clone path only) |
| `MEMORY_RESERVE_MIB` | 32768 | host reserve excluded from the VM budget |
| `SESSION_IDLE_SECONDS` / `VM_IDLE_SECONDS` | 1800 / 1800 | |
| `TEXTFILE_DIR` | empty = textfile sink off | node_exporter textfile directory (08 §3) |
| `MODEL_CATALOG` | packaged `model-catalog.json` | §10.5 |
| `OPENROUTER_KEY_FILE` | empty | platform devices only; file 0600; refused on org devices (`config_invalid`) |
| `GITHUB_CLIENT_ID` | required for GitHub features | public client ID of the GitHub App |
| `GITHUB_API_URL` / `GITHUB_WEB_URL` | `https://api.github.com` / `https://github.com` | doubles in tests |
| `MODEL_UPSTREAM_ANTHROPIC` / `MODEL_UPSTREAM_OPENROUTER` | `https://api.anthropic.com` / `https://openrouter.ai/api` | doubles in tests |
| `ANTHROPIC_USAGE_URL` | `https://api.anthropic.com/api/oauth/usage` | quota polling |
| `LOGIN_ARGV` | `["claude","setup-token"]` (JSON array) | login-CLI double in tests |
| `LOGIN_AUTOMATION` | `on` for platform devices (platform install profile), `off` for org devices | `on` enables §11.3 (needs the `login` extra; the platform install profile includes it) |
| `GOLDEN_TEMPLATE` | `images/golden.yaml` of the checkout | the build also reads `images/`, `src/agent_guest/` and `contracts/` next to the template; missing → `golden_source_missing` (KOR-03) |
| `TEST_MODE` | `0` | allows loopback HTTP and double upstreams; MUST be `0` in production (doctor warns) |

### 1.4 Files and store

- `$AGENT_RUNNER_HOME/device.json` and `keys/device.key` (0600, `O_EXCL`, refuse symlinks; pattern of
  `agent_cli.keys.create_key`), `pg/` (own PostgreSQL cluster managed like `agent_cli.pg`: `initdb --auth=trust`,
  `127.0.0.1` only, own port file, `LC_ALL=C`), `golden/` build records, `log/`, `tmp/` (0700; per-operation
  scratch, wiped at start).
- Schema `runner` (`02-data-model.md` §4). Scope keys are stored encrypted with the storage key and decrypted
  only into memory.

### 1.5 Reused legacy code (read-only imports, `00-overview.md` §5)

Imported: `agent_cli.pg` (cluster lifecycle), `agent_cli.keys.request_text`/`response_text` (signed requests and
response verification), `agent_cli.account_login.newest_magic_link` (called with a `Mailbox` value built from the
decrypted `mailbox` credential). Copied and adapted (new code under the 100 % gate): the usage parsers
`windows_snapshot`/`spend_snapshot` with the explicit window mapping of `00-overview.md` §5. Patterns
copied with attribution: key-file writing, atomic 0600 writes, gapless `origin_seq` under advisory lock,
capped reconnect backoff, supervisor restart limits, env allow-list from `spikes/model-access/claude_run.py`.

## 2. Identity, enrollment, sync

### 2.1 Enrollment

`enroll` (org device, `04-crypto.md` §5.8): parse code → derive `invite_id`, `package_key`, `sk_inv` →
`POST /v1/invites/lookup` → open package (failure `invite_code_invalid`) → `GET /v1/device/logs` is not yet
possible, so the package `head` and the log returned by lookup (`log_head`) are verified with the anonymous
`GET /v1/orgs/{org}/log`-equivalent included in the lookup response for device invites → generate X25519 +
Ed25519, write `device.key` → build `accept` (`sig`, `inv_sig`) → `POST /v1/device/enroll` signed with the new
key → store package keys (`source=package`), pin `hub_key` from the package (TOFU is not used), write
`device.json`. Platform device (§5.9): same key generation, `enroll_sig` over the canonical enrollment body,
state `pending_certification`; `run` polls `GET /v1/device/state` every 30 s until `active`.
Any failure leaves no partial identity (keys written last-but-one, `device.json` last; on error both removed).

### 2.2 Reconcile loop

Triggers: every WebSocket hint, every 30 s, after reconnect. Steps (each idempotent):
1. `GET /v1/device/state` (desired state).
2. Logs: fetch new membership/platform log entries, verify (`04-crypto.md` §6.1/§6.2); on `log_rollback` or
   `log_fork` stop acting for that org, raise `runner.alert` and audit (`runner.log_rollback` / `runner.log_fork`), keep syncing other orgs.
3. Wraps: fetch, verify W1–W3, unwrap, store.
4. Assignments: compute added/removed workgroups → VM actions (§3.5).
5. Sessions: for every desired session not running locally in the desired state → act (§6); for local sessions
   not in the desired set → stop.
6. Prompts: deliver `pending_prompts` (§6.2) after verifying the prompt event (E1–E6, author `wg:write` at `lh`).
7. Requests, pings, credential changes, rotation hints (devices never rotate; they only re-wrap per W2).
8. Catch-up (§2.5).

### 2.3 Outbox, pull, WebSocket

- Hub connections (KOR-18): the signed client reuses an idle connection for at most 2 s (`KEEPALIVE_EXPIRY_S`),
  well below the hub's 120 s and the 5 s of common server defaults, so it never writes on a connection the hub is
  closing for idleness. A request that still fails on a **reused** connection before any byte of the answer arrived
  (`RemoteProtocolError`, `ReadError`, `WriteError`; no timeouts, no connect errors) is sent exactly once more with a
  new signature (time, nonce) and the same body (the pool drops the broken connection and takes a new or another idle
  one; a failure of the repeat is final); a byte-identical repeat is harmless for every
  device endpoint (`03-api.md` §1 Idempotency: outbox items by `origin_seq`, responses by `seq`, credential writes by
  the stored bytes, operations by `operation_id`, enrollment by its keys; reads have no effect). Failures on a new
  connection, after the answer began, and answers that do not verify are not repeated. A transport error names
  whether the connection was reused and the error class (`push_failed reason=… reused=yes|no cause=…`).
- Outbox per `03-api.md` §6.1: items written in the same local transaction as the state change they describe;
  pushed in batches (≤ 500 items / 4 MiB) whenever non-empty: at once when no push started within the last 100 ms, else 100 ms after the start of the previous push (start to start; items written while a push is on the way leave together with the next one), never two batches at once (device rate budget `03-api.md` §1 and §6.1, ADR-048); a push still unanswered after three times the median time to an answer of the last 16 answered pushes, at least 150 ms, is sent once more with the same body and a new signature on another connection, the first answer counts and the other request is cancelled; at most one copy per 2 s and none for a batch above 256 KiB (hedged push, ADR-050, log `push_hedged after_ms=…`). Outbox content is kept as plaintext encrypted with
  the storage key; events are sealed at the first push attempt and the sealed bytes are reused for every retry,
  re-sealed only after `status:"reseal"` (`04-crypto.md` §5.12). After a local store wipe the outbox continues at
  `last_origin_seq + 1` and prompts listed in `delivered_prompt_event_ids` are never delivered again (`03-api.md`
  §6.1); sessions the hub still lists for this device but the wiped store does not know (children included) are
  reported `failed` (`device_shutdown`), or `stopped` when the hub wants them stopped, so the next follow-up starts a
  new main session (DR-01, KOR-09). `pushed_origin_seq`
  advances only on `acked_through`. `409 origin_seq_gap` → resend from `detail.expected`; when `expected` lies below
  the stored items (they were pruned, the hub was restored from an older export) the runner confirms the hub's
  position (`GET /v1/device/state`: `active`, `last_origin_seq == expected − 1`), renumbers its stored items gaplessly
  from `expected` (sealed bytes kept, `04-crypto.md` §5.12), reports the loss (log and audit `runner.alert`
  `{alert:"outbox_items_lost",value:<count>}`) and keeps pushing; unconfirmed → retried (KOR-10). `409
  origin_seq_conflict` is fatal: stop the `sync` task, keep sessions running, write `runner.alert
  origin_seq_conflict` to the log and the metrics (`agent_runner_device_sync_fatal 1`), require operator action
  (`agent-runner doctor` explains). `status:"rejected"` items are logged and skipped.
- Retention (task `retention`, KOR-03): acknowledged outbox items older than 1 h are deleted (a prefix only; the item
  with the highest `origin_seq` always stays; nothing while the hub's position is unknown); `prompt_event` rows whose
  delivery is final and unchanged for 24 h are deleted.
- Scope queues (ADR-047, KOR-08): an `event` the runner cannot seal yet (no key of the needed epoch, no verified log
  head) moves with every later item of its scope (an `operation_transition` naming a held result belongs to it) to
  `runner.outbox_held`, outside the numbering; the other later items are renumbered gaplessly and pushed at once, and
  new items of a waiting scope join its queue without an `origin_seq`. Before each batch the first item of every queue
  is tried again; once it seals, the queue is numbered after the current end. Only items the hub never stored are
  moved or renumbered. Reconcile step `outbox` (order 45) discards a queue only when the verified log shows the device
  may no longer read the scope, with audit `runner.alert {alert:"outbox_discarded",value:<items>}`; held items are not
  part of the outbox backlog.
- Pull: one cursor per scope (`inbox_cursor`), `GET /v1/device/pull` until empty after each `events`/`prompt`
  hint and every 30 s.
- WebSocket: signed upgrade; heartbeat every 15 s with capacity, sessions, VMs; reconnect backoff 1 s doubling to
  30 s, ±20 % jitter, reset after 30 s stable. Order on reconnect: reconcile → push outbox → pull.
- Every hub response is verified against the pinned hub key (`agent-response-v1`); verification failure =
  transport error (retry), never acted upon.

### 2.4 Inbound verification (mandatory before acting)

Events: E1–E6 + author right at `lh`; device-relevant kinds only. Log entries: L1–L10. Wraps: W1–W3. Credential
records: author signature, author is the owner or a device eligible for the owner's `CK`. Device requests:
user signature (identity key from a verified log of a shared org), addressed to this device, not expired.
Anything failing verification is ignored, audited (`audit` outbox item, action `runner.inbound_rejected`, result
`rejected`, metadata only) and counted (`agent_runner_device_verify_failures_total`). A refusal that depends only
on the device's copy of the logs (a wrap's entitlement, a request's user) is judged only after the platform log and
the acting orgs' logs were refreshed from the hub; a rightful item ahead of the copy is then accepted without audit
(HOT-04, KOR-17).

### 2.5 Catch-up wrapping

After every reconcile the runner calls `GET /v1/orgs/{org}/catch-up`-equivalent for devices (computed locally
from the verified log) and uploads wraps it may create under W2 for: new members and new devices of workgroups it
holds `WK` for, `OK` for org members/devices, and owners' `CK` for eligible devices. Rate: ≤ 200 wraps per
reconcile.

### 2.6 Credential cache

Records are fetched on `credentials` hint or when a session needs them, cached encrypted (`credential_cache`),
decrypted only for the duration of use (gateway provisioning, GitHub call, quota poll). Deleted credentials
(`404`) are purged from cache and from all guest gateways (`PUT …/credential` with access removed → session
gets `model_access_unavailable` on the next request and is stopped with that reason).

## 3. VM management (ADR-009)

### 3.1 Golden image

- Defined in `images/golden.yaml` (Lima template) + `images/provision/*.sh` + `images/files/`:
  `minimumLimaVersion 2.3.0`, `vmType: vz`, `arch: aarch64`, image Ubuntu 24.04 arm64 cloud image with pinned
  URL **and** `digest: sha256:…`, `cpus/memory/disk` from config, `mounts: []`, `containerd {system:false,
  user:false}`, no port forwards except
  `portForwards: [{guestSocket: "/run/agent-guest/control.sock", hostSocket: "{{.Dir}}/sock/agent-guest.sock"}]`.
- Provisioning (system mode, idempotent, guarded by `/var/lib/agent-golden.done`): slim steps of Spike A
  (`slim.yaml`: purge snapd and listed packages, disable timers, mask `systemd-networkd-wait-online`, GRUB timeout
  0); install `tmux`, `nftables`, `acl`; Node LTS from a pinned tarball (sha256 verified); Claude Code
  **2.1.330** from a pinned npm tarball (`npm install -g <file>` after sha256 check) installed to
  `/opt/claude/claude` (real binary path recorded), auto-updater disabled (managed settings env + `DISABLE_AUTOUPDATER`);
  copy `agent_guest` to `/opt/agent-guest/` (root, 0755, files 0644); `/etc/claude-code/managed-settings.json`
  (§4.2); `/opt/agent-guest/mcp.json`; harness static text `/opt/agent-guest/harness/RULES.md`; nftables
  `/etc/nftables.d/agent-sessions.nft` (§4.3); users and groups (§5.1); `/srv/sessions` own bind mount
  `noexec,nosuid,nodev` (fstab); systemd units `agent-guest-control.service` and `agent-guest-api.service`
  (enabled); a boot-time unit `agent-guest-harden.service` (runs **every boot**, after cloud-init, because Lima
  re-runs cloud-init on every clone and restores the default user's sudo) restricts the Lima default user's sudo
  to `systemctl restart agent-guest-*` and consumes the control token (below).
- Build: `agent-runner image build` → `limactl create --name=golden-build-<hex>` + `start` → provisioning →
  `exec_libs` computation (`ldd` of `/opt/claude/claude` and of the Python interpreter + `_ctypes`; realpaths,
  sorted, unique) stored in `/opt/agent-guest/exec-paths.json` → Landlock ABI probe stored in
  `/opt/agent-guest/build.json` (`{claude_version, guest_version, landlock_abi, exec_paths_sha256, built_at}`) →
  smoke tests inside (guest services healthy, `claude --version`, sandbox self-test §4.5, Claude TUI start time
  under Landlock measured and stored in `build.json`) → `limactl stop` →
  `limactl rename` to `golden-<claude>-<guest>-<yyyymmdd>` → `golden` row `ready`. Build time target < 2 min.
  The build fails (`golden` row stays `building` then removed) if any check fails or Landlock ABI < 4.
- Exactly one golden is `ready` for new clones (newest); older ones become `retired` once no VM references them
  and are deleted by `image retire`.

### 3.2 Warm pool and assignment

- Pool VMs: running clones of the ready golden without workgroup data or credentials. Lima name `arp-<8 hex>`,
  `vm_id` `vm-<8 hex>` (independent random values; mapping in `runner.vm`). Clone: `limactl clone -y <golden>
  <name>` (APFS clone, ~0.04 s) → `limactl start -y --set '.param.AGENT_GUEST_CONTROL_TOKEN="<token>"' <name>` (token 32 random bytes b64u passed as a
  Lima param before boot; `agent-guest-harden.service` writes it to `/etc/agent-guest/control-token` (root 0600) at
  boot; only the hash stored in `runner.vm.control_token_hash`, the token itself kept in memory and in
  `$AGENT_RUNNER_HOME/vm/<vm_id>.token` 0600; the param is removed from the instance config after first use)
  → sandbox self-test §4.5 (pool fill only) → `GET /v1/health` until `state:"unassigned"` (poll 100 ms,
  timeout 30 s).
- Assignment (hot path): pick a pool VM → `POST /v1/assign {vm_id, org_id, wg_id}` → `runner.vm` row
  `assigning` → `running`; outbox `vm_state`. Measured target 0.1 s (Spike A: 0.09–0.13 s).
- Refill: asynchronous after every assignment and every 60 s, up to `POOL_SIZE`, at most 2 clones in parallel,
  only while capacity allows (§3.4). A refill clone never delays a hot-path request.
- Fallback when the pool is empty: clone + start on the hot path (Spike A: 4.3–6.0 s incl. load with 100 VMs).
  Restart of a stopped workgroup VM: `limactl start` (Spike A: 4.1–5.9 s). Cold boot from the cloud image is
  never in the hot path.

### 3.3 VM states and failure handling

`pool` → `assigning` → `running` → (`stopping` → `stopped` → `running` via restart) → `deleted`.
- While the daemon stops, no clone is started or retried; clone, start and delete of one instance run under a
  per-instance lock (KOR-03).
- Clone or start failure: delete the half-created instance (`limactl delete -f`), retry once with a new name;
  second failure → the requesting session fails with reason `vm_start_failed`; metrics `agent_runner_vm_start_failures_total`.
- Guest unhealthy (health probe fails 3 times, 5 s apart) on a running VM: sessions → `failed`
  (`guest_unreachable`), VM restarted once; repeated failure → VM `stopped` and reported.
- One VM per (workgroup, device). Two sessions of the same workgroup never trigger two VMs: VM creation is
  serialised per workgroup (in-process lock + `runner.vm` unique partial index on `wg_id` where state ≠ `deleted`).

### 3.4 Capacity

`budget_mib = physical RAM − MEMORY_RESERVE_MIB`; `vm_slots_total = floor(budget_mib / VM_MEMORY_MIB)`;
`vm_slots_free = vm_slots_total − (VMs in pool, assigning, running, stopping)`. Stopped VMs do not consume
slots (RAM is returned on stop; Spike A high-water-mark finding). Reported in every heartbeat (`capacity`,
`02-data-model.md` §2.1). If a session needs a VM and `vm_slots_free = 0`: stop the least recently used idle
VM of another workgroup (no running or suspended session younger than `VM_IDLE_SECONDS`), else fail the request
with `capacity_exhausted` (the hub then re-schedules: a new main session of the task, `03-api.md` §4.6.2, ADR-049;
the refused session ID is never started again).

### 3.5 Lifecycle rules

- Idle stop: no session in `working|idle|prompt_delivering|…` for `VM_IDLE_SECONDS` → `limactl stop`.
- Workgroup unassigned, workgroup archived, org removed this device, device decertified: stop sessions
  (`stopping` reason `unassigned`), `limactl delete -f`, delete scope keys of that workgroup after rotation is
  observed or immediately when removed from the org.
- Golden upgrade: a workgroup VM keeps its golden version for its lifetime. At the next start of a stopped VM
  whose golden is not the newest ready one and which has no `suspended` session, the runner deletes it and assigns
  a fresh pool/clone VM of the newest golden instead. Sessions that were `stopped` restart fresh (no `--resume`);
  their harness says to read the history with `get_task_messages` (`03-api.md` §4.6.3).
- Pool VMs of a retired golden are deleted and refilled.
- Startup reconciliation: `limactl list --json` vs `runner.vm`; unknown `arp-*` instances are deleted (one Lima
  refuses to delete is logged, `vm.unknown_delete_failed`, and reconciliation continues; the empty directory of an
  interrupted clone is removed); known instances missing in Lima are marked `deleted`.

## 4. Sandbox (two independent layers, ADR-008)

Each layer alone MUST stop every attack of the attack suite (`07-testing.md`, ATK). Hooks (§5) are additional
visibility and policy, never the only barrier.

### 4.1 Layer 1 – Claude Code configuration

Command line (inside the Landlock launcher, §4.3):

```
/opt/claude/claude --restricted --strict-mcp-config --mcp-config /opt/agent-guest/mcp.json
  --tools Read,Glob,Grep,Edit,Write
  --disallowedTools Bash,Agent,Task,WebFetch,WebSearch,NotebookEdit,Skill
  --permission-mode dontAsk --disable-slash-commands
  --append-system-prompt-file /run/agent-guest/harness/<session_id>.md
  [--resume <claude_session_id>] [--model <model_id>] [<initial prompt>]
```

`/opt/agent-guest/mcp.json`:
`{"mcpServers":{"platform":{"type":"http","url":"http://127.0.0.1:7700/mcp","headers":{"Authorization":"Bearer ${PLATFORM_SESSION_TOKEN}"}}}}`.

### 4.2 Managed settings (`/etc/claude-code/managed-settings.json`, root 0644, generated at golden build by `agent_guest.sandbox.settings`)

- `permissions.defaultMode: "dontAsk"`, `permissions.disableBypassPermissionsMode: "disable"`,
  `disableAutoMode: "disable"`.
- `permissions.allow`: `Read`, `Glob`, `Grep`, `Edit`, `Write`, and every platform tool
  `mcp__platform__<tool>` of `03-api.md` §8.1.
- `permissions.deny` tools: `Bash`, `PowerShell`, `Agent`, `Task`, `TaskOutput`, `TaskStop`, `KillShell`,
  `BashOutput`, `Monitor`, `WebFetch`, `WebSearch`, `NotebookEdit`, `Skill`, `Workflow`, `CronCreate`,
  `CronDelete`, `RemoteTrigger`, `EnterWorktree`, `ExitWorktree`, `SendMessage`, `TeamCreate`, `LSP`.
- `Edit(…)` denies (cover all write tools; `Write(path)` rules are ignored by Claude Code 2.1.330):
  `~/.claude/**`, `~/.claude.json`, `**/.claude/**`, `**/.mcp.json`, `**/CLAUDE.md`, `**/.git/**`, `**/.gitconfig`,
  `//etc/**`, `//opt/**`, `//usr/**`, `//var/**`, `//proc/**`, `//run/**`, `//srv/sessions/*/home/**`.
- `Read(…)` denies: `//etc/agent-guest/**`, `//var/lib/agent-guest/**`, `//proc/**`, `//run/agent-guest/**`
  except the own harness file (denied paths are absolute; the own harness is outside these via the
  launcher's read rule – Claude reads it at start before tools run), `//srv/sessions/*/home/**`.
- `allowManagedPermissionRulesOnly: true`, `allowManagedHooksOnly: true`, `allowManagedMcpServersOnly: true`,
  `allowedMcpServers: [{"serverName":"platform"}]`, `strictKnownMarketplaces: []`,
  `disableSkillShellExecution: true`.
- Hooks: one HTTP hook per event of §5.1 → `http://127.0.0.1:7700/hooks/<Event>`, `timeout: 15`, header
  `Authorization: Bearer $PLATFORM_SESSION_TOKEN`, `allowedEnvVars: ["PLATFORM_SESSION_TOKEN"]`; tool events with
  `matcher: "*"`. `allowedHttpHookUrls: ["http://127.0.0.1:7700/hooks/*"]`,
  `httpHookAllowedEnvVars: ["PLATFORM_SESSION_TOKEN"]`.
- `env`: `DISABLE_AUTOUPDATER=1`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`,
  `DISABLE_ERROR_REPORTING=1`, `CLAUDE_CODE_DISABLE_FEEDBACK_SURVEY=1`.
- No command hooks, no stdio MCP servers, no command status line (each would need an exec exception).

### 4.3 Layer 2 – operating system

Per session (created by `agent-guest-control`):
- User `s-<30 hex of session id>` (all hex digits without the UUID version and variant digits 13 and 17; GST-05
  decision 18), UID from 20000–29999 (lowest free), group of its own, shell
  `/usr/sbin/nologin`, no home creation by `useradd`. Directories `/srv/sessions/<session_id>/{work,home,home/.claude}`
  owner session user, 0700; `work` is the workspace; `.claude.json` pre-seeded (onboarding done, trust for
  `work`). ACL `u:root` implicit; the control process reads the transcript as root.
- Harness file `/run/agent-guest/harness/<session_id>.md`, owner root, 0444 (directory 0711).
- Session token: 32 random bytes b64u, generated by control, only its SHA-256 stored; given to the unit as env.
- Transient unit `agent-session-<session_id>` started by `systemd-run --pty --unit=… --collect` inside the tmux
  window (§7) with properties:

| Group | Properties |
|---|---|
| Identity | `User=`/`Group=` session user, `WorkingDirectory=…/work`, `UMask=0077` |
| Environment | exactly the allow-listed env of §10.3 (+ `HOME`, `CLAUDE_CONFIG_DIR=…/home/.claude`, `PLATFORM_SESSION_TOKEN`, `TERM=xterm-256color`, `LANG=C.UTF-8`, `PATH=/usr/bin:/bin`) |
| Filesystem | `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateDevices=yes`, `PrivateTmp=no` + `TemporaryFileSystem=/tmp:rw,noexec,nosuid,nodev,mode=1777,size=512M`, `TemporaryFileSystem=/srv/sessions` + `BindPaths=/srv/sessions/<id>`, `TemporaryFileSystem=/run/agent-guest/harness` + `BindReadOnlyPaths=/run/agent-guest/harness/<id>.md`, `InaccessiblePaths=/etc/agent-guest -/var/lib/agent-guest -/run/agent-guest/control.sock -/run/agent-guest/api.sock`, and `InaccessiblePaths=-/run/dbus/system_bus_socket -/run/systemd/private -/run/systemd/transient` (the system bus, systemd's private socket and the transient unit files expose every unit's environment, i.e. other sessions' tokens; Landlock cannot refuse a UNIX connect; SBX-02 decision 24) |
| Kernel/processes | `ProtectProc=invisible`, `ProcSubset=pid`, `ProtectKernelTunables=yes`, `ProtectKernelModules=yes`, `ProtectKernelLogs=yes`, `ProtectControlGroups=yes`, `ProtectClock=yes`, `ProtectHostname=yes`, `RestrictNamespaces=yes`, `RestrictSUIDSGID=yes`, `LockPersonality=yes`, `RestrictRealtime=yes`, `PrivateIPC=yes`, `KeyringMode=private`, `CapabilityBoundingSet=`, `SystemCallArchitectures=native` |
| Syscalls | `SystemCallFilter=@system-service landlock_create_ruleset landlock_add_rule landlock_restrict_self`, `SystemCallFilter=~@privileged @mount @debug @module @reboot @swap @obsolete @cpu-emulation` |
| Network | `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `IPAddressDeny=any`, `IPAddressAllow=localhost` |
| Exec | `NoExecPaths=/`, `ExecPaths=/opt/claude/claude <python3 realpath> <lib-dynload dir> <exec_paths from build.json>` |
| Resources | main sessions: `MemoryMax=4G`, `CPUQuota=200%`, `TasksMax=256`, `IOAccounting=yes`, `RuntimeMaxSec=8h`; subagent sessions: same with `RuntimeMaxSec=30min` |

- Landlock launcher `agent_guest/sandbox/landlock.py` (stdlib `ctypes`, run as
  `python3 -I /opt/agent-guest/agent_guest/sandbox/landlock.py <args> -- /opt/claude/claude …`): requires ABI ≥ 4
  (else exit 126, session `failed` reason `sandbox_failed`); handles all filesystem access bits of the ABI
  and `NET_BIND_TCP|NET_CONNECT_TCP`; rules: read-only `/usr`, `/lib`, `/etc`, `/opt/claude`, `/opt/agent-guest`
  (read only, for mcp.json), `/proc`, `/sys/devices/system/cpu`, the harness file; read-write
  `/srv/sessions/<id>` and `/tmp`; devices `/dev/null`, `/dev/zero`, `/dev/urandom`, `/dev/random`, `/dev/tty`,
  `/dev/pts`, `/dev/ptmx` (`READ_FILE|WRITE_FILE|IOCTL_DEV` where ABI ≥ 5); `EXECUTE|READ_FILE` only on
  `/opt/claude/claude` and its ELF interpreter; `NET_CONNECT_TCP` only port 7700; no bind. Then
  `prctl(PR_SET_NO_NEW_PRIVS)`, `landlock_restrict_self`, `execv`. After `execv` the Python interpreter can no
  longer be executed (no EXECUTE right).
- nftables (`/etc/nftables.d/agent-sessions.nft`, loaded at boot):
  ```
  table inet agent_sessions { chain out { type filter hook output priority 0; policy accept;
    meta skuid 20000-29999 oif lo tcp dport 7700 accept
    meta skuid 20000-29999 ct state established,related accept
    meta skuid 20000-29999 counter drop
    meta skuid "platform" tcp dport 443 accept
    meta skuid "platform" udp dport 53 accept
    meta skuid "platform" tcp dport 53 accept
    meta skuid "platform" oif lo accept
    meta skuid "platform" ct state established,related accept
    meta skuid "platform" counter drop } }
  ```
  In `TEST_MODE` golden variants the `platform` rule additionally allows the host gateway port of the model double.
  Dropped packets are counted (metric `agent_runner_session_egress_denied_total` via guest metrics).

### 4.4 Isolation between sessions of one VM

Different UIDs, 0700 directories, `TemporaryFileSystem=/srv/sessions` + own bind (other sessions invisible),
`ProtectProc=invisible`, separate cgroups with limits, one token per session; the API process maps the token to
exactly one session and serves only that session's data.

### 4.5 Sandbox self-test

`agent_guest.sandbox.selftest` runs at golden build, when a pool VM is filled (before it is marked ready) and when the
guest version changes – never on the hot path: starts a probe (Python; the outer launcher layer additionally
allows executing the interpreter, the probe then applies the session's own ruleset as a second layer, so its
effective rights equal a session's – Landlock layers can only restrict) in an identical unit and asserts: `/bin/sh`, `git`,
`python3` exec → EACCES; `ld.so <file>` → mapping fails; write outside workspace → EROFS/EACCES; other session dir
invisible; TCP to 1.1.1.1:443 and to 127.0.0.1:8787 refused; DNS fails; port 7700 reachable; `unshare` → EPERM.
Failure ⇒ guest health `state:"broken"`, VM never assigned.

### 4.6 Spike defects that MUST be fixed

1. Session token compared with `hmac.compare_digest` against the stored SHA-256; session table cached in memory,
   updated via control (no file re-read per request).
2. Path policy resolves paths with `os.path.realpath` (symlinks), after joining relative paths to the workspace;
   a path whose resolution leaves the workspace (writes) or workspace ∪ own harness (reads) is denied; paths that
   do not exist are resolved through their deepest existing parent.
3. MCP and operation parameters are validated against the JSON Schemas (`additionalProperties:false`,
   `bool` is never accepted as integer).
4. Hashes over structured data use canonical JSON (`04-crypto.md` §1).
5. Asciicast offsets are monotonic across recorder restarts (offset continues from the last written offset;
   a restart writes an `"m"` marker event, not a new header).
6. Terminal bytes decoded with an incremental UTF-8 decoder (`codecs.getincrementaldecoder("utf-8")(errors="replace")`).
7. Prompt hashes are registered only through `POST /v1/sessions/{id}/prompts` (control API), never by direct DB
   writes.

## 5. Guest agent and hooks

### 5.1 Processes (least privilege, stdlib only)

| Service | User | Listens | Duties |
|---|---|---|---|
| `agent-guest-control` | root | `/run/agent-guest/control.sock` (root:`agent-ctl`, 0660) and `/run/agent-guest/api.sock` (root:`platform`, 0660) | control protocol `03-api.md` §7; session users, directories, units; tmux server on `/run/agent-guest/tmux.sock` (root 0600); recorder; transcript reader; file import/export; workspace scanner; journal owner (SQLite); pending MCP calls; pings; metrics (cgroup files) |
| `agent-guest-api` | `platform` (system user, no login) | `127.0.0.1:7700` | `/mcp`, `/hooks/<Event>`, `/model/…`; forwards journal records and pending calls to control over `api.sock` (JSON lines request/response); the API process may journal only `session.hook`, `usage`, `ping.delivered`, `guest.error` and `platform.refused`, every other kind is written by control (KOR-07) |

Group `agent-ctl` contains the Lima default user (so Lima's socket forwarder can connect) and nobody else.
Both services: `Restart=always`, `NoNewPrivileges=yes`; api additionally `ProtectSystem=strict`, `ProtectHome=yes`,
`PrivateTmp=yes`, no write paths. Hook and MCP handlers time out after 10 s internally (below the 15 s hook
timeout).

### 5.2 Hook events

The 18 events: `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `PermissionRequest`, `PermissionDenied`, `Notification`, `Stop`, `StopFailure`,
`SubagentStart`, `SubagentStop`, `PreCompact`, `PostCompact`, `ConfigChange`, `InstructionsLoaded`, `CwdChanged`.
Every call is journaled as `session.hook` (`03-api.md` §7.2). `SessionStart` is not delivered over HTTP in
2.1.330; the session start is journaled by control when the unit starts. `SubagentStart` must never occur (tools
denied); if it does it is journaled with `decision:"none"` and raises `guest.error subagent_hook_seen`.

### 5.3 PreToolUse policy (deterministic, `agent_guest/policy.py`)

Evaluated in order; first match decides:
1. Tool `mcp__platform__<t>` with `<t>` in the tool list of `03-api.md` §8.1 → allow.
2. Tool not in {`Read`,`Glob`,`Grep`,`Edit`,`Write`} → deny "Tool <name> is forbidden in agent.dfx.swiss sessions.
   Request actions through the platform tools (mcp__platform__request_operation)."
3. `Glob.pattern` / `Grep.glob`: deny if it starts with `/` or `~`, contains a `..` segment, or contains a NUL.
4. Path = `file_path` or `path` (default: workspace); relative paths joined to the workspace; resolved per
   §4.6.2.
5. `Write`/`Edit`: path must lie inside the workspace and not match a protected pattern (`*/.claude/*`,
   `*/.claude.json`, `*/.mcp.json`, `*/CLAUDE.md`, `*/.git/*`, `*/.gitconfig`) → else deny.
6. `Read`/`Glob`/`Grep`: path inside the workspace or equal to the own harness file → else deny.
7. Allow.

Deny reasons are fixed English sentences (contract fixtures). Every deny: journal `session.hook` with path, hub
audit item `hook.denied` (session, tool name, rule number – never the path), metric counter.

### 5.4 UserPromptSubmit (prompt origin)

`normalise(text)`: if the whole prompt matches `^<pasted_content id="[^"]*">\n(.*)\n</pasted_content id="[^"]*">$`
(DOTALL) take the inner text; strip surrounding whitespace; NFC. Accept iff a row
(`session_id`, `sha256_hex(normalise(text))`) exists in `prompt_registry` with `used_at` NULL; mark it used.
Otherwise block (`03-api.md` §8.2), journal `session.state prompt_refused` and audit `session.prompt_refused`.

### 5.5 Other hook handling

- `PostToolUse` for `Write`/`Edit`: journal `session.file` (path, sha256 of the new content read by control,
  size, patch ≤ 64 KiB) and `session.tool`.
- `Stop`: main session → state `idle`; subagent session → completion (§13). `StopFailure` → `session.status`
  with error.
- `PermissionRequest`, `PermissionDenied`: journal only (mode `dontAsk` never asks).
- Fail-open: if a hook request fails (500/timeout) Claude Code continues to its own permission check; layer 1
  denies and layer 2 sandbox remain, so a hook failure never opens a forbidden capability (tested by ATK with
  `FAULT_HOOKS`). Hook failures are counted (`agent_runner_session_hook_errors_total`).
- Pings: see §9; the `additionalContext` text is exactly the one of `03-api.md` §8.2.

## 6. Session lifecycle

### 6.1 Start (main session) – steps, states, budget

| Step | State pushed (`session_transition`) | Action | Budget |
|---|---|---|---|
| 1 | `vm_assigning` | ensure VM (same order as `03-api.md` §4.6.2 / ADR-009): running VM of the wg → none; stopped VM of the wg → restart (unless the golden replacement of §3.5 applies: delete it, then continue); pool → assign; else clone | pool 0.1 s / restart ≤ 6 s / clone ≤ 6 s |
| 2 | `vm_ready` | guest health assigned to this wg | – |
| 3 | – | choose model access (§10.1), decrypt credential | 0.05 s |
| 4 | `sandbox_ready` | `POST /v1/sessions` (user, dirs, harness file, token) + `PUT …/credential` | 0.3 s |
| 5 | `agent_starting` | register initial prompt hash; start tmux window + unit with the initial prompt as last CLI argument if it is ≤ 100 KiB (else start without it and deliver it by bracketed paste, §6.2) | ≤ 120 s, else `failed` `agent_start_failed` (reconcile step 56, counted from the first reconcile that saw the state; KOR-02) |
| 6 | `prompt_delivering` | Claude TUI up (transcript file created / first hook) | ≤ 2 s from step 5 |
| 7 | `working` | UserPromptSubmit accepted (`prompt_accepted`) | ≤ 0.5 s |

Warm-pool path total target ≤ 3 s; clone path ≤ 8.5 s; both under the 10 s requirement (`03-api.md` §4.6.1).
Each transition carries the device timestamp; the hub stores its receive time.

### 6.2 Prompt delivery (follow-up)

1. Verify the prompt event; decrypt; `POST /v1/sessions/{id}/prompts` (hash registered first).
2. Control: empty the input box by reading it – capture the pane and send `C-e C-u` ×3 per round, at most 20
   rounds, until the box reads empty (text two rounds do not change is a hint, not input); an input that cannot be
   emptied refuses the prompt before anything is written (the TUI puts an interrupted or hook-blocked prompt back
   into its input box). Then text ≤ 200 chars and single line → `tmux send-keys -l --` in 40-char chunks (40 ms
   apart); otherwise `tmux load-buffer -` (stdin) + `paste-buffer -p -d` (bracketed paste).
3. Wait 300 ms; the box must show the prompt (or the paste placeholder) and nothing else, else start again with
   step 2 (after waiting for the session state again).
   Send `Enter`; verified when the hook accepted it or the box no longer shows the first 30 characters (white space
   ignored) nor the placeholder; each check polls 1 s, `Enter` up to 3 retries. A `prompt_refused unregistered`
   journaled after the `Enter` while the prompt's registration is unused means the TUI submitted other text: empty and
   start again, at most 3 more attempts. Otherwise empty the input and `prompt_refused` reason `delivery_failed`
   (KOR-09).
4. `prompt_accepted` from the hook → `working`; `prompt_delivery` row `accepted`.
Prompts for a session are delivered strictly in event order; a prompt arriving while `working` waits until `idle`.

### 6.3 Control, idle, resume, stop

- Interrupt: `tmux send-keys Escape`. Stop: `systemctl stop agent-session-<id>` (SIGTERM, 10 s, SIGKILL), close
  window → `stopped`.
- Idle: `idle` for `SESSION_IDLE_SECONDS` → `suspend`: stop the unit, keep directories and transcript →
  `suspended`. Next prompt → `resuming` → start with `--resume <claude_session_id>` → deliver.
- Task closed → all sessions `stopping` → `stopped`; directories kept 7 days, then deleted by control.
- Failure reasons: the closed list `contracts/enums/session-failure-reasons.json` (`03-api.md` §11):
  `model_access_unavailable`, `vm_start_failed`, `guest_unreachable`, `sandbox_failed`, `agent_start_failed`,
  `agent_exited`, `prompt_refused`, `delivery_failed`, `device_offline`, `device_shutdown`, `capacity_exhausted`,
  `runtime_exceeded`, `budget_exceeded`, `oom`, `unassigned`.

### 6.4 Capture

- Transcript: control tails `…/home/.claude/projects/<cwd-key>/<claude_session_id>.jsonl` as root; `assistant`
  text blocks → `session.output` (one record per message), `tool_use`/`tool_result` pairs → `session.tool`.
  The transcript is display content only; usage comes from the gateway (§10.4).
- Files: `PostToolUse` + a workspace scan every 5 s (mtime/size, sha256 for changed files ≤ 10 MiB) → `session.file`
  for changes not already reported.
- Runner: journal → events (`session.*` kinds), encrypted with the current WK, signed by the device, outbox.

### 6.5 Task title

`task.set_title` operation (03 §9) → runner writes a device-authored `task.meta` event.

## 7. Terminal recording (ADR-016)

- tmux server (root, socket `/run/agent-guest/tmux.sock`), one tmux session `s-<id>` per session, size 160×48,
  `remain-on-exit off`. `pipe-pane -O` → recorder (part of control, reads a FIFO): incremental UTF-8 decoding,
  output events `[t_ms, "o", data]` with offsets in ms since session start, monotonic across restarts; batches
  flushed every 100 ms or at 16 KiB → journal `session.terminal` → runner → encrypted events (≤ 64 KiB). A record
  holds ≤ 16 KiB data and its body JSON ≤ 64 KiB − 16 bytes (JSON escapes count); the runner splits a well-formed
  record whose sealed event would still be too large into several `session.terminal` events (KOR-02).
- Read-only by construction: no API accepts terminal input; the only writers to the pane are the prompt
  delivery and interrupt of §6 (driven by verified prompt events and authorised control requests); nobody
  attaches to tmux. Latency target pane → browser p95 < 1 s.
- History: all chunks are kept as events (full history); the web client replays them (`06-web.md`).

## 8. Harness

- Static rules (`/opt/agent-guest/harness/RULES.md`, versioned with the guest): the session runs in
  agent.dfx.swiss; it must not run programs, scripts, shell, git, tests or builds, must not start subagents
  itself, must not modify or bypass hooks, settings, harness or sandbox; files may be read in the workspace and
  written only there; every other action must be requested with `mcp__platform__request_operation` (list of
  types with parameter examples from `03-api.md` §9); subagents only via `subagent.request`; platform notices
  arrive as content-free hints – use `get_notifications`; blocked attempts are recorded.
- Generated context block (`agent_guest.harness.render`): org/workgroup/task/session names and IDs, task owner
  display name, rights, parent session (subagents), limits, the MCP tool list with one example each, and – for
  restarted sessions – the instruction to read history with `get_task_messages`.
- Rendered by control into `/run/agent-guest/harness/<session_id>.md` (root, 0444) = RULES.md + context;
  `harness_version` (sha256 of RULES.md) journaled in `session.state` and stored at the hub transition reason.
  Placed via `--append-system-prompt-file` (top-level system prompt; Spike C: mid-conversation system messages
  are rewritten by OpenRouter).

## 9. Pings (ADR-020)

- Sources: hub `ping` hint and the durable `pings` list of `GET /v1/device/state` (hub table `session_ping`; whiteboard entry with notification), runner-internal `subagent.finished`,
  `operation.finished` (operation finished after the MCP call returned `pending`).
- Runner → `POST /v1/sessions/{id}/pings` for every active session in scope (org level: all sessions of that org
  on this device; wg level: of the wg; task level: of the task). Content-free: kind, level, IDs only.
- Guest stores the ping (`ping` table). Delivery: on the next response to `PreToolUse`, `PostToolUse` or
  `UserPromptSubmit` of that session, the api adds the `additionalContext` notice of `03-api.md` §8.2 (once per
  pending batch) and on `get_notifications` returns the list and marks delivered. The TUI is never typed into
  and the session is never interrupted; an idle session sees the notice with its next prompt.
- Delivery journaled (`ping.delivered`) and audited (`ping.delivered`) for every ping; for a hub ping the audit also
  marks the hub `session_ping` row delivered, the runner's own pings have no hub row (KOR-07). Never via `Stop` (a `Stop` hook answer with context would force the session to continue).

## 10. Model access (ADR-010, ADR-025)

### 10.1 Selection

Inputs: owner's `user_settings.model_access` (from `GET /v1/device/state`), owner's credentials decryptable on
this device, device kind. `auto` order: `claude_subscription` → `anthropic_api_key` → `platform` (only on
platform devices with `OPENROUTER_KEY_FILE`). An explicit preference that is unavailable → session `failed`
reason `model_access_unavailable` (no silent fallback). Model: owner's `model_id` if allowed for the access kind
in the catalog, else the catalog default (`platform`: `deepseek/deepseek-v4.1-flash-1017`; API key/subscription:
Claude Code default, no `--model`). Chosen kind and model are reported in `session_transition`.

### 10.2 Gateway (`/model/…` in `agent-guest-api`)

- Accepts only the session token (header `Authorization: Bearer` or `x-api-key`), maps to the session's
  provisioned credential; strips `authorization`, `x-api-key`, `host`, `content-length`, `connection`,
  `accept-encoding`; adds per kind: `anthropic_api_key` → `x-api-key: <key>`; `platform`/OpenRouter →
  `Authorization: Bearer <key>`; `claude_subscription` → `Authorization: Bearer <token>` and appends
  `oauth-2025-04-20` to `anthropic-beta`. Upstream base from the credential provisioning (§1.3 upstream URLs).
- Routes: `POST /model/v1/messages`, `POST /model/v1/messages/count_tokens`, `HEAD|GET /model/api/hello` (local
  200). Anything else → 404 + `guest.error gateway_route`.
- No credential (it is memory only in `agent-guest-api`; a restart loses it): journal `guest.error
  credential_missing` and hold the request without a model slot, polling every 0.2 s, until the runner provisions
  again (it ignores reports within 10 s of its last provisioning), at most 22 s and only while the session is active;
  then 503 `api_error`. One request per session waits and repeats the report every 10.5 s; at most 4 requests of a
  session wait, a further one is answered 503 at once (KOR-09).
- Checks: `model` in the catalog allow-list for the access kind (else 400 `model_not_allowed` in Anthropic error
  shape); daily platform budget of the provisioning (`budget {usd_per_day,spent_usd,journal_through?}`) → 429
  `budget_exceeded` once `spent_usd` plus the cost observed since the provisioning reaches `usd_per_day`. `spent_usd`
  = the hub's `spent_today_by_org` plus the cost of this device's platform `usage` outbox items above
  `last_origin_seq`, never lower within a hub day; `journal_through` = the VM's journal cursor, read after those items.
  A renewal carries `max(own charges journaled above journal_through, old spent + observed − new spent)`; a new day or
  no budget clears it (KOR-03, KOR-10).
- Streams responses unchanged (`read1` 64 KiB); parses SSE `message_start`/`message_delta` for usage and, for
  OpenRouter, `usage.cost` and `provider`; non-stream JSON `usage`. Writes journal `usage` per request.
- OpenRouter provider allow-list (catalog `providers_allowed`, empty = any): a `provider` outside the list is
  recorded as audit `model.provider_outside_allowlist` (metadata) and counted; the request is not blocked
  (provider is only known after the response starts).
- Egress only from the `platform` UID (nftables §4.3).

### 10.3 Session environment (allow-list; nothing inherited)

`ANTHROPIC_BASE_URL=http://127.0.0.1:7700/model`, `ANTHROPIC_AUTH_TOKEN=<session token>`, `ANTHROPIC_API_KEY=`
(empty), `CLAUDE_CODE_MAX_RETRIES=3`, `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`, `DISABLE_TELEMETRY=1`,
`DISABLE_ERROR_REPORTING=1`, `DISABLE_AUTOUPDATER=1`. For `platform`: `ANTHROPIC_MODEL`,
`ANTHROPIC_DEFAULT_OPUS_MODEL`, `ANTHROPIC_DEFAULT_SONNET_MODEL`, `ANTHROPIC_DEFAULT_HAIKU_MODEL`,
`CLAUDE_CODE_SUBAGENT_MODEL` all = chosen model; `CLAUDE_CODE_MAX_CONTEXT_TOKENS` = catalog window when < 1 000 000;
`CLAUDE_CODE_MAX_OUTPUT_TOKENS` from the catalog when the model's max output < 32 000.

### 10.4 Usage, cost, context

Per request (journal `usage` → outbox `usage` delta): tokens from the stream; cost: OpenRouter `usage.cost`
(`cost_basis:provider`); API key: catalog list price × tokens (`list_price`); subscription: list price as
equivalent (`subscription_equivalent`). Claude Code's own cost fields are never used (Spike C). Context:
`input + cache_read + cache_write` of the latest request ÷ catalog `context_window`.

### 10.5 Model catalog (`model-catalog.json`, schema `contracts/model-catalog.json`)

`{"version":1,"access":{"platform":{"default":"deepseek/deepseek-v4.1-flash-1017","allowed":[…],"providers_allowed":[…]},"api_key":{"default":null,"allowed":[…]},"subscription":{"default":null,"allowed":[…]}},"models":{"<id>":{"context_window":…,"max_output":…,"price_per_mtok":{"input","output","cache_read","cache_write"}}}}`.

### 10.6 Quotas

- Subscription: for each owner the hub names in `device/state.quota_poll_owners` (the election is authoritative;
  the owner's sessions may run on another device), poll `ANTHROPIC_USAGE_URL` every 900 s (`Authorization: Bearer`, `anthropic-beta:
  oauth-2025-04-20`, 8 s timeout, no redirects; 429 → wait `max(retry-after, 300 s)`); parse with
  `agent_cli.usage.windows_snapshot`; outbox `quota`. Only one device polls per owner (the online device
  with the smallest ID among those that can open the credential); a record not cached on that device is fetched
  from the hub after the desired state and the logs were refreshed (the owner may be ahead of the device's log copy;
  at most once per 900 s; `404` or a failed fetch skips the owner until then). For setup tokens the usage
  endpoint may refuse access: quota polling is feature-detected (first 401/403 ⇒ `quota_unavailable` for that
  credential version, retried after 24 h or after a new login; the runner sends one outbox
  `quota {owner_id,unavailable:true,fetched_at}` and the hub drops the owner's windows, KOR-05) and the UI shows
  "not available"; the M0 LIVE check (`07-testing.md`) verifies the
  gateway path with the read-only current access token.
- Platform key: `GET <openrouter>/api/v1/key` every 900 s → `spend_snapshot` → device metrics
  (`agent_runner_device_model_spend_*`), operators only.
- Tokens are never logged, never written to disk outside the encrypted credential record.

## 11. Credentials, device requests, subscription login (ADR-011)

### 11.1 Device request handling

`request` hint → fetch, verify (§2.4), decrypt, process by kind; respond with encrypted, signed responses
(`seq` 1..n). Expiry 15 min; at most one active request of each kind per user per device.

### 11.2 `claude.login` – setup-token relay (default)

1. Create temp dir `$AGENT_RUNNER_HOME/tmp/login-<id>` (0700) with its own `CLAUDE_CONFIG_DIR`/`HOME`; existing
   profiles are never read or written.
2. Start `LOGIN_ARGV` in a PTY with an allow-list env and `BROWSER` set to a capture helper (pattern of
   `account_login._write_capture_helper`); wait ≤ 30 s for the authorize URL.
3. Response 1: `{step:"authorize", url}`. The user opens it in their own browser, authorizes, copies the code.
4. Follow-up request (new request ID, same kind `claude.login`, header `follows` = original request ID, `04-crypto.md` §4.5): `{code}` → typed
   into the PTY.
5. Read the token from the PTY output (the setup-token line), validate format, build the `claude_subscription`
   credential record (CK), `PUT /v1/device/credentials/{owner}/claude_subscription`, response 2
   `{step:"done", email?}` (`email` only when the CLI reports it; optional); responses carry `final:true` on the last
   message; wipe the temp dir. Timeouts → `{step:"failed", code}`.

### 11.3 `claude.login` – mailbox automation (`LOGIN_AUTOMATION=on`; default on platform devices, off on org devices)

Requires the owner's `mailbox` credential. Same PTY start; instead of step 3 the runner opens the authorize URL in
headless Chromium (Playwright for Python), enters the email, polls IMAP with the logic of
`agent_cli.account_login.newest_magic_link` (INBOX + junk, sender filter, ≤ 1 MiB, newest first, poll 5 s,
≤ 10 min), opens the magic link, clicks authorize, reads the code and types it. Any unexpected page state →
fall back to §11.2 step 3 with `{step:"authorize", url, automation:"failed"}` (the web shows `login_automation_failed`). Hermetic tests only (login-CLI
double + auth-page double + mail double); live tests of logins are forbidden.

### 11.4 GitHub connect (ADR-015)

`github.connect` request → `POST <GITHUB_WEB_URL>/login/device/code` (`client_id`; a network error or `5xx` is
tried again after 2 s and 8 s, three attempts, then `upstream_unreachable`: the request has no effect for the user, an
unused device code expires; KOR-16) → response 1
`{step:"device_code", user_code, verification_uri, expires_in}` → poll `/login/oauth/access_token` with the
interval (respect `slow_down`) → `GET <GITHUB_API_URL>/user` → credential `github {token, login, user_id, scopes}`
(stored with repetition: a read or write lost in transport, `hub_unreachable`, is sent again after 1 s, 2 s and 4 s
like a response, the write with the same record bytes, which the hub answers with the stored version, so a lost
answer never stores a second version; the user has already confirmed the code, so one lost request must not discard
the connection; refusals and answers that do not verify end the flow; a failure is logged with `step=store`; KOR-18;
the same applies to the `claude_subscription` credential of §11.2)
→ response 2 `{step:"done", login}`. The App is configured with non-expiring user tokens; disconnect = credential
deletion (`DELETE /v1/me/credentials/github`), runners purge caches; the UI links to the GitHub App settings for
revocation at GitHub.

## 12. Operations (ADR-012)

Pipeline for every `request_operation`:
1. Guest api: tool arguments that break `mcp/tools/request_operation.input.json` or that the runner could not store
   (a NUL character or lone surrogate in a string, nesting deeper than 64 levels) are refused with `params_invalid`
   (journaled `platform.refused`; no `operation_id`, nothing reaches the hub, KOR-09). An unknown `type` or `params`
   that break `ops/<type>.params.json` → still forwarded with `local_check:"rejected"` (so it is recorded), then
   `rejected` returned.
2. Control journals `operation.request`; runner receives it, assigns state `received`.
3. Runner formal checks (`03-api.md` §9) on plaintext: settings from the latest verified `settings.workgroup`
   event, GitHub credential present, sizes, secret patterns. For `github.commit` the runner takes **one** workspace export
   snapshot into the scratch directory `tmp/op-<id>` here, before registration; scan, local tree SHA and the later
   commit all use exactly this snapshot (`03-api.md` §9).
4. Dedupe tag (`04-crypto.md` §4.10) with the current WK epoch.
5. Encrypted `operation.params` event; `POST /v1/device/operations` (retry with backoff while the hub is
   unreachable; 10 min in `received` → `expired`, `hub_unreachable`).
6. Decision: `accepted` → execute (§12.1) with transitions `executing` → `succeeded|failed`; `duplicate` → load
   the original's `operation.result` (if the original is not final yet: wait within the MCP wait time, else return
   `pending` with `duplicate_of`), return with `status:"duplicate"`; `denied`/`rejected` → return.
7. Result: outbox `event` (`operation.result`) then `operation_transition`, then `POST /v1/pending/{id}/answer`.
   If the MCP call already returned `pending`, a ping `operation.finished` is sent.

### 12.1 Executors (host side, REST only, no `git` binary, scratch under `tmp/op-<id>`)

| Type | Steps |
|---|---|
| `github.list_repositories` | `GET /user/installations` → `GET /user/installations/{id}/repositories` (paginated); mark `allowed` from settings |
| `github.checkout` | resolve ref (`GET /repos/{r}/commits/{ref}`) → `GET /repos/{r}/tarball/{sha}` streamed to scratch → safe extraction (regular files/dirs only; reject absolute, `..`, links, devices; strip the top directory; size limits) → re-tar → `files/import?dir=repos/<owner>/<name>&replace=true` (`<owner>/<name>` in lower case: GitHub names, the allow-list and the dedupe are case-insensitive; the result's `dir` names the path, KOR-10); remember base sha per (session, repo) |
| `github.get_pull_request` | `GET /repos/{r}/pulls/{n}`, `/files` (paginated), `/issues/{n}/comments` |
| `github.create_branch` | `GET /repos/{r}/git/ref/heads/{from}` (or commit sha) → `POST /repos/{r}/git/refs` |
| `github.commit` | uses the snapshot taken in step 3 (no second export) → parent = head of `branch` at GitHub at check time, or the checkout commit if the branch does not exist → compare with the parent tree (`GET /repos/{r}/git/trees/{parent_tree}?recursive=1`; blob and tree SHA-1 computed locally in git object format) → dedupe fields include the local tree SHA → after acceptance `POST /git/blobs` for changed files → `POST /git/trees` with `base_tree` (+ deletions) → verify returned tree SHA = local (`tree_mismatch`) → `POST /git/commits` (parent as determined) |
| `github.push` | check fast-forward (`GET /repos/{r}/compare/{head}...{sha}` status `ahead`/`identical`) → `PATCH /repos/{r}/git/refs/heads/{branch}` `force:false` or `POST /git/refs` if missing |
| `github.create_pull_request` | `GET /repos/{r}/pulls?head=<owner>:<head>&base=<base>&state=open` (existing ⇒ operation `succeeded` with that PR and `result.existing:true`; MCP status `duplicate`) → else `POST /repos/{r}/pulls` |
| `github.comment` | `POST /repos/{r}/issues/{n}/comments` |
| `subagent.request` | hub returns `child_session_id`; runner starts the child (§13) |
| `task.set_title` | device-authored `task.meta` event |

Rate limits: honour `Retry-After`, `x-ratelimit-remaining = 0` → wait until reset (≤ 60 s else retryable failure).
Every GitHub request carries `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, timeout
30 s, no redirects except for tarball downloads (to the codeload host of the configured web URL).

## 13. Subagents (ADR-017)

- Created only after hub acceptance of `subagent.request` (limits depth ≤ 2, ≤ 3 running children per task,
  ≤ 10 per task are enforced by the hub; the runner pre-checks the same numbers).
- Same VM, new sandbox, `depth = parent.depth + 1`, `parent_session_id` set, same task, same model access as the
  parent unless `model_id` given and allowed. Initial prompt = `"Purpose: <purpose>\n\n<prompt>"`; harness adds
  "You are a subagent session of session <parent> for task <task>; finish by answering; you may request further
  subagents only via subagent.request".
- One-shot: the first `Stop` ends it: final text = last assistant message (≤ 64 KiB), state `stopped`;
  `RuntimeMaxSec=30min` → `failed` `runtime_exceeded`. Parent gets ping `subagent.finished`; result via
  `get_subagent_result`.
- Full session in every respect: own events, terminal, hooks, audit, metrics; visible in the web as a child.

## 14. Metrics

Sampling every 15 s by the `metrics` task (sources and sinks in `08-monitoring.md`): host values, `limactl list
--json`, VZ process values, guest `/v1/metrics`. Outbox `metrics` items and, if configured, the textfile.

## 15. Claude Code updates

Version pinned in `images/versions.json` (`claude_code`, tarball sha256). An update is a PR that changes the pin
and MUST pass: model-double E2E (`07-testing.md`), the attack suite ATK (scripted adversary, hook fault mode,
poisoned settings, OS probe) on a golden built with the new version, and a check that no new routes/headers are
unknown to the model double (double fails on unknown routes). Then `agent-runner image build`; running VMs move
per §3.5.
