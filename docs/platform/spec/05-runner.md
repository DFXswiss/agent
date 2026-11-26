# 05 Runner: host daemon, VMs, sandbox, harness, hooks, sessions, model access, operations

> As built: where the implementation refines or deviates from this document, see the register
> [`spec-deviations.md`](../spec-deviations.md#05-runner) (DOC-01).

Status: binding. Components RUN (`agent`: `src/agent_runner/`), GST (`src/agent_guest/`) and SBX (`images/`,
`src/agent_guest/sandbox/`). Contracts referenced: `03-api.md` §6–§9, data `02-data-model.md` §4–§5, crypto
`04-crypto.md`. Decisions: ADR-008, -009, -010, -011, -012, -013, -015, -016, -017, -020, -021, -022, -024, -025, -068.
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
| `agent-runner enroll --hub URL (--code CODE \| --code-file PATH) [--platform]` | §2.1; `--code-file` reads the code from a file (at most 4 KiB), so it never appears in an argv (§16) |
| `agent-runner run` | foreground daemon (development, tests, launchd target) |
| `agent-runner status [--json]` | identity, hub connection, outbox backlog, VMs, sessions, pool, capacity |
| `agent-runner doctor` | checks: Lima ≥ 2.3, vz available, golden ready, Landlock ABI in golden (from build record), PostgreSQL, login command on the host (`login_cli`: `LOGIN_ARGV[0]` resolved like §11.2; for the default command its `--version`, with a time limit and the login's allow-list env, equals the golden's Claude Code pin, else warn `version_mismatch`; missing or not executable: fail `login_cli_missing`; details in `runbooks/runner-installation.md` §4), hub reachability and pinned key, textfile dir writable, the operator's subscriptions (`operator_subscriptions`: ok `off`/`usable` with the count of usable token files, warn `files_ignored`/`none_usable`/`dir_unusable`; no file name or organization; ADR-052), clock skew < 5 s against hub time, the operator's GitHub token file (`operator_github`: ok `off`/`usable`, warn with the reason; §11.4) |
| `agent-runner image build [--claude-version V]` / `image list` / `image retire VERSION` | §3.1 |
| `agent-runner pool status` | pool VMs and refill state |
| `agent-runner service install` / `service uninstall` | §1.1 |
| `agent-runner uninstall [--keep-identity]` | stop service, delete VMs created by the runner, delete store; identity only without the flag |

Output: one line per fact with fixed prefixes (`runner.status …`), JSON with `--json`; never secrets, tokens or
decrypted content (repository rule). Exit codes: 0 ok, 1 failure, 2 usage.

### 1.3 Configuration (`AGENT_RUNNER_*`, fail-closed)

Loaded once at start; a missing required or invalid value exits with `config_invalid <NAME>` (no value echoed).
The former fixed numbers of what may run at once - `SESSIONS_PER_VM`, `MAX_VMS`, `WORKVM_SLOTS`,
`WORKVM_MAX_PER_TASK`, `WORKVM_MAX_PER_OWNER` and `WORKVM_MAX_PER_ORG` - no longer exist (ADR-075 addendum 2): set
in an older `runner.env` they are ignored, never `config_invalid`, and the start names each once
(`runner.config.ignored name=<NAME> reason=fixed_limit_removed`). The device admission (§3.8) decides by free RAM,
CPU and disk.

| Name | Default | Notes |
|---|---|---|
| `HOME` | `~/.local/share/agent-runner` | 0700 |
| `HUB_URL` | required | `https://` (tests: `http://127.0.0.1…` allowed only with `AGENT_RUNNER_TEST_MODE=1`) |
| `PLATFORM_ROOT_KEY` | required | b64u Ed25519 public key |
| `VM_CPUS` / `VM_MEMORY_MIB` / `VM_DISK_GIB` | 4 / 8192 / 20 | per workgroup VM |
| `POOL_SIZE` | 2 | 0 disables the pool (clone path only) |
| `MEMORY_RESERVE_MIB` | 32768 | host reserve excluded from the VM budget |
| `VM_LOAD_MAX_PERCENT` | 85 | a running workgroup VM takes one more session only while its own free resources last: its 1-minute load per vCPU is at most this (10–100) and its free RAM is enough (§3.3 step 2, `vm.room`) |
| `CPU_LOAD_MAX_PERCENT` | 85 | the device admission (§3.8) starts a new VM of any kind only while the host's 1-minute load per core is at most this (10–100) |
| `SESSION_IDLE_SECONDS` / `VM_IDLE_SECONDS` | 1800 / 1800 | |
| `TEXTFILE_DIR` | empty = textfile sink off | node_exporter textfile directory (08 §3) |
| `MODEL_CATALOG` | packaged `model-catalog.json` | §10.5 |
| `OPENROUTER_KEY_FILE` | empty | platform devices only; file 0600; refused on org devices (`config_invalid`) |
| `OPERATOR_SUBSCRIPTION_DIR` | empty | platform devices only (refused on org devices); directory 0700 of the runner account, not a symlink, holding the operator's subscription token files `<name>.token` (§10.1, ADR-052); alone valid since ADR-079 (the organizations come from the root-signed `org_operator_subscriptions` of the platform log) |
| `OPERATOR_SUBSCRIPTION_ORGS` | empty = nobody | comma-separated organization IDs whose sessions may use the operator's subscriptions while the platform log has no `org_operator_subscriptions` of the organization (an entry of the log wins; kept for the move into the log, ADR-079 §3); needs `OPERATOR_SUBSCRIPTION_DIR` (the list without it: `config_invalid`) |
| `OPERATOR_GITHUB_TOKEN_FILE` / `OPERATOR_GITHUB_USERS` | empty / empty | the operator's own GitHub token (absolute path) and the operator's user ID (exactly one UUID; more is `config_invalid` `OPERATOR_GITHUB_USERS:one_user_only`, HOT-14); both or neither, platform devices only, refused on org devices (`config_invalid`); the file is checked at each read (§11.4, KOR-27, ADR-053) |
| `BROWSER_IDLE_SECONDS` / `BROWSER_LIVE_MAX_KBPS` / `BROWSER_SEARCH_URL` | 600 / 3000 / empty | KOR-44: idle teardown of a platform browser session (60–3600, `browser.open` may set its own), the cap of the live picture's bandwidth (100–100000 kbit/s), the prepared search switch (an http(s) URL template with `{q}`; empty = off, question F9; 13-work-vm.md §13) |
| `LOG_MAX_MIB` | 256 | KOR-43: the kept output of one test run or long work VM job, its last part (1–1000000; 13-work-vm.md §12.3) |
| `WORKVM_IDLE_SECONDS` / `WORKVM_KEEP_STOPPED_SECONDS` | 600 / 86400 | work VMs (ADR-059 addendum 4, §3.7): an idle work VM is stopped and gives its slot back (60–604800); a stopped one keeps its disk that long (600–2592000); the other `WORKVM_*` settings: 13-work-vm.md |
| `CONTEXT_INTERVAL_SECONDS` / `CONTEXT_CACHE_MIB` / `CONTEXT_WORKSPACE_MIB` / `CONTEXT_TRANSCRIPT_MIB` | 600 / 20480 / 4096 / 4096 | context checkpoints (§6.6, KOR-33): interval while working (30–86400), local encrypted cache cap (every object; the newest of each task is always kept), workspace above the limit is left out, transcript above the limit cannot be checkpointed |
| `CONTEXT_WAIT_SECONDS` | 180 | how long a restore waits for the final checkpoint of the session it continues and retries transient hub failures (0–3600, §6.6) |
| `OPERATOR_GITHUB_TOKEN_FILE` / `OPERATOR_GITHUB_USERS` / `OPERATOR_GITHUB_ORG` | empty / empty / empty | the operator's GitHub token (absolute path), the approved user IDs (comma-separated UUIDs) and the organization that owns the operator's account (an org UUID, KOR-47, ADR-078 §7); the file needs the users or the organization, each of them needs the file; platform devices only, refused on org devices (`config_invalid`); the file is checked at each read (§11.4, KOR-27, ADR-053) |
| `GITHUB_CLIENT_ID` | required for the device flow | public client ID of the GitHub App; users approved for the operator's GitHub account (§11.4) connect without it |
| `GITHUB_OAUTH_CLIENT_ID` | empty | public client ID of the OAuth App (device flow enabled, no secret) whose device flow "Connect GitHub" prefers (scopes `repo workflow`; KOR-65, ADR-077); without it the GitHub App's |
| `GITHUB_APP_SLUG` | empty | URL name of the GitHub App (`https://github.com/apps/<slug>`), for the install link of a held operation's next step (KOR-65) |
| `GITHUB_API_URL` / `GITHUB_WEB_URL` | `https://api.github.com` / `https://github.com` | doubles in tests |
| `MODEL_UPSTREAM_ANTHROPIC` / `MODEL_UPSTREAM_OPENROUTER` | `https://api.anthropic.com` / `https://openrouter.ai/api` | doubles in tests |
| `ANTHROPIC_USAGE_URL` | `https://api.anthropic.com/api/oauth/usage` | quota polling |
| `LOGIN_ARGV` | `["claude","setup-token"]` (JSON array) | login-CLI double in tests; the default needs Claude Code on the host at the golden's pin (doctor `login_cli`) |
| `SUBSCRIPTION_LOGIN_ARGV` | `["claude","auth","login","--claudeai"]` (JSON array) | the full login of a further subscription (§11.6, ADR-092); `--email <address>` is appended when known |
| `CLAUDE_TOKEN_URL` | `https://platform.claude.com/v1/oauth/token` | token endpoint of the renewal of further subscriptions (§10.6, ADR-092); loopback `http` only in test mode |
| `CLAUDE_OAUTH_CLIENT_ID` | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` | client ID of that renewal (the pinned Claude Code's) |
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
- A workgroup may have several VMs on a device (ADR-075); a VM belongs to exactly one workgroup for its life. The
  VM of a session is chosen under the in-process lock of its workgroup, and the session row names its VM before the
  lock is released, so two starts never both take the last place of a VM. Order (deterministic): (1) the VM the hub
  names (`vm_id_pref`, a user's choice) when it is a live VM of the workgroup; (2) for a subagent the VM of its parent,
  for a main session with `resume_from` the VM of that session – both regardless of their room; (3) a running VM of
  the workgroup with free resources of its own for one more session - no fixed number of sessions per VM (ADR-075
  addendum 2) -, measured on the VM (`vm.room`: the guest's `GET /v1/metrics`, 2 s; `MemAvailable` less what one more
  session needs - measured, no fixed number: the largest `memory.current` among its sessions that have not ended - leaves
  10 % of the VM's RAM, and the load per vCPU is at most `VM_LOAD_MAX_PERCENT`; a VM that does not answer counts as
  having room, since its own sandbox limits still hold; log `runner.vm.session_room`), smallest VM ID first.
  The same measurement decides whether a subagent fits on its parent's VM (`subagents.limits.vm_room_refusal`). A VM
  keeps the size it started with (it does not grow, ADR-075 addendum 3), so its fixed RAM and its sessions' measured
  use decide how many sessions it takes (DFX workgroup VM, 8 GiB, sessions of 350-510 MB: about 13-15). No VM with
  room: the order goes on unchanged - a stopped VM, then a further VM of the workgroup on a free slot of the device
  (§3.4, §3.8), else `capacity_exhausted` and the hub places the next attempt, ADR-013, ADR-049;
  (4) a stopped VM of the workgroup with room (restart; replaced by a VM of the newest golden instead when it may
  move, §3.6 – the new VM first, then the old one is deleted), smallest VM ID; (5) a new VM
  from the pool, else a clone, when a slot is free (§3.4) – without a ready golden yet (a device just added still
  builds it) `capacity_exhausted` (`golden`), not `vm_start_failed` (KOR-67); (6) else the session fails
  `capacity_exhausted`.

### 3.4 Capacity

`budget_mib = physical RAM − MEMORY_RESERVE_MIB`; `vm_slots_total = floor(budget_mib / VM_MEMORY_MIB)` - the memory
budget alone, no fixed number of VMs (ADR-075 addendum 2);
`vm_slots_free = vm_slots_total − (VMs in pool, assigning, running, stopping) − the slots other areas hold`. Stopped
VMs do not consume slots (RAM is returned on stop; Spike A high-water-mark finding). Reported in every heartbeat
(`capacity`, `02-data-model.md` §2.1). A session needing a new VM goes through the device admission (§3.8): CPU load
and disk first, then a free slot - when only idle work VMs hold it, they give way (`RECLAIM`, §3.7); else a pool VM is
given up, idle work VMs are stopped, or the least recently used idle VM of another workgroup (no running or suspended
session younger than `VM_IDLE_SECONDS`) is stopped; else the request fails with `capacity_exhausted` (the hub then
re-schedules: a new main session of the task, `03-api.md` §4.6.2, ADR-049; the refused session ID is never started
again). A device without a ready golden and without a VM (a new one that
still builds its first golden, ADR-074 amendment KOR-67) reports `vm_slots_free = 0` and `pool_ready = 0`, so the hub
places nothing on it until the golden is ready.

The capacity report also carries (all optional for the hub, ADR-075): `cpus` and `memory_total_mib` of the host,
`disk_free_mib` of the volume holding `$LIMA_HOME`, `vms_running`, `sessions_active` (occupying sessions),
`work_vms_running` (VMs of other runner areas, e.g. work VMs; areas report them through
`<area>/capacity.py:USAGE`, which returns their VM count and the memory they hold, both counted against the
budget). The former settings `sessions_per_vm` and `vms_max` are no longer reported (the hub treats their absence
as no limit). The enrollment body keeps the five base counters. The
live channel announces `log.org_assign` and `vm.choice` (`Agent-Features`, ADR-075).

### 3.5 Lifecycle rules

- Idle stop: no session in `working|idle|prompt_delivering|…` for `VM_IDLE_SECONDS` → `limactl stop`.
- Workgroup unassigned (also by `device_unassign_org`), workgroup archived, org removed this device, device
  decertified: stop sessions (`stopping` reason `unassigned`), `limactl delete -f` of every VM of the workgroup, delete scope keys of that workgroup after rotation is
  observed or immediately when removed from the org.
- Golden upgrade (ADR-093, §3.6): a workgroup VM keeps its golden version for its lifetime and is replaced by a VM
  of the newest ready golden once it may move – by itself (§3.6) or at the next start of the stopped VM (§3.3 step 4).
  The new VM is assigned and running before the old one is deleted. A later main session of the task, and a
  `suspended` session whose VM is gone, continues the original conversation from the task's newest context
  checkpoint (§6.6, ADR-072); only when no checkpoint can be restored does its harness say to read the history with
  `get_task_messages` (reported emergency path, `03-api.md` §4.6.3).
- Pool VMs of a retired golden are deleted and refilled.
- Startup reconciliation: `limactl list --json` vs `runner.vm`; unknown `arp-*` instances are deleted (one Lima
  refuses to delete is logged, `vm.unknown_delete_failed`, and reconciliation continues; the empty directory of an
  interrupted clone is removed); known instances missing in Lima are marked `deleted`.

### 3.6 Switch to the newest golden (ADR-093, KOR-70)

A live workgroup VM (`running` or `stopped`) whose golden is not the newest `ready` one is *outdated*. It **may
move** when every session on it that is not `stopped`/`failed` is a main session in `suspended` whose newest context
checkpoint is complete: not due, taken with workspace state `full` or `empty` (`runner.context_session.
checkpoint_state`, written with every taken or identical checkpoint, `dropped` when a request ended without one), its
newest object in the local cache and no failed restore from that object (retire hook `checkpoints`, §6.6) – or that
has nothing to save (HOT-24): it never worked (no prompt accepted for it on this device, `runner.prompt_delivery`) or
its newest request found no complete turn and it never had a checkpoint (`nothing`: guest `state_conflict`, archive
`context_incomplete`).
Otherwise it is *held*; each cause is reported with `since` (persisted, `runner.golden_switch_hold`):
`session_active` (a session that is not paused, also the main orchestrator, which is never suspended), `subagent` (a
child that has not ended: it has its own workspace and no checkpoint), `checkpoint_due`, `checkpoint_missing`,
`workspace_incomplete` (above `CONTEXT_WORKSPACE_MIB` or 100 000 entries, or entries left out),
`checkpoint_impossible` (HOT-24: the guest refused the export `not_found` – also a guest without the export route –
or `size_exceeded`; stored as `impossible` with `impossible_reason` and the accepted prompts at that moment; no request
makes the session due again until it worked again; with `checkpoint_at`, the save it would continue from), `capacity`,
`switch_failed`.

Task `vm-pool` runs the switch after every refill (60 s and after every assignment), requested VMs first, each under
its workgroup's lock (a session start waits meanwhile and then chooses the new VM):

1. a hold by a session, an incomplete workspace or `checkpoint_impossible`: nothing happens – the VM is neither started
   nor stopped for the switch;
2. a stopped VM held only by due or missing checkpoints is started to take them (when a slot is free without
   stopping another workgroup's VM; attempts 5 min apart, doubling, at most 3 until a new request by hand or until
   the VM ran for its sessions; kept across restarts in `runner.golden_switch_attempt`, HOT-24);
3. a running VM is stopped – the retire hooks take the final checkpoints of its sessions – and judged again; a hold
   then leaves it stopped as it is;
4. a pool VM of the newest golden, else a clone (no other workgroup's VM is stopped for it), is assigned and
   running; then the old VM is deleted (`vm_state` `deleted` with `replaced_by`) and `runner.golden_switch` records
   it. A failure deletes nothing (`capacity`, `switch_failed`; next attempt after the backoff).

The paused sessions keep their `vm_id`; their next prompt resumes them (§6.3) on a VM of the workgroup – normally the
new one – restored from the checkpoint; the restore note says "on the new version of the workgroup VM … your files
were carried over" and the audit `session.context_restored` carries `moved: golden`. The status goes to the hub with
`vm_state` (`upgrade`, `null` once current) whenever it changes, only to a hub whose desired state names `vm_switch`
(an older hub refuses unknown fields). The desired state's `vm_switch` lists the VMs a member with administration
rights asked to switch now (`POST /v1/workgroups/{wg}/vms/{vm}/switch`); reconcile step `vm_switch` (order 85) hands
them to the switch, which applies the same rules – with one exception (HOT-24): a request covers the
`checkpoint_impossible` holds the device had reported before the time of the request (`vm_switch_at`, only from a hub
with `vm_upgrade_version` ≥ 2); those sessions move and continue without the state of the old VM (from the newest
checkpoint of the session or its task, else fresh). The requests taken over are kept in
`runner.golden_switch_request`, so a request still listed after a restart is no new one. Without a request such a VM
stays as it is, and a start of the workgroup restarts it (§3.3 step 4).

### 3.7 Work VM slots (ADR-059 addendum 4)

Work VMs (13-work-vm.md) run in slots, one macOS account with its own Lima home and pinned SSH port each (13 §6.1).
The slot assignment of `agent_runner/workvm/manager.py` is the only mechanism that hands out and frees slots; a slot
counts only while its VM is needed, and **the free resources, not a fixed number, decide how many run**:

- **Slot accounts** (`workvm.accounts.slot_count`): as many as the RAM above `MEMORY_RESERVE_MIB` could ever hold
  standard work VMs (`(RAM - reserve) / 8192`, at least 1, at most 100 - the port range of the accounts) - isolation
  units, not a number of what may run: they never limit before the resources do. `agent-runner workvm install` creates
  them (again after more RAM: it adds the missing ones); the daemon offers work VMs once the first slot account exists
  (checked at the first use, once per daemon). The former `WORKVM_SLOTS` and `WORKVM_MAX_PER_TASK`/`_OWNER`/`_ORG` are
  gone (§1.3).
- **Admission** (`WorkVms._fits`, for every new or restarted work VM, browser VM, test run VM and the work VM pool): the
  device admission (§3.8) as kind `work`: the VM's RAM fits into the RAM above the reserve that the workgroup VMs
  (also those being started) and the work VMs leave, less what waiting requests of a kind with fewer active instances
  (or of the same count and older) need, the host's 1-minute load per core is at most `CPU_LOAD_MAX_PERCENT`, the disk
  quota `WORKVM_DISK_QUOTA_GIB` holds, and a slot account is free. Otherwise the request waits in the queue (below).
- **Idle and speculative VMs give way**: the running task and session work VMs and the work VM pool are reported as yielding
  (`Usage.yielding_vms`/`yielding_mib`, `workvm/capacity.py:USAGE`): they take no workgroup slot (`vm_slots_free` of
  the capacity report is not reduced by them; `work_vms_running` still counts them). A workgroup VM start (§3.4
  `_free_slot`) whose slot is free only because of them asks the areas' `RECLAIM` for `VM_MEMORY_MIB`
  (`vm.slot_from_work_vms released_mib=`): `WorkVms.give_way` drops ready work VM pool entries, then stops idle work
  VMs (no job and no call in progress; `workvm.state` `stopped`, reason `idle`, disk kept), least recently used first
  (`runner.workvm gave_way need_mib= released_mib=`). Running work is never ended for it: a VM with a job or a call in
  progress keeps running, and the session waits for room or is placed again by the hub. The workgroup pool refill
  uses only room nobody holds (`Capacity.slots_free_now`). Run and browser VMs are not yielding. A work VM never runs
  on another device than its session (ADR-059 decision 11): a request that does not fit waits; the hub places new
  sessions by the reported capacity.

- **Held** by a work VM in `starting`, `running`, `stopping` or `deleting` and by a warm pool entry
  (`workvm.records.SLOT_STATES`; unique index `workvm_slot_held`, migration `0038_workvm_slots`). A `stopped` VM keeps
  its disk in its slot's Lima home and holds neither the slot nor RAM.
- **Settings**: `WORKVM_IDLE_SECONDS` (default 600, 60–604800) and `WORKVM_KEEP_STOPPED_SECONDS` (default 86400,
  600–2592000).
- **Freed at once** when the task is closed (reconcile step `workvm`, `task_closed`) or the session ends (observer of
  `session.state`, `session_ended`; the task VM when no session of the task is active any more).
- **Freed when idle**: every minute the idle pass stops (`limactl stop`, `workvm.state` `stopped`, reason `idle`) a
  task or session VM with no tool call for `WORKVM_IDLE_SECONDS`, no running job (`workvm-job list`) and no call in
  progress. Idle is the runner's activity record (the start and the end of every call), never CPU. Run and browser VMs
  belong to their owner (13 §12, §13) and are not stopped here.
- **Stopped VMs**: the next tool call or `workvm.request` of the session starts it again with its disk (`created`
  again, `created:false`). It starts only in its own slot (its disk is in that slot's Lima home; the host filter opens
  only that slot's port for it): it waits in the queue until that slot is free and its RAM fits; a ready pool entry in
  that slot gives the slot back. A VM that does not start is deleted (`failed`) and the call obtains a new one; a
  call whose stopped VM still waits when the queue time ends answers `unavailable` ("The stopped work VM could not
  start yet: …") and the VM stays stopped. A stopped VM is deleted after `WORKVM_KEEP_STOPPED_SECONDS` (`idle`), with
  its session or task (above), when the disk quota needs its space for a waiting request (the oldest first, `idle`),
  and at a restart. Every deletion is a `workvm.state` `deleted` event with its reason: nothing goes silently.
- **New VMs** take the free slot with the fewest stopped VMs (then the lowest number), so the own slot of a stopped VM
  is mostly free when it is needed.
- **Queue** (`WORKVM_QUEUE_SECONDS`, 13 §2): requests wait in the order they came in. A request goes when every
  request before it is a stopped VM whose own slot another VM holds (it could not start now anyway); a request that got
  its slot keeps the ones behind it waiting until its VM runs. Every slot, RAM or place in the queue that becomes free
  (a deletion, a stop, a pool entry dropped, a request leaving) wakes the waiting requests at once; the 2 s poll stays.
  The waiting requests are mirrored in `runner.workvm_wait` (ticket, order, own slot of a stopped VM, since) for the
  status; the mirror is display only.
- **Reconciliation at start** (task `workvm`, before the first pass): every VM left over from before the restart is
  deleted (`restart`, KONZEPT idea 44), every own `wvm-` instance without a row too, and the mirror rows of requests
  of the old process are removed (`runner.workvm waits_cleared count=`).
- **Slot pass** (task `workvm`, every minute after the idle pass): per slot `limactl list` (a slot whose listing fails
  is skipped this time, `runner.workvm slot_check_failed slot= code=`), then for every row that is not `deleting` (the
  sweep's) and not being started, stopped or deleted right now:
  - instance missing → deleted (`failed`; its open runs `lost`);
  - `starting` without a request that starts it → deleted (`failed`);
  - `running`/`stopping` but the instance does not run → `stopped` (`workvm.state` `stopped`, reason `failed`; disk
    kept); a run or browser VM is deleted instead (its owner obtains a new one);
  - `stopping` and the instance runs (an interrupted stop) → `running` again (the idle pass decides);
  - a `running` or `stopped` task VM whose task has no active session on the device, or session VM whose session is
    final or unknown → deleted (`session_ended`; the end was missed).
  Log `runner.workvm slot_released workvm_id= state= cause=instance_missing|start_interrupted|not_running|sessions_ended`
  and `slot_settled`.
- **Visible**: the chat shows `stopped` with reason `idle` as one quiet line ("Arbeits-VM angehalten, ungenutzt –
  startet bei Bedarf neu"); the metrics `agent_runner_workvm_slots{state}` (`held`, `free`),
  `agent_runner_workvm_requests_waiting` and `agent_runner_workvm_stopped` (08 §3); `workvm_status` reports a stopped
  VM as `stopped`; the capacity metrics `agent_runner_workvm_memory_free_bytes`, `agent_runner_workvm_cpu_load_ratio`
  and `agent_runner_workvm_disk_used_bytes` (08 §3); `agent-runner status` prints `runner.status.workvm slots= held=
  free= stopped= waiting= oldest_wait_s= memory_free_mib= cpu_load_pct= cpu_load_max_pct= disk_used_gib=
  disk_quota_gib=<n|none>`.

### 3.8 Device admission

ADR-059 addendum 4, ADR-075 addendum 2, 16-ci-runners.md §6.6. One admission per device decides for every kind of VM
whether it may start now (`agent_runner/admission.py`, `admission.of(context)`, one instance per daemon). There is no
second mechanism and **no fixed number and no fixed share of what may run**, for no kind: a kind alone on a device may
use it up to 100 %.

- **Fewest active first** (decision of the client, 2026-11-26: "immer der von dem es am wenigsten aktive gibt"): when
  room becomes free or several requests come at the same time, the waiting request of the kind with the fewest active
  instances on this device goes first, on a tie the older request. Kinds: `session` (workgroup VMs: active = those that
  hold a slot), `work` (work, browser and test run VMs: active = not stopped), later `ci`; the areas register their
  counts (`Admission.counters`). A request that waits holds a ticket (`Admission.waits`: kind, RAM, since): a work VM
  request in its queue, and a session the device refused for lack of capacity as a hold that keeps the age of the
  first refusal until the hub's next attempt starts here (`refused`/`started`, at most `REFUSAL_HOLD_S`, 120 s). A new
  request may start only with the RAM the waiting requests ahead of it leave (`ahead_mib`, reason `turn`); with room
  for all, all start. Within a kind its own queue keeps the order (work VMs FIFO); CI requests carry a group
  (organisation, class) and the same rule holds between the groups.
- **One transaction**: the workgroup VM area and the work VM area take the same lock (`Admission.lock`;
  `Pool.slot_lock` and `WorkVms.slot_lock` are this lock) from the check to the row that holds the capacity. RAM of
  workgroup VMs being started or filled into the pool counts while they start (`Admission.pending`); the warm pool of
  workgroup VMs fills only room no waiting request needs.
- **Checks** (`Admission.judge`, reasons `cpu`, `memory`, `disk`, `macos`, `turn`): the host's 1-minute load per core
  is at most `CPU_LOAD_MAX_PERCENT` (default 85); the VM's RAM fits into the RAM above `MEMORY_RESERVE_MIB` that the
  running VMs of every kind leave; the disk it needs is free; at most **2 macOS VMs per Mac** (`MACOS_VMS_MAX`, Apple's
  licence, 16 §8.4) - the only fixed number; then the turn (above).
- **Running work is never ended** to make room. What gives way: warm pool entries, then idle work VMs (§3.7, stopped
  with their disk, started again on demand).
- A workgroup VM refused here fails its session with `capacity_exhausted` (`cpu`, `disk`, `turn` or no slot), the hub
  places the next attempt - here or on another device where it fits; a work VM request waits in its queue (§3.7).
- **Visible**: `agent-runner status` prints `runner.status.admission kind= active= waiting=` per kind (from the store:
  the VM rows and the mirror `runner.workvm_wait`, which holds the work queue and the session holds with their kind),
  the metrics `agent_runner_admission_active{kind}` and `agent_runner_admission_waiting{kind}` (08 §3).

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
  [--resume <claude_session_id>] [--model <model_id>]
```

The command line carries no prompt (HOT-09): Claude Code connects its MCP servers in the background and runs a turn
from a command-line prompt at once, so that turn would miss the platform tools. The initial prompt is delivered into
the TUI like a follow-up once the agent listed the platform tools (step 6).

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
| Syscalls | `SystemCallFilter=@system-service landlock_create_ruleset landlock_add_rule landlock_restrict_self`, `SystemCallFilter=~@privileged @mount @debug @module @reboot @swap @obsolete @cpu-emulation`, `SystemCallFilter=~bind:EPERM listen:EPERM io_uring_setup:EPERM io_uring_enter:EPERM io_uring_register:EPERM` (no socket name another session could reach, ADR-064 §4; the launcher also refuses `setsockopt(SOL_SOCKET, SO_PASSCRED/SO_PASSPIDFD)` by seccomp) |
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

Context checkpoints (ADR-072 §§2, 7; `agent_guest/contexts.py`; review KOR-33): the export reads only the
conversation's own files below `home/.claude` (`projects/<cwd key>/<id>.jsonl`, its companion directory,
`todos/<id>*`, `file-history/<id>/`; every `.jsonl` cut after its last line feed) and the workspace, as root with
`O_NOFOLLOW` and only files of the session user; it never exports settings, `.claude.json`, commands, agents, skills
or credentials. The workspace keeps execute bits (`0755`/`0644`), empty directories and links whose target stays
inside it; protected names (`.git`, `.claude`, `CLAUDE.md`, `AGENTS.md`, ...), links leaving it, devices, foreign
and unreadable files are left out and counted (`context.json` `workspace_omitted`). A staged context lives in
`/srv/sessions/.contexts/<id>` (root 0700, hidden from sessions by `TemporaryFileSystem=/srv/sessions`, removed
after 6 h unused); the stage accepts only the files of the conversation named in `context.json`, does not parse
transcript lines (integrity comes from the runner's AEAD, digest and signed manifest), and already replaces the old
session directory and cwd key byte for byte with the new ones in the transcript files and names (entries are never
filtered or reordered) and writes the transcript baseline. The restore in `POST /v1/sessions` only moves the staged
files into the sandbox - replacing those of a `suspended` session of the same ID - before `sandbox_ready`, hands
everything to the session user (without following links) and starts `claude --resume <id>` (same conversation ID,
the same file is continued). The workspace scanner starts afterwards and takes the restored tree as its baseline;
the transcript follower skips the records of the restored transcript (`transcript-baseline.json`), so a restore
posts neither `session.file` nor earlier turns again. Before an export reads anything, the conversation file must be
quiet: the export waits (polling every 0.1 s, at most 20 s) until the file was not written for 1 s and - for an
`idle` session - until its last conversation record is a final assistant answer (no `tool_use` block); `working` and
starting sessions are exported at once. A conversation that then holds no complete turn (no `user` and no
`assistant` record) is `409 state_conflict`: there is nothing to save yet (KOR-33 E2E: the `Stop` hook reports
`idle` before Claude Code has written the turn; such checkpoints held only a few hundred bytes and their restore
failed the agent's start).

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
| `agent-guest-control` | root | `/run/agent-guest/control.sock` (root:`agent-ctl`, 0660) and `/run/agent-guest/api.sock` (root:`platform`, 0660) | control protocol `03-api.md` §7; session users, directories, units; tmux server on `/run/agent-guest/tmux.sock` (root 0600); recorder; transcript reader; file import/export; context export, staging and restore (ADR-072, `contexts.py`); workspace scanner; journal owner (SQLite); pending MCP calls; pings; metrics (cgroup files) |
| `agent-guest-api` | `platform` (system user, no login) | `127.0.0.1:7700` | `/mcp`, `/hooks/<Event>`, `/model/…`; forwards journal records and pending calls to control over `api.sock` (JSON lines request/response); the API process may journal only `session.hook`, `usage`, `ping.delivered`, `guest.error` and `platform.refused`, every other kind is written by control (KOR-07) |

Group `agent-ctl` contains the Lima default user (so Lima's socket forwarder can connect) and nobody else.
Both services: `Restart=always`, `NoNewPrivileges=yes`; api additionally `ProtectSystem=strict`, `ProtectHome=yes`,
`PrivateTmp=yes`, no write paths. Hook and MCP handlers time out after 10 s internally (below the 15 s hook
timeout).

### 5.2 Hook events

The 18 events: `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `PermissionRequest`, `PermissionDenied`, `Notification`, `Stop`, `StopFailure`,
`SubagentStart`, `SubagentStop`, `PreCompact`, `PostCompact`, `ConfigChange`, `InstructionsLoaded`, `CwdChanged`.
Every call is journaled as `session.hook` (`03-api.md` §7.2), except the routine ones: `PreToolUse` `allow` and
`PostToolUse` `none` (without a reason) are counted per session in the journal of control and written as one
`session.hook` summary `{event:"summary", decision:"allow", counts, tools, from, to}` at most every 10 s, before any
other hook or state record of the session and by a one-second ticker (ADR-086 §3, `agent_guest/hook_summary.py`,
setting `limits.HookLimits`; 0 turns it off). Denies, blocks, faults, `UserPromptSubmit`, `Stop` and every other
event stay single records in their place. `SessionStart` is not delivered over HTTP in
2.1.330; the session start is journaled by control when the unit starts. `SubagentStart` must never occur (tools
denied); if it does it is journaled with `decision:"none"` and raises `guest.error subagent_hook_seen`.

### 5.3 PreToolUse policy (deterministic, `agent_guest/policy.py`)

Evaluated in order; first match decides:
1. Tool `mcp__platform__<t>` with `<t>` in the tool list of `03-api.md` §8.1 → allow.
1a. Tool `AskUserQuestion` of a main session (KOR-39, ADR-076 §3) → the guest requests the operation `human.question`
   for the session with `{question}` built from the tool input (each question with its options as a list, cut to
   4000 characters) – sealed and registered like every operation – and denies the tool with "Your question was passed
   to the person through agent.dfx.swiss. End your turn now; the answer arrives as your next prompt." (a subagent:
   rule 2).
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
An accepted row's `speaker` (the sender line the runner registered with it, ADR-083 §1) is returned by `prompt.check`
in the same transaction and becomes the first part of `additionalContext` of the answer (addon texts follow after a
blank line); it is part of the decision, not an addon. A row without it gets no line.

### 5.5 Other hook handling

- `PostToolUse` for `Write`/`Edit`: journal `session.file` (path, sha256 of the new content read by control,
  size, patch ≤ 64 KiB) and `session.tool`.
- `Stop`: main session → state `idle`; subagent session → completion (§13). `StopFailure` → `session.status`
  with error.
- `PermissionRequest`, `PermissionDenied`: journal only (mode `dontAsk` never asks).
- Fail closed (ADR-064, KOR-40; replaces the former fail-open): Claude Code itself goes on when a hook request fails
  (500/timeout), so the guest closes the path around it. The gateway forwards a `/v1/messages` request with tools
  (or `tool_choice`, `mcp_servers`) only during a turn the `UserPromptSubmit` hook confirmed (session `working`),
  else `403` and `guest.error turn_unconfirmed`. A failed decision hook (`UserPromptSubmit`, `PreToolUse`, `Stop`,
  `StopFailure`: fault, failed control call, exception, answer later than the hook timeout) ends the session with
  `failed hook_unavailable`. Layer 1 denies and the layer 2 sandbox remain; the attack suite proves layer 2 alone
  with the switch `open` and the production reaction with `500` (ATK-38 `outage`). Hook failures are counted
  (`agent_runner_session_hook_errors_total`).
- Pings: see §9; the `additionalContext` text is exactly the one of `03-api.md` §8.2.
- Session notice (KOR-33, addon `hook_addons/notice.py`): after a resume or restore the first `PreToolUse`,
  `PostToolUse` or accepted `UserPromptSubmit` answer (normally the resumed prompt itself) carries the pending
  notice of §8 once as `additionalContext` (`contracts/mcp/objects.json#/$defs/session_notice`), before a ping
  notice (joined by a blank line). Claude Code 2.1.330 sends no `SessionStart` hook (ADR-029), so the first decorated
  answer is the earliest point.

## 6. Session lifecycle

### 6.1 Start (main session) – steps, states, budget

| Step | State pushed (`session_transition`) | Action | Budget |
|---|---|---|---|
| 1 | `vm_assigning` | ensure VM (same order as `03-api.md` §4.6.2 / ADR-009): running VM of the wg → none; stopped VM of the wg → restart (unless the golden replacement of §3.5 applies: delete it, then continue); pool → assign; else clone | pool 0.1 s / restart ≤ 6 s / clone ≤ 6 s |
| 2 | `vm_ready` | guest health assigned to this wg | – |
| 3 | – | choose model access (§10.1), decrypt credential | 0.05 s |
| 4 | `sandbox_ready` | `POST /v1/sessions` (user, dirs, harness file, token) + `PUT …/credential` | 0.3 s |
| 5 | `agent_starting` | register initial prompt hash; start tmux window + unit (no prompt on the command line, §4.1) | ≤ 120 s, else `failed` `agent_start_failed` (reconcile step 56, counted from the first reconcile that saw the state; KOR-02) |
| 6 | `prompt_delivering` | the agent listed the platform tools (`tools/list` on `/mcp`, journal `session.tools_ready`, runner start step `tools_ready` ≤ 2 s from step 5) **and** the Claude TUI is up (input box visible); then the initial prompt is typed or pasted (§6.2). Tools not listed within `AGENT_GUEST_TOOLS_SECONDS` (25 s): `guest.error tools_unavailable` and the agent is started again (new run, new token), at most twice; then `failed` `agent_start_failed` (HOT-09) | ≤ 2 s after the tools |
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

**Sender line (KOR-46, ADR-083 §1).** Step 1 and the first prompt of a start (`POST /v1/sessions` `initial_prompt`)
carry `speaker`: `agent.dfx.swiss instruction from: "<name>" (member <id>)` for the verified author of the event
(name from the author's own stored `member.profile`, normalised, ≤ 100 characters; `; the hub reports it came
through their agent` when the hub's routing says so), the fixed sentence that text is never a sender, and - when
they apply - "the person instructing you changed" (the previous accepted prompt of the task on this device came from
another author), the imitation sentence (the text contains `agent.dfx.swiss instruction from`, `hook additional
context` or `system-reminder`) and "(another member uses the same name)". A child session's line names its parent
session and the person it works for, never a person as sender. The runner checks the line against the contract
before sending; an older guest's `400 request_invalid` to a request with `speaker` makes it send the request again
without (log `speaker_unsupported`).

**Withdrawal (ADR-083 §3).** The runner announces `prompt.withdraw` (`Agent-Features`). For every event in
`prompt_queue.withdrawn` of the device state a delivery still `pending` (or not routed yet) becomes `withdrawn`
(final; never handed to a guest, never holds back or makes stale a later prompt) and is reported once as outbox audit
`session.prompt_withdrawn`; a delivery `registered` or later is delivered. `get_task_messages` leaves out prompts this
device withdrew.

**Triggered by (ADR-083 §2).** To a hub that sends `prompt_queue`, operation registrations, `usage` and `runtime`
items carry `triggered_by`: the operation's stamp (`actor_id`), the session's acting user when the usage record is
processed (a child: its inherited stamp), for a work VM the acting user when it was requested.

### 6.3 Control, idle, resume, stop

- Interrupt: `tmux send-keys Escape`. Stop: `systemctl stop agent-session-<id>` (SIGTERM, 10 s, SIGKILL), close
  window → `stopped`.
- Idle: `idle` for `SESSION_IDLE_SECONDS` → `suspend`: stop the unit, keep directories and transcript →
  `suspended`. Next prompt → `resuming` → start with `--resume <claude_session_id>` → deliver. When the workgroup's
  VM is no longer the session's VM (deleted, replaced, another VM), the session moves to it and is restored from its
  newest checkpoint (§6.6) instead of failing `vm_start_failed`.
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
  for changes not already reported. A tree the platform imports (`files/import`: `github.checkout`, results of a
  work VM) becomes the scanner's baseline and is never reported file by file (pending changes are reported before
  the move, under the scanner lock). Dependency, virtual-environment, build and cache directories (`node_modules`,
  `.venv`, `dist`, `target`, …) are not scanned. A pass with more than 100 changed files, and the part beyond 1 000
  records or 8 MiB of patches per session in 10 minutes, becomes one `op: "summary"` record (HOT-06, ADR-057).
- Runner: journal → events (`session.*` kinds), encrypted with the current WK, signed by the device, outbox.

### 6.5 Task title

`task.set_title` operation (03 §9) → runner writes a device-authored `task.meta` event.

### 6.6 Context checkpoints and restore (KOR-33, ADR-072)

- Area `agent_runner/contexts/`. A checkpoint of a main session is due when the guest reports `idle`, `suspended`,
  `stopped` or `failed` (observer `contexts.checkpoint`), every `CONTEXT_INTERVAL_SECONDS` (600) while it is
  `working`, and before its VM is stopped (idle, LRU, unhealthy) or deleted (retire hook `contexts.checkpoint` of
  `vm/extensions.py`, bounded to 120 s; a stopped VM with due checkpoints is restarted instead of replaced by a newer
  golden). A due checkpoint of a VM that exists but does not run stays due until the VM runs or is deleted.
- The daemon task `contexts` runs two loops: exports (up to four at a time, one per session, coalesced) and uploads.
  Export: guest `POST /v1/sessions/{id}/context/export` (workspace limit `CONTEXT_WORKSPACE_MIB`, transcript limit
  `CONTEXT_TRANSCRIPT_MIB`, 4096 each) into the runner's `tmp/`; untrusted-archive checks (`github.tarsafe`; links
  only inside `work/`; transcript lines are not refused); an archive whose transcript and workspace members are
  unchanged ends there; gzip level 6, 2 MiB pieces sealed with `k_ctx` under the workgroup's current epoch
  (`crypto.context`); signed manifest; local cache `<home>/contexts/` (`CONTEXT_CACHE_MIB`, 20480). Every CPU or
  disk bound step runs in a worker thread. Upload: `PUT /v1/device/contexts/{object}/chunks/{i}` then `POST …/commit`
  with backoff on transport failures, `unavailable` and `rate_limited`; `404` on the first chunk (a hub without the
  route) or any other refusal (`quota_exceeded`, …) keeps the object local (`refused`); `404` after accepted chunks
  or a commit refused for missing chunks (the hub expired the upload) makes the checkpoint due again as a new object.
  Tables `runner.context_session`, `runner.context_object`, `runner.context_pr_link` (migration `0017_context.sql`).
  KOR-43 (migration `0022_test_runs.sql`): `runner.context_object.kind` (`context`, `log`), `runner.test_run` (one row
  per test run, 13-work-vm.md §12.2), `runner.workvm` scope `run` and the teardown reasons `run_ended`,
  `credit_exhausted`, `session_paused`.
- Restore (request amender `contexts.restore`, also for a resume whose VM changed; the session's `vm_id` changes only
  after the guest accepted the create): first the final checkpoint of the session continued (`resume_from`, or the
  session itself): on this device until it is taken or given up, on another device until the device state names a
  checkpoint of that session, at most `CONTEXT_WAIT_SECONDS`; then the newest of the cached objects (refused ones
  included) and the desired entry's `context`, ordered like the hub (session first, then UUIDv7). Hub reads are
  retried with backoff within the same limit; `404` ends at once. Checks: manifest signature with the key of a device
  assigned to the workgroup at the manifest's epoch, IDs, digest, AEAD, archive checks, `context.json`; a damaged
  cached copy is read again from the hub when it holds it; version rule: target Claude Code ≥ the highest `version`
  of the transcript lines. Then `POST /v1/contexts/{session}/stage` (the guest's heavy work happens there),
  `restore {claude_session_id, object_id, note}` in `POST /v1/sessions` (longer timeout); `note` is the one sentence
  about what changed (same VM / another VM / another device, checkpoint time, workspace and what was left out, stale,
  work VM), which the guest delivers with the current session facts as the session notice of §8 - not in the
  harness file, which a resumed model never reads. An archive whose conversation holds no complete turn is refused
  (`context_incomplete`): never sealed at the export, `context_invalid` (emergency path) at a restore. Audit
  `session.context_restored {object_id, workspace, stale}` once the guest reports `agent_starting`;
  otherwise `RESTART_LINE` and audit `session.context_fallback {reason}` (`context_missing`, `context_invalid`,
  `context_unavailable`, `version_incompatible`, `restore_failed`). A checkpoint whose restored agent failed inside
  its start window is not used again on this device.
- Restore after a device failure (ADR-087 §3.5): a session the device state names in `failovers {session_id,
  from_session, cancelled_runs}` with `from_session` = its `resume_from` does not wait for the final checkpoint of
  `from_session` (the failed device never sends it); it restores the newest of the cache and the desired entry's
  `context` (the checkpoint the hub pinned at the move). Its `note` is the failover sentence: its own earlier
  conversation, continued on a VM of another device because the device it ran on failed (when the checkpoint's author
  is this device: continued on this device after it was restarted following a failure, §3.7), from its checkpoint of
  <manifest `ts`>; the workspace text; work after that checkpoint may be lost and the interrupted instruction is
  repeated next; when `cancelled_runs` > 0, that number of test runs was cancelled there and is to be started again
  when their results are needed; the work VM sentence. Log `context_restored … failover=true`. A malformed
  `failovers` entry is ignored; an old hub sends none. The interrupted instruction (ADR-087 §3.4): a prompt whose
  delivery row is `accepted` for the lost session is moved to the successor as `pending` when the hub routes it there
  and delivered once more (`queue.route(..., failover_from=)`); every other final row stays final.
- Lost VMs after a restart (ADR-087 §3.7): once the startup reconciliation of the VM area ran, the task `guestlink`
  fails every session that has not ended, is not `idle` or `suspended`, and whose VM row is `deleted` or missing (a
  host reboot took the instance) with `guest_unreachable` through the outbox (`SessionService.vms_lost`, log
  `vm_lost session_id=`), so the hub places a successor instead of showing the task working.
- Work VM lease (ADR-087 §4): `HubClient.contact` is the wall-clock time of the last verified hub answer (a 2xx
  answer or a verified refusal; the client's creation before the first one; a monotonic clock stands still while the
  host sleeps). A negative age (the clock went back) counts as no contact. While it is older than `failover_seconds` of
  the last desired state (default 300 before the first state, from an old hub, or when malformed), `workvm_exec` and
  `workvm_browse` - and the implicit work VM they would start - are refused with the tool error `unavailable` (the
  device may have been declared failed and its sessions moved; these tools reach the internet without a hub
  registration). The other work VM tools, running jobs and running turns go on: a hub outage stops no device.
- Pull requests: every succeeded `github.create_pull_request` of the device is registered once as an opaque tag
  (`POST /v1/device/pr-links`, ADR-072 §8).

## 7. Terminal recording (ADR-016)

- tmux server (root, socket `/run/agent-guest/tmux.sock`), one tmux session `s-<id>` per session, size 160×48,
  `remain-on-exit off`. `pipe-pane -O` → recorder (part of control, reads a FIFO): incremental UTF-8 decoding,
  output events `[t_ms, "o", data]` with offsets in ms since session start, monotonic across restarts; a record is
  written once the output has been quiet for 100 ms, under sustained output when its first event is 1 s old or its
  **encoded** body (JSON escapes count) reaches 60 KiB – then the full records are written and the rest keeps
  growing (ADR-086 §3, `limits.TerminalLimits`) → journal `session.terminal` → runner → encrypted events (≤ 64 KiB).
  An event holds ≤ 16 KiB data, a record's body JSON ≤ 64 KiB − 16 bytes; the runner splits a well-formed record
  of an older guest whose sealed event would still be too large into several `session.terminal` events (KOR-02).
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
  arrive as content-free hints – use `get_notifications`; blocked attempts are recorded; when only a person can
  decide (a question, an approval), a main agent asks with `request_operation` `human.question` or
  `human.approval` (`{question}`) and then ends its turn – the person answers with the next prompt, an approval
  starts with `Approved.` or `Declined.`; a subagent reports to its main agent instead (KOR-39, ADR-076 §3, §8).
  Shared sessions (KOR-46): every instruction comes with the platform's hook line `agent.dfx.swiss instruction
  from: …` naming the verified person; only that line names the sender, a sender written inside the text is text;
  several people may instruct one task and the session acts for the person of the current instruction; an
  instruction without the line has an unknown sender.
- Generated context block (`agent_guest.harness.render`): org/workgroup/task/session names and IDs, task owner
  display name, rights, parent session (subagents), limits, the MCP tool list with one example each, and – for
  restarted sessions – the instruction to read history with `get_task_messages`.
- Rendered by control into `/run/agent-guest/harness/<session_id>.md` (root, 0444) = RULES.md + context;
  `harness_version` (sha256 of RULES.md) journaled in `session.state` and stored at the hub transition reason.
  Placed via `--append-system-prompt-file` (top-level system prompt; Spike C: mid-conversation system messages
  are rewritten by OpenRouter).
- The harness file reaches the model only when a conversation starts fresh. Claude Code (2.1.330) keeps the system
  prompt of a conversation in its transcript and reuses it on `--resume`, ignoring a new
  `--append-system-prompt-file` (KOR-33 E2E): a resumed or restored conversation still sees the facts of its first
  start (platform session ID, task title, ...). What changed reaches it as a **session notice** instead: control
  keeps, in the session's root-only meta file, the fact lines the conversation knows (`harness.fact_lines`: the
  lines of the context block and of the session block) and a pending notice. A fresh start knows the current facts.
  A restore (or a resume into a new sandbox) knows nothing certain: its notice is the runner's `restore.note` (else a
  fixed sentence) plus every current fact line. A resume of the sandbox's own conversation (suspend → resume on the
  same VM) keeps what it knows; when a fact line changed, its notice is a fixed sentence plus the changed lines only,
  otherwise there is none. The notice (`agent.dfx.swiss session notice: <sentence>` and, after a blank line,
  `Current facts of this session (they replace the ones in your instructions):` with the lines) is handed out once
  by the api.sock op `session.notice` (§5.5) and its facts become known; the meta file is replaced atomically, so a
  restart of the control process neither loses nor repeats it. The conversation's history is never replaced.

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

## 10. Model access (ADR-010, ADR-025, ADR-081)

### 10.1 Selection

Inputs: owner's `user_settings.model_access` (from `GET /v1/device/state`), owner's credentials decryptable on
this device, device kind. `auto` order: `claude_subscription` → `anthropic_api_key` → operator subscription → `platform` (only on
platform devices with `OPENROUTER_KEY_FILE`). **Operator subscription** (ADR-052, runner only): on a platform device
with `OPERATOR_SUBSCRIPTION_DIR`, a session of an organization granted in the verified platform log
(`org_operator_subscriptions`, ADR-079; without such an entry: listed in `OPERATOR_SUBSCRIPTION_ORGS`) takes a usable
token file of the operator's pool (the file with the fewest bound sessions, then the one unused the longest); the
explicit preference `claude_subscription` takes it too when the owner has no own subscription on this device. It is
reported as `claude_subscription` (`session_transition`, web); only the runner log and the textfile metrics show the
pool. Token files: regular files of the runner account, mode 0600, one access token in the format of §11.2, opened
with `O_NOFOLLOW` and checked before reading (anything else is ignored, logged once by name and never read).
**Shared subscriptions** (KOR-69, ADR-090 §3) join the pool as entries `sub-<record ID>`: the further subscriptions
the desired state lists in `subscriptions.shared`, each fetched and verified like every record of its owner and used
for a session only when the record's **sealed** `use` allows the session's organization (`orgs` names it, or `pool`
while the owner holds the platform role `operator` and the verified platform log grants the organization the
operator's subscriptions); a record without a token or a use is no entry. They follow the rules of the files
(choice, binding, renewal at a new record version, blocks, deferrals, exhaustion; a session whose entry no longer
allows its organization moves to another one at its renewal). Without `OPERATOR_SUBSCRIPTION_DIR` the pool holds them
alone; metrics, `doctor` and `operator_quota` show the token files only. Other
organizations see no difference; an empty pool changes nothing (`model_access_unavailable` or the next kind). An explicit preference that is unavailable → session `failed`
reason `model_access_unavailable` (no silent fallback); the task's chat offers its owner the connection, and the
hub continues the task by itself once the owner stored a model credential (ADR-079 §4). Model: owner's `model_id` if allowed for the access kind
in the catalog, else the catalog default (`platform`: `deepseek/deepseek-v4.1-flash-1017`; API key/subscription:
Claude Code default, no `--model`). Chosen kind and model are reported in `session_transition`. Exhausted
subscriptions are skipped and the crossing from the owner's own access to DFX access follows the owner's fallback
settings and the task's consent (§10.7, ADR-081).

### 10.2 Gateway (`/model/…` in `agent-guest-api`)

- Accepts only the session token (header `Authorization: Bearer` or `x-api-key`), maps to the session's
  provisioned credential; strips `authorization`, `x-api-key`, `host`, `content-length`, `connection`,
  `accept-encoding`; adds per kind: `anthropic_api_key` → `x-api-key: <key>`; `platform`/OpenRouter →
  `Authorization: Bearer <key>`; `claude_subscription` → `Authorization: Bearer <token>` and appends
  `oauth-2025-04-20` to `anthropic-beta`. Upstream base from the credential provisioning (§1.3 upstream URLs).
- Routes: `POST /model/v1/messages`, `POST /model/v1/messages/count_tokens`, `HEAD|GET /model/api/hello` (local
  200). Anything else → 404 + `guest.error gateway_route`.
- Operator subscription (ADR-052): the session is bound to its token file (`runner.operator_binding`), not to a value.
  The gateway runs in the guest and cannot read the file, so the runner provisions the file's current content again
  whenever the file's identity (device, inode, mtime, size) changes (daemon task `operator_subscriptions`, every 2 s,
  and the reconcile step `model`); a token replaced in the file never ends the session. The upstream status of the
  gateway's `usage` records feeds the pool: `401`/`403` for the content the session holds block the file until it
  changes and move the session to another usable file at its next renewal (none: it keeps its token; refusals within
  60 s after the gateway got a token are ignored, one session blocks at most one file per 10 min); `429` defers the
  file for 300 s for new sessions only (the record carries no `retry-after`); running sessions stay on it until an
  exhaustion is confirmed (§10.7). Blocks and confirmed exhaustions survive a restart (`runner.access_limit`).
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
The deltas of a session are merged over 10 s into one outbox `usage` item (`runner.usage_pending`, written in the
journal page's transaction; the daemon task `usage_merge` writes due items every second and at once when the
session has ended; counters added, the other fields the newest – the hub's totals are unchanged; ADR-086 §3,
`model/merge.py`). Only deltas with the same session, `model`, `access_origin` and UTC day of `at` merge (the hub
bills an item under its model and origin and prices and books it at its `at`, 12-billing.md §§2-4; a delta without
`at` has no day and merges): a delta with another model, origin or day writes the pending item first and starts a new
one. The merged item's `at` is the time of its newest request that has a valid time (at most 10 s after its first,
never on another UTC day). The runner does not know the price list versions (`billing.active` only), so a price list
whose `valid_from` falls inside a merge window prices at most 10 s of requests before it with the newer list. The
platform budget counts the pending cost (§10.2), every pending item besides the bounded stored ones.

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
  endpoint refuses access (`403 permission_error`: a setup token has only `user:inference`, KOR-71): quota polling is feature-detected (first 401/403 ⇒ `quota_unavailable` for that
  credential version, retried after 24 h or after a new login; the runner sends one outbox
  `quota {owner_id,unavailable:true,fetched_at}` and the hub drops the owner's windows, KOR-05) and the UI shows
  "not available"; the M0 LIVE check (`07-testing.md`) verifies the
  gateway path with the read-only current access token.
- Further subscriptions (KOR-69, ADR-090 §4; `quota/further.py`): towards a hub whose state carries
  `subscriptions.api`, the polling device lists the owner's `claude_subscription` records (`GET
  /v1/device/subscriptions`, at most every 900 s) and polls every non-primary one that holds a token with the rules
  above (900 s, `429`, 24 h after `401`/`403` unless a new login); items `quota {owner_id, subscription_id, windows |
  unavailable, fetched_at}` without `limited` (these subscriptions serve no session of the owner). A record under an
  older `CK` epoch than the newest one the device holds is first re-sealed under it (`PUT
  /v1/device/subscriptions/{id}` without a request), so a rotation by a client that does not know further
  subscriptions leaves none unreadable. A record without a token (not logged in) is skipped.
- Renewal of further subscriptions (KOR-71, ADR-092 §2; `quota/renewal.py`): the same polling device renews a full
  login (`kind: "oauth_login"` with `token` and `refresh_token`) `1800 s` before its `expires_at` and at once after a
  `401` of the usage endpoint (then it measures again at the next tick instead of reporting "not readable"): `POST
  CLAUDE_TOKEN_URL` (JSON `{grant_type: "refresh_token", refresh_token, client_id: CLAUDE_OAUTH_CLIENT_ID, scope}`,
  30 s, no redirects). The answer is sealed as the next record version and stored in `runner.subscription_renewal`
  (with the sealed record it replaces) **before** it is written with `PUT /v1/device/subscriptions/{id}` without a
  request; the row is written again on every tick (the token endpoint is not asked again for the record meanwhile)
  until the hub confirmed it, the record is gone (`404`) or a newer login replaced it (after a `version_conflict` the
  current record decides: still the replaced refresh token, or marked `relogin` → merged into it; else dropped).
  `invalid_grant` → the next version without `token` and `refresh_token`, with `relogin: "renewal_refused"` (only
  while the record still holds the refused refresh token); any other failure → again after 60 s. Rows of owners the
  device no longer measures are still written (the hub accepts the device that wrote the stored version). Setup-token
  records are never renewed. Neither request nor answer is logged.
- Platform key: `GET <openrouter>/api/v1/key` every 900 s → `spend_snapshot` → device metrics
  (`agent_runner_device_model_spend_*`), operators only.
- Tokens are never logged, never written to disk outside the encrypted credential record (and its sealed pending
  renewal); the only exception is the login's own plaintext store in `tmp/login-<id>` while a full login runs (§11.6),
  removed on every path and, after a crash, at the next start of the daemon.

### 10.7 Fallback, exhaustion and waits (ADR-081 sections 1–5)

- **Cost classes.** `own`: the owner's subscription or API key; `dfx`: an operator subscription or `platform`
  (OpenRouter, also the per-device key of §10.9). Inside a class the runner falls back by itself; from `own` to `dfx`
  only when the owner's `fallback.own_to_dfx` (default off) or the task's `model_consent` allows it. From the operator
  pool to `platform` only with `fallback.operator_to_platform` (default on). Both settings come with every session of
  the owner in `GET /v1/device/state` (`sessions[].fallback`, `sessions[].model_consent`); an older hub that sends
  neither means the defaults. An explicit `api_key` or `platform` preference has no fallback.
- **Exhaustion** (subscriptions only; access key `own:<owner_id>` or `operator:<file name>`). Trigger: a gateway
  `usage` record with status `429`. Readable quota: the runner reads the usage endpoint with the same token at once
  (at most once per access per 60 s); exhausted when `five_hour` or `seven_day` reports `used_percent >= 100` with
  `resets_at` in the future, until the latest such `resets_at` (`weekly:<model>` does not count). Not readable, or
  below 100 %: 3 `429` records within 300 s without a success in between mark it until `now + 900 s`; a single `429`
  marks nothing. The quota polls (§10.6, §10.8) mark and release with the same rule. Stored in
  `runner.access_limit` (access key, `until`, `readable`, `limited_at`; the `401`/`403` block of an operator file with
  the file's identity), read at start; a row whose `until` passed is released. A `402` of a `platform` session on an
  org device marks `dfxkey:<device_id>` for 120 s.
- **Order** (`model.access.resolve`): the preference's steps, exhausted accesses skipped; the operator pool picks the
  file with the lowest known `used_percent` of its fullest window first (unknown after known), then fewest bound
  sessions, unused longest, name. Deterministic: stored limits, latest snapshots, bindings and settings only.
- **Wait.** No allowed access left: `until` = the earliest release of the skipped accesses (an own credential that
  cannot be used right now: `now + 300 s`), `choice` = a `dfx` access would be available but the crossing rule bars it
  (never while the organization's credit is exhausted). Without any release time (only blocked files): `failed`
  `model_access_unavailable` as before. A new main session starts on the access with the earliest release and is
  suspended before its first prompt; a new subagent session fails with `model_quota_exhausted`.
- **Wait reports.** Every wait of a session – a new main session waiting in its start chain as well as a later change
  of a pause (a new release time, `dfx_choice` set or cleared, the end of the wait with `wait_until: null`) – is sent
  as the outbox item `session_wait {session_id, wait_until: time|null, dfx_choice, at}` (only to a hub with
  `model_fallback.api >= 1`); the hub keeps the newest report by `at`, only from the device the session runs on, and
  derives the task's `quota_wait` / `quota_choice` from it.
- **Running sessions** (reconcile step `model_fallback`, after `billing`, on every desired state and every 5 s; one
  run at a time per daemon – a run that starts while another is still working waits for it, so two runs never pause,
  move or resume the same session concurrently): an
  exhausted operator file is replaced in place by another file of the pool (gateway renewal); a change of the model
  kind pauses the session like a credit pause (`runner.model_pause`, interrupt, suspend under the deliverer's lock)
  and resumes it at once on the newly resolved access; with nothing allowed the session is `suspended` with
  `error_code` `model_quota_exhausted`, `wait_until` and `dfx_choice` (sent only to a hub with
  `model_fallback.api >= 1`). The pause ends by itself at `until`, at an earlier release, or when the setting or the
  consent allows a `dfx` access; the session then receives one fixed continuation sentence ("The platform switched
  this session's model access from <from> to <to> because <from> is used up until <time>. Continue the interrupted
  work." or "The model quota is available again. Continue the interrupted work."). A subagent session is moved in
  place but never paused (`model_quota_exhausted`). A running session is never moved back; a credit pause comes
  first. Every switch writes the outbox audit `session.model_switched` `{from_access, to_access, from_model, to_model,
  cause: exhausted|unusable|released, until}`; the access values are `subscription`, `operator` (an operator
  subscription), `api_key` and `platform`. The hub tells the task's owner with the notification `model.switched`
  (`ref` = `<from>:<to>:<until or empty>`).

### 10.8 Quotas of the operator's subscriptions (ADR-081 section 6)

- A platform device with `OPERATOR_SUBSCRIPTION_DIR` polls the usage endpoint with every usable file's token every
  900 s and at a confirmation (§10.7), parsed like §10.6. It sends the outbox item `operator_quota {at, subscriptions:
  [{name, state: usable|exhausted|blocked|deferred|ignored, readable, windows, limited_at, until, fetched_at}]}` – the
  complete snapshot of the device, at most every 60 s and only when something changed; file names only, never a
  token. The own subscription's `quota` item carries the latest observed limit `limited: {at, until, readable}`.
- Accepted: the file names (labels) of the operator's subscriptions are shown to the members of every organization
  granted the operator's subscriptions; the operator names the files accordingly (no personal data in a file name).
- The hub keeps the newest snapshot per device and serves `GET /v1/orgs/{org}/quotas` (03-api.md §4.3): the web shows
  it in the settings section "Model access and quotas" and as the counter of the situation view, the agent tool as
  `agent-platform quotas --org`.

### 10.9 DFX key on organization devices (ADR-081 section 7, idea 46 case 3)

- **Issuer**: a platform device whose runner has `AGENT_RUNNER_OPENROUTER_MANAGEMENT_KEY_FILE` (0600, platform
  devices only, never logged or sent) announces `dfx_key_state {issuer: true}`; the hub elects the online issuer with
  the smallest device ID and lists `dfx_keys.requests` (`{request_id, org_id, device_id, device_kem, headroom_usd}`:
  one per active org device of a non-exhausted organization without a key, only while billing is active; a request
  without headroom is not listed) and `dfx_keys.keys` in its device state.
- **Issuing**: per request the issuer deletes a key left over for the same request (by name), creates one key named
  `dfx-` + the whole request ID (the first characters of a UUIDv7 are its time part and would not tell requests
  apart) with `limit` = `min(headroom_usd, cap)`, seals it with HPKE to the device's `device_kem`, signs the envelope
  with its device signing key, forgets the key and sends `dfx_key_issued`. The issuer keeps at most one live key per
  organization and device: a request for a pair that already has a live key is not answered. It touches only keys
  whose name starts with `dfx-` (any other key of the provider account is never read, changed or deleted).
- **Orphan sweep**: keys named `dfx-…` that the hub lists neither as a request nor as a key (left by a crash, a
  failover or a key the hub forgot) are deleted by the issuer, so no key outlives its listing.
- **Org device**: accepts the envelope (`dfx_key_held` of its device state) only when the signer is a platform device
  certified in its verified platform log and the header names this device, its KEM key and an organization it serves;
  keeps it sealed in `runner.dfx_key` and opens it only to provision a `platform` session. Its usage items carry the
  origin `platform` and are not charged by the hub (the provider's figures are, below).
- **Measuring**: every 60 s the issuer reads each listed key's spend and sends the provider's cumulative figure
  `dfx_key_usage {key_ref, org_id, device_id, spent_micro_usd, at}` and sets `limit = spent + min(headroom_usd,
  AGENT_RUNNER_DFX_KEY_MAX_HEADROOM_USD (5))`; `disabled` switches the key off at the provider, `revoke` reads the
  final spend, reports it and deletes the key. The credit pause of ADR-062 §6 acts on an org device's `platform`
  sessions; sessions on own access there are never touched.
- **Accepted limit**: the hub learns a key's spend only from the issuer's poll, so a key may spend up to its limit
  between two polls (at most one poll interval of overspend per key, bounded by its headroom) before it is disabled
  or its limit is lowered; that spend is booked at the next report.
- Operator subscriptions stay on platform devices; no token of the pool reaches an org device.

## 11. Credentials, device requests, subscription login (ADR-011)

### 11.1 Device request handling

`request` hint → fetch, verify (§2.4), decrypt, process by kind; respond with encrypted, signed responses
(`seq` 1..n). Expiry 15 min; at most one active request of each kind per user per device.

### 11.2 `claude.login` – setup-token relay (default)

1. Create temp dir `$AGENT_RUNNER_HOME/tmp/login-<id>` (0700) with its own `CLAUDE_CONFIG_DIR`/`HOME`; existing
   profiles are never read or written.
2. Start `LOGIN_ARGV` in a PTY with an allow-list env and `BROWSER` set to a capture helper (pattern of
   `account_login._write_capture_helper`); wait ≤ 30 s for the authorize URL. The program runs on the host and is
   looked up in the runner's `PATH`: Claude Code is a host prerequisite of every runner device
   (`runbooks/runner-installation.md` §1), checked by `agent-runner doctor` (`login_cli`).
3. Response 1: `{step:"authorize", url}`. The user opens it in their own browser, authorizes, copies the code.
4. Follow-up request (new request ID, same kind `claude.login`, header `follows` = original request ID, `04-crypto.md` §4.5): `{code}` → typed
   into the PTY.
5. Read the token from the program's **screen** (below), validate format, build the `claude_subscription`
   credential record (CK), `PUT /v1/device/credentials/{owner}/claude_subscription`, response 2
   `{step:"done", email?}` (`email` only when the CLI reports it; optional); responses carry `final:true` on the last
   message; wipe the temp dir. Timeouts → `{step:"failed", code}`.

**Further subscriptions (KOR-69, ADR-090 §2).** The root body may add `subscription_id` (the further subscription to
fill; a new ID creates it), `name`, `note` and, for `automatic`, `email` (the address to sign in with instead of the
mailbox's). With `subscription_id` step 5 reads the current record (`GET /v1/device/subscriptions/{id}`), keeps its
`name`, `note` and `use` unless the request names them, and stores `{token, email, kind:"setup_token", …}` with `PUT
/v1/device/subscriptions/{id}` (`credentials/subscriptions.py`); the primary record is never written this way
(`upstream_error`). A malformed field ends `upstream_error` before the program starts. The runner announces the
feature `subscriptions`, so clients send these fields only to such a device.

**Screen, not raw text (ADR-051, KOR-21).** `claude setup-token` draws a terminal UI: it places words with cursor
positioning instead of spaces, erases lines and overwrites single cells. The runner therefore replays the PTY output
on a terminal screen of the PTY's window size (`login/screen.py`: printable and wide characters, wrap at the right
margin, CR, LF with scrolling, BS, HT, ESC 7/8, CSI `A B C D E F G H f d J K`; every other sequence without effect)
and reads only its rows. The labels of the program are constants in one place (`login/setup_token.py`), checked against the
golden's Claude Code pin; a pin change re-checks them (`runbooks/claude-code-upgrade-gate.md`).

- **Wait for the input field.** The program shows "Paste code here if prompted" a few seconds after the URL and drops
  what is typed before. Step 4 types the code only once that label is on the screen (and 0.3 s later, the program may
  enable its input just after drawing it); not within `prompt_wait` (30 s, counted from the code) → `timeout`, the
  program ended or showed an error before → `login_cli_failed`.
- **Errors while the program runs.** A wrong code leaves the program open ("✘ OAuth error: … Press Enter to retry").
  As soon as "OAuth error" is on the screen the login ends with `code_invalid`; otherwise the program must end within
  `token_wait` (60 s; on success it ends by itself 500 ms after showing the token) → else `timeout`. An exit status
  ≠ 0 is `code_invalid` with the error on the screen, otherwise `login_cli_failed`.
- **Token.** From the screen after the program ended: the rows after the row with "OAuth token", from the first
  non-empty row to the next empty row, stripped and joined (a narrow window wraps the token), must be a whole token
  (`sk-ant-oatNN-…`). Without that row (a later version may word it differently): exactly one run of non-empty rows
  on the whole screen that is, joined, a token and nothing else. In both ways that run must be the only run of rows
  that looks like a token or a piece of one (token characters only, with the prefix or ≥ 16 long; a token split by an
  empty row leaves a piece on its own run). Anything else →
  `token_invalid`.
- **Diagnosis without secrets.** A failure after the start logs only the form of the screen
  (`runner.login screen rows=<n> marker=yes|no token_blocks=<n> prompt=yes|no error=yes|no`), never its text, the URL,
  the code or the token.

### 11.3 `claude.login` – mailbox automation (`LOGIN_AUTOMATION=on`; default on platform devices, off on org devices)

Requires the owner's `mailbox` credential. Same PTY start; instead of step 3 the runner opens the authorize URL in
headless Chromium (Playwright for Python), enters the email, polls IMAP with the logic of
`agent_cli.account_login.newest_magic_link` (INBOX + junk, sender filter, ≤ 1 MiB, newest first, poll 5 s,
≤ 10 min), opens the magic link, clicks authorize, reads the code and types it. Any unexpected page state →
fall back to §11.2 step 3 with `{step:"authorize", url, automation:"failed"}` (the web shows `login_automation_failed`). Hermetic tests only (login-CLI
double + auth-page double + mail double); live tests of logins are forbidden.

### 11.4 GitHub connect (ADR-015)

Which device flow (KOR-65, ADR-077): `{}` runs the OAuth App's (`GITHUB_OAUTH_CLIENT_ID`, form field `scope=repo workflow`)
when it is set up, else the GitHub App's (`GITHUB_CLIENT_ID`); `{app:"oauth_app"|"github_app"}` names one; one that is
not set up ends `{step:"failed", code:"flow_unavailable"}`. Both tokens are remembered as minted and revoked on
disconnect (KOR-40). The live channel's `Agent-Features` header names `github.ways` and, when set up,
`github.device_flow` (a client ID) and `github.operator` (the operator's token file), which the hub lists in
`GET /v1/me/devices` for the web.

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

**Operator's GitHub account (KOR-27, ADR-053).** The operator's own GitHub connection, for exactly one user: the
operator. `OPERATOR_GITHUB_USERS` names one user; more than one is `config_invalid`
`OPERATOR_GITHUB_USERS:one_user_only` (the daemon does not start, doctor names it; HOT-14). For that user (the user
of the verified request header) the runner skips the device flow: it reads `OPERATOR_GITHUB_TOKEN_FILE` anew (regular file,
not a symlink, owned by the runner's account, 0600 or stricter, one line with the token, in a directory of the
runner's account that nobody else may write; anything else is not read,
`credential_missing`, the reason logged once), checks the token with `GET <GITHUB_API_URL>/user` (scopes from
`X-OAuth-Scopes` as far as GitHub names them), stores the credential as above and sends the only response
`{step:"done", login}` (final). A refused token ends `{step:"failed", code:"upstream_error"}`. Log lines carry
`source=operator`; doctor `operator_github` and the counters `agent_runner_operator_github_connects_total` /
`_connect_failures_total` (08 §3) watch the file and the connects. Every other user keeps the device flow. The stored credential is
sealed under the user's `CK` like every `github` credential, so that user can open it, and operations use it only
for that user's own instructions (§12 step 3, ADR-053).

**Existing token (KOR-25, ADR-065).** A second, equal way: `github.connect` with `{token}` (an existing
GitHub token, e.g. that of a GitHub CLI login or a personal access token, HPKE-encrypted to the device like every
request). No `GITHUB_CLIENT_ID` is needed. The device checks it with `GET <GITHUB_API_URL>/user`: login and account
ID from the body, scopes from `X-OAuth-Scopes` (a missing header means a fine-grained token: `scopes: []`); a classic
token with neither `repo` nor `public_repo` → `failed scope_missing`; `401` → `failed token_refused`; network errors
→ `upstream_unreachable` (retried as above); an organisation that requires SSO authorisation → `sso_required`. On
success the device stores the credential `github {token, login, user_id, scopes, source:"token"}` (the device flow
stores `source:"device_flow"`; records without `source` stay valid) with the repetition above and answers
`{step:"done", login, scopes, source}`. The token is never logged or sent back. Such a token reaches as far as the
account; the boundary stays the workgroup's allow-list, checked formally and audited for every operation.

**Several accounts, company accounts, signing keys (KOR-47, ADR-078).** `github.connect` may name the record to fill
(`identity_id`: reconnect a listed account of the requesting user); without it the device updates the primary record
when it holds the same GitHub account (`user_id`), creates the primary record when there is none and otherwise adds a
record (`PUT /v1/device/github-identities/{id}`). With `org_id` (only accepted by the hub from an admin of that
organization; the device checks the admin role in the verified log again) the flow runs as for a user, but the device
stores nothing: the final answer `{step:"done", …, body}` – end-to-end encrypted to the requesting admin – carries the
credential body, which the admin's client seals under the org key, signs and stores. On a platform device with
`OPERATOR_GITHUB_ORG` (requires `OPERATOR_GITHUB_TOKEN_FILE`; platform devices only, `config_invalid` otherwise) an
admin's `github.connect` for that organization takes the operator's token (`source:"operator"`) instead of the device
flow; copies for the users of `OPERATOR_GITHUB_USERS` are stored with `source:"operator"` too, and every change of that
list is audited once (`runner.operator_github_users {count, added, removed}`). `signing_key:true` creates an Ed25519
key after the account is checked, registers it with `POST /user/ssh_signing_keys` and puts it into the body
(`signing:"registered"`); GitHub's `403`/`404` (scope `write:ssh_signing_key` missing) is `signing:"scope_missing"`,
another refusal `signing:"refused"`; the connection is stored either way.

### 11.5 Pull-request state (`github.pull_status`, ADR-084 §2, KOR-48)

A fresh read of pull requests for the pull-request view; repositories, numbers and states stay end-to-end encrypted
between the device and the requester. Body `{org_id, wg_id, task_id?, pulls:[{repo, number}]}` (1–50, no pull request
twice; any other form → `failed upstream_error`). The device answers only for a workgroup it is assigned to and a
requester who can read that workgroup in the verified organization log, and a task it knows must belong to that
workgroup and organization – otherwise `failed forbidden`. A requester who can write the workgroup reads with the
identity the ADR-078 resolution picks for an operation of that task (the task's choice when the device knows the task,
the workgroup's default, the organization's default, the requester's own primary record, the operator's account; each
usable by the requester, §12 step 3); a reader only with the own primary record. Records open only through
`credentials.acting`. Nothing usable (or GitHub's `401`) → `failed github_not_connected`; the hub unreachable while a
record is needed → `failed upstream_unreachable`. Per pull request, in the order asked: outside the workgroup's
repository list (when set) → `{repo, number, found:false, code:"repo_not_allowed"}` without a request; else
`GET /repos/{r}/pulls/{n}` (read again after 1 s and 2 s while an open pull request's `mergeable` is `null`, then
`null` is passed on) → `{repo, number, found:true, title, state, draft, merged, mergeable, mergeable_state, head_ref,
head_sha, head_repo, base_ref, url, updated_at}`; `404` → `not_found`, another `403` → `repo_not_accessible`, anything
else → `upstream_error` for that pull request. One final response `{step:"done", pulls}`. Log lines never name a
repository or a number. The GitHub double computes `mergeable` asynchronously like GitHub (first read after head or
base moved `null`) and offers `POST /_double/commits` to move a base in tests.

### 11.6 `claude.login` of a further subscription – full login (KOR-71, ADR-092 §1)

With `subscription_id` the login runs `SUBSCRIPTION_LOGIN_ARGV` (default `claude auth login --claudeai`) instead of
`LOGIN_ARGV`, with `--email <address>` appended when an address is known (the automatic login's, else the one the
record holds); the primary record keeps the setup-token relay of §11.2. Same temporary directory, allow-list
environment, `BROWSER` capture helper (so the device never opens a browser), PTY and cleanup as §11.2, and:

- **Link.** The URL handed to `BROWSER` redirects to `http://localhost:<port>/callback`; the response
  `{step:"authorize", url}` carries the link of the code page from the **screen** (`login/full_login.py`): the
  non-empty rows after "If the browser didn't open, visit:" up to the input field "Paste code here if prompted",
  joined and checked like every authorize URL (else `login_cli_failed`). The automation of §11.3 opens the same link.
- **No keychain.** `PATH` begins with a private directory of the login holding `security`, a script that always
  exits 1; the program reaches the keychain only through it and keeps the login in its plaintext store inside the
  temporary directory.
- **Login.** After the code the program must end with status 0 within `token_wait`. The runner reads
  `$CLAUDE_CONFIG_DIR/.credentials.json` (`claudeAiOauth`; regular file of the runner account, not a link, ≤ 64 KiB)
  and the address of `.claude.json` (`oauthAccount.emailAddress`, optional). Missing or malformed → `token_invalid`;
  `scopes` without `user:profile` and `user:inference` → `scope_missing`. The record gets `{token, refresh_token,
  expires_at, scopes, subscription_type?, rate_limit_tier?, email, kind:"oauth_login"}` plus the kept `name`, `note`,
  `use` (no `relogin`). The tokens never reach a log, a response or a guest (sessions get `token` only, §10.1).

## 12. Operations (ADR-012)

Pipeline for every `request_operation`:
1. Guest api: tool arguments that break `mcp/tools/request_operation.input.json` or that the runner could not store
   (a NUL character or lone surrogate in a string, nesting deeper than 64 levels) are refused with `params_invalid`
   (journaled `platform.refused`; no `operation_id`, nothing reaches the hub, KOR-09). An unknown `type` or `params`
   that break `ops/<type>.params.json` → still forwarded with `local_check:"rejected"` (so it is recorded), then
   `rejected` returned.
2. Control journals `operation.request`; runner receives it, assigns state `received`.
3. Runner formal checks (`03-api.md` §9) on plaintext: settings from the latest verified `settings.workgroup`
   event (no event: the defaults, no repository list; a stored event that no longer opens: no repository allowed;
   ADR-054), repository list when set, GitHub credential of the acting user present, sizes, secret patterns. For `github.commit` the runner takes **one** workspace export
   snapshot into the scratch directory `tmp/op-<id>` here, before registration; scan, local tree SHA and the later
   commit all use exactly this snapshot (`03-api.md` §9).
   **Acting user (HOT-14, ADR-070).** A personal connection must never be used on behalf of another person. Several
   people may write in one task (shared sessions), so an operation that needs a personal connection (today every
   `github.*` type) uses the connection of the **acting user** - the verified author of the prompt that started the
   session's current turn - and never the task owner's as such. The runner keeps the author of every verified prompt
   (`prompt_event.author_id`); the guest's `session.state prompt_accepted` of a main session sets the turn's acting
   user, `idle` closes the turn; a second prompt of another (or an unknown) author in the same open turn leaves the
   turn without acting user. `operation.request` is stamped with the acting user at receipt (`operation.actor_id`; a
   held operation keeps it); a child session takes the acting user of the `subagent.request` that started it. One
   module decides and opens the credential (`credentials/acting.py`); a structural test keeps it the only one. An
   operation needing a personal connection without acting user is rejected at once with `github_not_connected` and
   uses nobody's connection; it is not held (nobody could be asked). The `operation.result` event names the user
   whose connection an executed operation used (`connection_of`). Users and their agents author prompts; a prompt
   of an agent counts as its user's (ADR-082). Credential kinds of a session (inventory): `github` is the only personal connection an
   operation uses; the model access is the session's, chosen at its start, its consumption carried by the
   organisation (KONZEPT A15); `mailbox` and the login kinds serve only that user's own device requests.
   **Held instead of rejected (KOR-25, ADR-066).** If the check of a `github.*` type fails with `github_not_connected` (no
   usable connection of the acting user), the operation is not rejected but held locally before registration: state stays `received`,
   marked held with the reason and `until` = receipt + 15 min. The runner writes one `session.status`
   `{state:"waiting", waiting_for:"github_connection", operation_id, operation_type, repo?, for_user, until}`
   (`03-api.md` §10; `for_user` = the acting user), which the web shows to that person as a card in the task's chat
   (everybody else, the task owner too, sees whom it waits for). It checks again at every
   scan (5 s) and as soon as a new `github` credential arrives; when the check
   passes the operation goes on normally (steps 4–7; the MCP call answered `pending` after 20 s, the result comes with
   the ping `operation.finished`). At `until` it is rejected with the original code and the message "…not connected
   within 15 minutes" (audited like every rejection). Flood limits: at most 3 held operations per session at a time
   (more are rejected at once as before) and at most one `waiting` event per session and reason per
   60 s. The registration expiry (10 min in `received`) counts for a held operation from the end of the hold.
   A held operation whose session ended meanwhile (`stopping`, `stopped`, `failed`; closing a task stops its sessions)
   is rejected with `not_found` without running its checks.
   **Held for access (KOR-65, ADR-077).** A repository-level pre-check that GitHub refuses for access (`404`; `403`
   for an organisation's OAuth App restriction, its ban of classic tokens, or SAML single sign-on) fails with
   `repo_not_accessible` and the one next step (`github.reach`), and is held the same way with the reason
   `github_access`; the `waiting` event carries `next {action, owner, url?}`. While the acting user's credential is
   unchanged the access is checked again at most every 30 s (an installation, an approval or an authorisation at
   GitHub reaches the runner only through a new check); a new credential is checked at once; a changed reason or step
   is announced again. At `until` it is rejected with the timeout text followed by the step. A credential GitHub refused is remembered by its record
   version (`held_credential`): the pre-check asks GitHub again only when a new version arrives. A fourth held
   operation of a session is rejected at once with a fixed text saying that the person is already being asked.
   `github.request_connection` has no other check: its pre-check asks `GET /user` once; held while the acting user is not
   connected or the token is refused, else answered with `{login}` from the credential. The session's context and the harness state the owner's GitHub connection
   (connected as `<login>` / not connected) and the rule: if the task needs GitHub, request the operation even
   without a connection; the platform obtains the connection from the user; do not explain what is missing and do
   not offer workarounds.
   **GitHub identity (KOR-47, ADR-078 §§2–4).** Every `github.*` check first resolves the identity the operation acts
   with (`03-api.md` §9): operation parameter → session → task → workgroup default → organization default → the acting
   human's own primary record → the operator's account (`OPERATOR_GITHUB_ORG`), among the identities that human may use
   (own records; company records of the task's organization; the operator's account for members of its organization,
   checked against the verified membership log). The acting human is the acting user stamped on the operation
   (ADR-070; none: rejected with `github_not_connected`). Choices and defaults come from the desired
   state and are kept in the store (`github_selection`, `github_default`), so a restart without the hub never falls back
   to another account; identity records are fetched by ID (`GET /v1/device/github-identities/{id}`), verified like
   every credential record (a company record only with an active admin of its organization as author) and opened with
   the user's `CK` or the org key of the record's epoch. An explicit choice the human may not use is rejected with
   `identity_forbidden`; one whose record is missing or does not open is held like a missing connection. The
   executors open exactly the resolved record. The runner announces the feature `github.identities` on its live
   channel.
4. Dedupe tag (`04-crypto.md` §4.10) with the current WK epoch.
5. Encrypted `operation.params` event; `POST /v1/device/operations` (with `triggered_by` = the acting human and, for
   `github.*` types, `github_identity` = the resolved identity; KOR-47) (retry with backoff while the hub is
   unreachable; 10 min in `received` → `expired`, `hub_unreachable`).
6. Decision: `accepted` → execute (§12.1) with transitions `executing` → `succeeded|failed`; `duplicate` → load
   the original's `operation.result` (if the original is not final yet: wait within the MCP wait time, else return
   `pending` with `duplicate_of`), return with `status:"duplicate"`; `denied`/`rejected` → return.
7. Result: outbox `event` (`operation.result`) then `operation_transition`, then `POST /v1/pending/{id}/answer`.
   If the MCP call already returned `pending`, a ping `operation.finished` is sent.

### 12.1 Executors (host side, REST only, no `git` binary, scratch under `tmp/op-<id>`)

| Type | Steps |
|---|---|
| `github.list_repositories` | `GET /user/installations` → `GET /user/installations/{id}/repositories` (paginated); a token without the GitHub App (`403` on `/user/installations`, ADR-053) lists `GET /user/repos` (paginated, same limits) instead - the same for a connection with an existing token (§11.4); mark `allowed` from settings (every entry when the workgroup sets no list, ADR-054) |
| `github.checkout` | resolve ref (`GET /repos/{r}/commits/{ref}`) → `GET /repos/{r}/tarball/{sha}` streamed to scratch → safe extraction (regular files/dirs only; reject absolute, `..`, links, devices; strip the top directory; size limits) → re-tar → `files/import?dir=repos/<owner>/<name>&replace=true` (`<owner>/<name>` in lower case: GitHub names, the allow-list and the dedupe are case-insensitive; the result's `dir` names the path, KOR-10); remember base sha per (session, repo) |
| `github.get_pull_request` | `GET /repos/{r}/pulls/{n}` (an open pull request whose `mergeable` is `null` – GitHub computes it asynchronously – is read again after 1 s and 2 s, then `null` is passed on with `mergeable_state`; KOR-48), `/files` (paginated), `/issues/{n}/comments` |
| `github.create_branch` | `GET /repos/{r}/git/ref/heads/{from}` (or commit sha) → `POST /repos/{r}/git/refs` |
| `github.commit` | uses the snapshot taken in step 3 (no second export) → parent = head of `branch` at GitHub at check time, or the checkout commit if the branch does not exist → compare with the parent tree (`GET /repos/{r}/git/trees/{parent_tree}?recursive=1`; blob and tree SHA-1 computed locally in git object format) → dedupe fields include the local tree SHA → after acceptance `POST /git/blobs` for changed files → `POST /git/trees` with `base_tree` (+ deletions) → verify returned tree SHA = local (`tree_mismatch`) → `POST /git/commits` (parent as determined) |
| `github.push` | check fast-forward (`GET /repos/{r}/compare/{head}...{sha}` status `ahead`/`identical`) → `PATCH /repos/{r}/git/refs/heads/{branch}` `force:false` or `POST /git/refs` if missing; the push of a merge commit marks its preparation `pushed` (KOR-48) |
| `github.merge_base` (KOR-48, ADR-084 §4) | pre-check: `GET /repos/{r}/pulls/{n}` (open, head in the same repository) → base tip `GET /git/ref/heads/{base}` → merge base `GET /compare/{tip}...{head}` (`up_to_date` when equal) → workspace protection (one export, compared with the head's tree and the stored digest; `workspace_dirty`) · executor: the trees of merge base, head and tip (`GET /git/commits`, `GET /git/trees?recursive=1`) → `merge3.merge_entries` → contents from the head's, then the base's tarball, each member checked against its blob SHA (`GET /git/blobs/{sha}` for the rest, ≤ 1000) → line merges (diff3 style) → one plain tar → `files/import?…&replace=true` → merged blobs of protected names `POST /git/blobs` → `runner.merge_prep` (migration `0027_merge_prep`) and `runner.workspace_state`; both pruned after 7 days by `retention` |
| `github.create_pull_request` | `GET /repos/{r}/pulls?head=<owner>:<head>&base=<base>&state=open` (existing ⇒ operation `succeeded` with that PR and `result.existing:true`; MCP status `duplicate`) → else `POST /repos/{r}/pulls` |
| `github.comment` | `POST /repos/{r}/issues/{n}/comments` (issues and pull requests alike) |
| `github.list_issues` | `GET /repos/{r}/issues?state&labels&sort&direction&per_page&page` (one page; entries with `pull_request` are left out; `next_page` from the `Link` header) |
| `github.get_issue` | `GET /repos/{r}/issues/{n}` → `GET /repos/{r}/issues/{n}/comments?per_page&page` (one page; `comments_total` from the issue's `comments`); cut to 200 KiB (newest comments of the page first, then the body; `truncated`) |
| `github.create_issue` | `POST /repos/{r}/issues` `{title, body?, labels?}` |
| `github.update_issue` | `PATCH /repos/{r}/issues/{n}` with the given fields only (`state_reason` with `state`; `labels` replaces all) |
| `github.list_pull_requests` | `GET /repos/{r}/pulls?state&base&sort&direction&per_page&page` (one page; `next_page` from the `Link` header) |
| `github.request_connection` | no GitHub call: `{login}` of the acting user's credential |
| `subagent.request` | hub returns `child_session_id`; runner starts the child (§13) |
| `task.set_title` | device-authored `task.meta` event |

Rate limits: honour `Retry-After`, `x-ratelimit-remaining = 0` → wait until reset (≤ 60 s else retryable failure).
Error mapping of every GitHub type: `401` → `github_not_connected` (token revoked or expired; in a formal check that
reaches GitHub this holds the operation for a new connection, §12 step 3); `404` → `not_found`; `410` (issues disabled in the repository) → `not_found` with
"Issues are disabled in this repository."; `403` with `x-github-sso: required…` → `upstream_error` with a message
that an organisation requires SSO authorisation of the token (the URL of the header is never kept); other `403` →
`upstream_error`. Every executor works with both kinds of connection (device flow and existing token, §11.4).
Every GitHub request carries `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, timeout
30 s, no redirects except for tarball downloads (to the codeload host of the configured web URL).

## 13. Subagents (ADR-088, ADR-038)

- Created only after hub acceptance of `subagent.request` (the limits depth and total are settings of the hub,
  ADR-088 §2, enforced in check H4; the runner pre-checks them with the values of the device state's `limits` and the
  measured room of the parent's VM, `vm.room`). How many children run at once is no fixed number (ADR-075 addendum
  2): the free resources decide; an older hub's `subagent_max_running` is read past.
- Same VM, new sandbox, `depth = parent.depth + 1`, `parent_session_id` set, same task, same model access as the
  parent unless `model_id` given and allowed. Initial prompt = `"Purpose: <purpose>\n\n<prompt>"` (prompt ID = the
  operation ID: the child's first instruction); harness adds "You are a subagent session of session <parent> for task
  <task>; answer each instruction of your main agent with your last message of that turn; you keep running and
  receive further instructions until your main agent stops you; you may request further subagents only via
  `subagent.request`".
- **Turns.** A child keeps running after a turn: when a turn ends (`idle`), the last assistant message (≤ 64 KiB)
  is the result of that instruction and the parent gets the ping `subagent.finished` (per turn, ping ID stable per
  child and instruction, `ref` = the child; only for a parent that has not ended). A turn that ends with
  `StopFailure` fails the child `agent_exited`. An idle child is suspended after `AGENT_RUNNER_SESSION_IDLE_SECONDS`
  like a main session (never while an instruction waits) and resumed with its conversation by the next instruction.
- **Instructions.** `subagent.instruct {session_id, prompt}` gives a child of the requesting session its next
  instruction (runner pre-check: a child of this session in the same task that has not ended, else `not_found`; a
  suspended child is resumed, so the VM's measured room is checked, `limit_exceeded`).
  Instructions of one child (`runner.subagent_instruction`, the request first) are handed over one at a time in the
  order of their creation, one per turn: the next waits until the child accepted the previous one and that turn
  ended. The executor resumes a suspended child, registers the prompt at the guest with the child's sender line
  (prompt ID = operation ID) and succeeds with `{session_id, instruction_id}`. The acting user of a child's turn is
  the acting user of the instruction it accepted (`credentials.acting.stamp`; before its first acceptance the one of
  the `subagent.request`): it decides the child's reads, its operations' `triggered_by` and its personal connections.
- **End.** `subagent.stop {session_id}` (same pre-check) stops the child; it also ends when its parent ended
  (stopped by the runner), when the task is closed, and when `subagent_max_runtime_seconds` (> 0) is reached since its
  creation (`failed` `runtime_exceeded`). The end pings the parent with the ID of the newest instruction.
- **Result.** `get_subagent_result {session_id, wait_seconds?}` answers the newest instruction: `working` (or a start
  state) with `instruction_id` while it waits or its turn runs; `idle`/`suspended` with `final_text`, `finished_at`
  and `instruction_id` once that turn ended; `stopped` with the last turn's `final_text`; `failed`. `wait_seconds`
  (≤ 30) waits for the end of that turn or of the child. The text is read from the outbox (ADR-038).
- **Older guests.** A child whose VM runs a guest before ADR-088 (golden guest version < 0.3.0) keeps the one-shot
  end of ADR-038 (stopped after its first turn, one ping per child), its former harness line and the former answers of
  `get_subagent_result`.
- Full session in every respect: own events, terminal, hooks, audit, metrics; visible in the web as a child (at any
  depth).

### 13.1 Rights profiles (KOR-51, ADR-094 §3)

A session may carry a rights profile `{name, operations, reads}`; without one it may do what its human may (A1,
E-11). The runner keeps the section `agent_profiles` of the desired state (`profile_state`, reconcile step 46
`agent_profiles`; a desired state without it clears it) and, for a child the hub accepted with a profile of its own,
the answered `child_profile` (`session_profile`) until the next state lists the child; a child the state does not
list yet has its parent's profile. It enforces the effective profile three times:

- **Operations:** right after the formal checks of every request (`profiles.check.operation`): a type outside the
  profile is rejected locally with `rights_denied` and a sentence that names the profile and who can change it (a
  person for a main session, the parent for a child). `subagent.request` with `profile` needs a name known in the
  workgroup that allows nothing beyond the session's own profile; the name is registered as `child_profile`.
- **Work VM and browser tools** (`workvm.call`): a tool whose type (`workvm.request`, `browser.open`) the profile does
  not allow is answered with the tool error `forbidden` before anything starts.
- **Reads** (`platform.query`): reach `workgroup` reads no other workgroup and not the organization's member list,
  reach `task` in addition no other task, no task list and no workgroup or organization whiteboard (`forbidden`).

Every session start carries the profile in `POST /v1/sessions` (`profile`, guests 0.4.0 and later: the guest does
not offer the excluded tools and records an excluded operation as locally rejected) and one harness line naming the
profile. Merges (`github.merge`), verdicts (`review.verdict`) and the workgroup's merge criteria and separation of
duties are §12.1 and 03-api.md §9.

**Process analysis** (KOR-53, [ADR-097](../adr/097-process-analysis.md)): the read tools `get_usage`, `list_audit`,
`list_workgroup_operations` and `get_metrics` (03-api.md §8.1) are answered like `list_tasks`: the arguments are
checked again, the workgroup (`workgroup_id`, default the session's) must be readable by the read scope (owner ∩
instructing human, device assigned, within the profile's reach), then the runner asks the hub's device route of that
workgroup (03-api.md §6.1) and keeps only rows of the session's organization and that workgroup in the contract's
form; an audit row's `detail` keeps only flat tokens and an untokenised `target_id` becomes null. Every call is a
`platform.read` event and a `read.session.<tool>` audit row with `read_wg_id` and `triggered_by`. The runner announces
`platform.analysis` (`Agent-Features`), which the hub requires for analysis tasks. A session with the profile
`analysis` is restricted like any other profile; its main session's harness names the analysis brief.

## 14. Metrics

Sampling every 15 s by the `metrics` task (sources and sinks in `08-monitoring.md`): host values, `limactl list
--json`, VZ process values, guest `/v1/metrics`. Outbox `metrics` items and, if configured, the textfile.

## 15. Claude Code updates

Version pinned in `images/versions.json` (`claude_code`, tarball sha256). An update is a PR that changes the pin
and MUST pass: model-double E2E (`07-testing.md`), the attack suite ATK (scripted adversary, hook fault mode,
poisoned settings, OS probe) on a golden built with the new version, and a check that no new routes/headers are
unknown to the model double (double fails on unknown routes). Then `agent-runner image build`; running VMs move
per §3.5.

## 16. Own device: installation without a terminal

Decision: ADR-074 (KOR-59).

An own Mac becomes an org device through one signed, versioned package (`agent_runner/install/`):

| Part | Module | What it does |
|---|---|---|
| Build | `install/build.py`, `scripts/macos-package/` | From one commit and the lock (`lock.json`, `requirements.lock`, every input pinned by SHA-256): relocatable CPython 3.12 with the runner's dependencies (`pip --require-hashes`) and `agent_runner`/`agent_cli`, Lima (without its agents for macOS guests), PostgreSQL, the golden's sources, the launchers; modes and times normalised, byte code with unchecked hashes, each file compiled by a fresh interpreter that reads no byte code (so it does not depend on what a compiling process compiled before); optional code signing (hardened runtime, entitlements) **before** the manifest, refused with `build_failed` `sign_missing` (each `sign_missing path=…` on standard error) when a Mach-O file lies inside an archive; `release.json` + signature; `pkgbuild`; `latest.json` + signature with an expiry (≤ 120 days). Two unsigned builds of one commit give the same `release.json` (a signed build is compared on its unsigned stage). |
| Release | `install/release.py` | Canonical JSON, Ed25519 signatures of the release key; only keys of `install/release_keys.py` (public data) are accepted; version `YYYY.MM.DD.HHMM` of the commit time. |
| Checks | `install/preflight.py` | macOS ≥ 14, Apple silicon, `kern.hv_support`, memory ≥ 16 GiB, free space ≥ 50 GiB; memory reserve of an own device: a quarter of the memory (4–32 GiB). |
| Owner | `install/owner.py` | The runner runs as the Mac's owner (UID ≥ 501): the agent way names the owner of the request file, Installer.app the user at the screen, an update keeps the recorded one. No account is created (macOS 27 refuses to delete one again, ADR-074 §3). |
| Root helper | `install/root.py` | `postinstall` (check the payload against its manifest, `current`, keep one previous version, Claude Code for the host, maintenance daemon, runner home created by the owner, enrollment from the spool with the code on `enroll --code-file -`'s standard input, runner daemon, `state.json`), `uninstall` (runner's `uninstall` as the owner, its home and the parents the installation created, plists, receipt, payload; prints `leftovers`), `leftovers`, `maintain`. |
| Daemons | `install/daemons.py` | `swiss.dfx.agent-runner` (the owner, settings below) and `swiss.dfx.agent-runner.maintenance` (root, every 15 min), both through `libexec/dfx-agent-runner` (`python -I -B -m …`). |
| Updates | `install/update.py` | Feed read at most hourly, verified with the installed version's keys; installed when no session works, inside 02:00–05:00 or after 7 days of waiting; store kept as an APFS clone; kept only when the runner reports a new hub connection within 10 min, else the way back; two failures mark a version bad. |
| Agent way | `install/admin.py`, `agent_platform/ownmac.py` | One `osascript … with administrator privileges` with a constant root script (package copied, digest checked on the copy, request and owner moved into the spool, `installer`). |

Settings of an own device's runner (plist `EnvironmentVariables`): `HOME=~/.local/share/dfx-agent-runner`, the
manifest's `HUB_URL` and `PLATFORM_ROOT_KEY`, `POOL_SIZE=0`, `MEMORY_RESERVE_MIB` as above, `GOLDEN_TEMPLATE` of the
payload, `LOGIN_AUTOMATION=off`, `VM_PREFIX=dfx-` (`TEST_MODE=1` only for a test build whose hub is loopback HTTP).

A verified `remove` of the device in every org whose log it holds is persisted as `device_removed` (§2.2 step 4,
`assignments/teardown.py`; `status` prints `removed`); the maintenance daemon then removes the installation by itself.
A `key_revoked` refusal of the hub alone never does.

Paths: payload `/Library/Application Support/DFXAgentRunner/` (`versions/<v>/`, `current`, `host/`, `spool/` 0700,
`downloads/` 0700, `log/`, `state.json` 0644, `maintenance.json` 0600), plists in `/Library/LaunchDaemons/`, receipt
`swiss.dfx.agent-runner`. Release operation: `runbooks/own-device-release.md`.


## 17. The main orchestrator as a platform session

Decision: ADR-082 §6 and §9.6–§9.9 (KOR-57). The user's main orchestrator (its agent with its own identity) may run
inside a running **main** session of a task in the user's **private** organization on a **platform** device. The
hub owns the request, the binding of the session to its device and task owner and the revocation at the session's
end (`03-api.md` §4 and §6); this section is the runner's, the guest's and the agent tool's part.

### 17.1 Flow

1. The hub hints the runner (`session.orchestrator`, content-free); the hint wakes the reconcile run (§2.2), and
   `GET /v1/device/state` lists the work as `orchestrators:[{session_id, step:"keys"|"handover"|"revoked",
   pairing_id}]` (`keys` only while the session has no live pairing at the hub: every such item gets fresh keys from
   the guest; `revoked` with the pairing the hub revoked while the session goes on: the runner ends its row, so the
   relay stops, and asks the guest to end the orchestrator, `POST /v1/sessions/{id}/orchestrator/end`).
   The reconcile step `orchestrator` takes each session that runs here, is a main session (`depth` 0) that has not
   ended, whose organization is private in the verified log and whose device is a platform device; anything else is
   skipped with a log line.
2. Keys: the runner asks the guest (`POST /v1/sessions/{id}/orchestrator/keys`, §17.3) with the hub's origin, its
   pinned response key and the platform root key. The guest runs `agent-platform pair --session` under the system
   account `agent-orch` (§17.4); the tool draws the key pair in its state directory and returns
   `{pairing_id, pub:{kem,sig}, pop, fp}`. The private keys never leave the VM. The hub key goes in with the ID
   `pinned` (the device pins the key itself, `device.json`); the session's VM must run. The runner checks the answer
   (exactly these fields, `fp` the fingerprint of `pub`, `pop` the proof of possession for `pairing_id`) before it
   signs anything (`keys_invalid` otherwise). A session whose pairing was handed over is not asked again.
3. Registration: `POST /v1/device/orchestrator-pairings`
   `{session_id, pairing_id, pub, pop, fp, device_sig}` (signed by the device) with
   `device_sig = b64u(Ed25519_device(P‖"orchestrator-key\0"‖canonical({"fp","pairing_id","session"})))`, `P` =
   `agent.dfx.swiss/v1/`. The hub answers `{pairing_id, expires_at}`. The runner stores the pairing
   (`runner.orchestrator`, `registered`).
4. The user approves in the browser (the hub's and the web's part) and the browser stores the handover; the work list
   then names the session with `step` `handover`. The runner fetches `GET /v1/device/sessions/{id}/orchestrator` →
   `{session_id, user_id, requested_at, requested, pairing:{pairing_id, state, expires_at, handover:{ct, sig}|null}}`.
   It accepts the handover only when `session_id` and `pairing_id` are its own row, `user_id` is the session's task
   owner, the task owner is an active user of the session's organization in the runner's **verified** log and `sig`
   verifies under that user's signing key over `P‖"orchestrator-handover\0"‖canonical({"ct","pairing","session"})`.
   Anything else is refused (log line `handover_refused`, the pairing stays `registered`). A hub therefore cannot hand
   the session an account of its choosing.
5. The runner passes `{pairing_id, ct, user_id, user_pub:{kem,sig}}` (the keys from the verified log) unopened to the
   guest (`POST /v1/sessions/{id}/orchestrator/handover`) and marks the pairing `handed_over`. The guest has the
   tool claim (`agent-platform pair --session --handover`): the tool opens `ct` (remote handover format of ADR-067),
   claims through the relay, checks the anchor against `user_pub` and `GET /v1/me` and stores its state. Then the
   guest starts the tool's MCP server (`agent-platform mcp --socket /run/agent-orch/mcp.sock`) and restarts the
   session's agent with `--resume` and the second MCP configuration (§17.5) once the session is `idle`.

### 17.2 Hub relay

The tool has no network: it signs every hub request itself (`agent-request-v1`, key ID `agent.<32 hex>`) and hands
`{method, target, headers, body}` to the guest control process over `/run/agent-guest/orch.sock` (§17.4). The control
process journals it as the pending call `relay` (journal kind `hub.relay {pending_id, method, target, headers,
body}`, `body` b64u) of the orchestrator session and waits for the answer (`RELAY_WAIT_SECONDS`, 40 s). The runner
(`orchestrator/journal.py`) forwards the bytes unchanged to **its own hub origin** – only for a session that has an
orchestrator pairing (`registered` or `handed_over`) and has not ended, only with the methods `GET`, `POST`, `PUT`,
`PATCH`, `DELETE`, a target under `/v1/` without `..` segments, the headers `Agent-Key` (an `agent.` key ID),
`Agent-Time`, `Agent-Nonce`, `Agent-Signature`, `Content-Type` and a body of at most `RELAY_MAX_BODY` (2 MiB) – and
answers with `POST /v1/relay/{pending_id}/answer {status, headers, body}` (the headers `Agent-Hub-Key`,
`Agent-Hub-Time`, `Agent-Hub-Signature`, `Content-Type`, `Retry-After` as the hub sent them; body b64u, at most
`RELAY_MAX_RESPONSE` = 8 MiB; a header value longer than the contract allows is left out) or
`{error:"refused"|"hub_unreachable"|"too_large"}` (`refused` also for more than 16 relays of one session at a time,
`hub_unreachable` also for any unexpected failure of the forwarding, logged by type). The tool verifies the answer
with its pinned hub key as every agent does; the runner never signs, opens or changes anything of it. A relay record
of any other session is answered `refused` and logged (`relay_refused`).

### 17.3 Guest control routes (runner → guest, `03-api.md` §7)

| Endpoint | Request → Response |
|---|---|
| `POST /v1/sessions/{id}/orchestrator/keys` | `{hub_url, hub_key:{id,public_key}, platform_root_key}` → `{pairing_id, pub:{kem,sig}, pop, fp}`; only for an active main session while no other session of the VM is (or is becoming) the orchestrator (`state_conflict`); a repeat for the same session before the handover returns the same pairing; the tool failing is `unavailable` |
| `POST /v1/sessions/{id}/orchestrator/handover` | `{pairing_id, ct, user_id, user_pub:{kem,sig}}` → `{}` (the claim runs in the background); another pairing or no keys `state_conflict`; a repeat of the same handover `{}` |
| `POST /v1/sessions/{id}/orchestrator/end` | `{pairing_id}` → `{}`; the guest stops the tool and deletes its keys when the session's orchestrator is still that pairing (revoked at the hub, or the session ended while suspended), else nothing |
| `POST /v1/relay/{id}/answer` | `{status, headers, body}` or `{error}` → `{}`; answers a `relay` pending call (unknown `404 not_found`, a second different answer `409 state_conflict`) |

### 17.4 The account `agent-orch` in the guest

`install.sh` creates the system user and group `agent-orch` (no login, no home, no network: the last rule of
`agent-sessions.nft` drops everything it sends) and installs the agent tool with its pinned wheels to `/opt/agent-orch`
(`root:agent-orch`, directories `0750`, files `0640`: neither the session users nor the API process can read or start
it; `images/README.md`). The state directory `/var/lib/agent-orch` is `0700` of `agent-orch`; the session users (and so
Claude Code) cannot enter it. The guest control process (root) runs every tool command as `agent-orch` (`runuser`,
environment `AGENT_PLATFORM_HOME=/var/lib/agent-orch/state`, `AGENT_PLATFORM_RELAY=/run/agent-guest/orch.sock`, nothing
inherited); the MCP server runs as the transient unit `agent-orch-mcp` of that account. `orch.sock` is
`root:agent-orch 0660` (JSON lines like `api.sock`, op `relay`); the MCP socket lies in `/run/agent-orch`
(`agent-orch:platform 0710`), so only the account and the API process (`platform`) reach it. When the session is
stopped or failed, when the runner ends it (§17.1) and at the start of the control process when the recorded
orchestrator session no longer runs, the guest stops the unit and deletes the state directory's content (keys
included); a suspend keeps it (§17.6).

### 17.4a The agent tool in session mode

Session mode is the environment `AGENT_PLATFORM_RELAY` (the absolute path of `orch.sock`). The guest calls, as
`agent-orch`, with the JSON input on stdin and one JSON object on stdout (the tool's usual result):

| Command | stdin → result |
|---|---|
| `agent-platform pair --session` | `{session_id, hub_url, hub_key:{id,public_key}, platform_root_key}` → `{ok:true, state:"waiting", pairing_id, pub:{kem,sig}, pop, fp}`; fresh keys in the state directory (a pending session pairing is replaced); nothing secret is printed |
| `agent-platform pair --session --handover` | `{pairing_id, ct, user_id, user_pub:{kem,sig}}` → `{ok:true, state:"connected", agent_id, user_id}`; opens `ct` with the pending keys, claims through the relay, requires the anchor's keys to equal `user_pub` and `GET /v1/me`, stores the state; a failure (`pairing_failed` with `reason`) deletes the pending pairing |
| `agent-platform mcp --socket <path>` | serves `POST /mcp` (HTTP/1.1, JSON-RPC 2.0, JSON answers, `202` for notifications) on the unix socket `<path>` (mode `0666`; the directory guards it) until `SIGTERM` |

Every signed request of the tool goes through the relay: one JSON line `{"id":<int>,"op":"relay","args":{method,
target, headers, body}}` on `orch.sock` (`body` b64u, `headers` the four `Agent-*` headers and `Content-Type` for a
body), answered `{"id":…, "ok":true, "result":{status, headers, body}}` or `{"id":…, "ok":false, "error":
"refused"|"hub_unreachable"|"too_large"|"timeout"|"unavailable"|"request_invalid"}`. The tool sends no anonymous
request in session mode (`not_in_session`) and verifies every answer with the hub key pinned from the `pair
--session` input.

### 17.5 The second MCP server (session API, `03-api.md` §8)

`POST /mcp-orchestrator` on `127.0.0.1:7700` authenticates like `/mcp` (session token bound to the peer UID); it answers
only the session that is the **claimed** orchestrator (any other session `404 not_found`) and forwards the JSON-RPC body
(≤ 1 MiB) to the tool's MCP server on `/run/agent-orch/mcp.sock`. Claude Code gets the server (`orchestrator`, tools
`search` and `run`, KOR-63) from `/opt/agent-guest/mcp-orchestrator.json` as a second `--mcp-config` only after the
claim; Claude Code never starts the tool. In session mode the tool runs only the guest's own command lines (`pair
--session [--handover]`, `mcp --socket <path>`) and an allow-list of hub commands that print (`relay.SESSION_ALLOWED`:
no local login, browser, pairing, registration, recovery or installation, no secret printed into the session, no
long-running server), and refuses every option that writes a path (`--output`, `--config`): the session cannot make the
account overwrite its own key store.

### 17.6 No context checkpoint, no idle suspend, the end

An orchestrator session (a row in `runner.orchestrator`) never becomes due for a context checkpoint (§6, KOR-33): it
reads every scope of its user, and a checkpoint sealed under its workgroup's key would show that to every reader of the
workgroup. Billing is unchanged. Nor is it suspended when idle (§6.3): a suspend would end its run, and the guest keeps
the keys only while the session runs; the guest ends the orchestrator only when the session is stopped or failed (and
when the runner ends it). The runner deletes the row once the session ended, together with a checkpoint still due
for it. The relay also stops once the verified log no longer shows the organization private with the task owner as
its only user (§17.2), and only the pairing's own key (`Agent-Key` = `agent.<pairing id hex>`) is relayed.
