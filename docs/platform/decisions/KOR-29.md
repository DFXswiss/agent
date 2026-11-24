# KOR-29 – decisions

Work VM on request (ADR-059, spec `13-work-vm.md`).

1. **Tools instead of Claude Code's Bash tool.** The session works in the VM through five MCP tools with a Bash-like
   contract and a mirrored workspace, not through a redirected Bash tool (ADR-059, alternatives).
2. **Exec calls are not operations.** `workvm.request`/`workvm.release` are registered operations (audited at the
   hub); the frequent command calls travel as the guest journal kind `workvm.call` and are visible as `session.tool`
   events and in the terminal. A hub registration per command would add a round trip to every command without adding
   information the operation record of the VM does not already carry.
3. **Boundary on the host, not in the VM.** Lima's user-mode network exposes the host loopback at `host.lima.internal`; the
   session is root in the VM. The runner therefore starts work VMs as a dedicated macOS account and a pf anchor filters
   that account's sockets (spec 11 §6).
4. **One VM per task, optional own VM per session** (ADR-059 §7).
5. **Whole-workspace export.** The guest export accepts `dir="."` (the whole workspace) so the runner can mirror the
   workspace in one call; import still refuses `.` (from_vm needs a directory).
6. **Migration number 0012, ADR number 057.** Runner migrations must be gapless (`store/migrate.py`); on top of
   `ad5f2026-live` (last 0011) the next is 0012. A package that lands on the live branch first with its own 0012
   (KOR-25 currently carries 0011_operation_hold) moves this file to the next free number at the rebase. ADR-059 is
   above the numbers of the parallel packages (KOR-24/KOR-27: ADR-054/056).
7. **Addendum 1 of the client** (credentials are the session's business; internet only through Cloudflare,
   fail-closed): no destination log (the former `runner.workvm_netlog` is dropped), no scanning in the VM; the
   Cloudflare requirements went to the operator as one issue text (work-package report `KOR-29-cloudflare-bedarf`).
8. **Security review of the design (fresh-context subagent, before building) – dispositions:**
   - H1 (Lima's SSH over vsock still uses a host-loopback proxy; confirmed on a real VM: "Started vsock forwarder:
     127.0.0.1:<port> -> vsock:22"): one macOS account per slot with a pinned SSH port; pf allows each slot only its
     own port on loopback. Fixed in spec 11 §6.1/§6.2.
   - H2 (host's own addresses over lo0, global IPv6): address-independent lo0 block per slot and the host's interface
     addresses in the blocked table; IPv6 egress refused until the gateway offers it. Fixed.
   - H3 (gvisor-tap-vsock services, guest-agent port events): template without forwarding, `guestIPMustBeZero:
     false`, attack case WVM-09. Fixed in spec; verified by the attack case on a real VM.
   - H4 (host disk exhaustion): own APFS volume with quota (built). Disk in admission and log truncation are not
     built (#17, #20).
   - M1 (pf caps per protocol, rate cap shared, ICMP): per-slot state caps, no global cap; the pipes are configured
     but not fed (#20); ICMP is not forwarded
     by the egress path (only TCP/UDP through the gateway). Fixed.
   - M2 (fail-closed check incomplete): `pfctl -E` reference daemon, main-ruleset and skip checks, check before every
     start and every minute, stop running VMs on failure. Fixed.
   - M3 (shared account blast radius): per-slot accounts, `_config` root-owned. Fixed.
   - M4 (sudo environment): `sudo -n -H` with `env -i` and an explicit environment. Fixed.
   - M5 (from_vm risks): host-side re-pack, merge semantics keeping excluded files, change counts in the answer.
     Fixed.
   - M6 (DNS through the host resolver bypasses the filter and the site address rule): host resolver off, DNS through
     the gateway. Fixed.
   - M7 (fairness): organization RAM share, disk in admission. Fixed.
   - L1 (proxy env, cidata, host keys): `propagateProxyEnv: false`, credential search includes cidata, fresh host
     keys per clone. Fixed.
   - L2 (journal record bound to its source VM): the runner resolves the session from the journal of the workgroup VM
     that carried the record. Fixed.
   - L3 (restrictions inside the VM): `stdin` added to `workvm_exec`, idle teardown after 30 min (`WORKVM_IDLE_SECONDS`, addendum 2) and never while a job runs, no
     global rate cap; port 25 is the one intended outbound restriction (addendum 1). A pty is not offered (Claude
     Code's tools are not interactive either); recorded.

9. **Addendum 3 (Cloudflare's answer, the client's clarification): the client on the device replaces the gateway
   VM.** The operator installs the Cloudflare One client on the execution device; its tunnel carries the device's
   traffic with Cloudflare's shared egress addresses. The anchor now passes a slot account only `on utunN` (the
   client's tunnel) instead of `route-to` a gateway VM; `WORKVM_GATEWAY` is removed; the VMs resolve through
   Cloudflare's public resolvers in the tunnel. Fail-closed holds without the watcher: a disconnected client moves the
   routes to the physical interface, where no rule passes a slot account.
10. **A root watcher instead of rules that follow the interface by themselves.** macOS pf has no reliable interface
    group for `utun`, and the client's interface number changes. A root-owned shell script (LaunchDaemon
    `swiss.dfx.agent-runner.pf`, `KeepAlive`, 5 s rounds) loads the full anchor with the current interface, or the
    fail-closed anchor; it is generated by Python (`firewall.watch_script`) and tested for real under `/bin/sh` with
    stub commands. A sub-anchor holding only the pass rule was rejected: whether `quick` in a nested anchor ends the
    evaluation of the parent on macOS is not verified, and the whole anchor in one file keeps the reviewed rule order.
11. **The host-address table entry is dropped.** The host's own addresses are delivered over lo0 (blocked for slot
    accounts regardless of address) and nothing passes on the physical interface, so the runner no longer needs root
    to keep host addresses in the table; the table holds the static special-purpose ranges only and the anchor is
    static per slot set.
12. **The runner reads pf through `sudo -n` with exact commands.** `pfctl` needs root to read; the sudoers file now
    also allows the runner the five read-only `pfctl` invocations of the check (exact arguments, no wildcard).
13. **`agent-runner workvm install|firewall render|firewall check|status`** (area CLI `workvm/cli.py`) installs the
    volume, the images directory, the group and slot accounts, the wrapper, sudoers and the firewall in one
    idempotent root command; spec section 6.2 names it.
14. **Operator checks as a script** (`images/workvm/egress-check.sh`): the checks of the Cloudflare issue run as a
    slot account on the host (the VMs' traffic leaves from the slot account's sockets), optionally inside a VM, and
    with `--disconnect-test` the fail-closed case. The site address is an argument, never in the repository.
15. **ICMP is outside the per-account filter (residual risk, open).** pf's `user` matches TCP and UDP only, so the
    slot rules do not see ICMP. If Lima's user network forwards a guest's echo requests through unprivileged ICMP
    sockets, a VM can ping LAN hosts and, while the client is disconnected, send echo requests with the site address.
    No TCP/UDP payload leaves that way. A host-wide ICMP rule would affect the production device, so none is added;
    attack case WVM-03 measures it on the first device and the result decides a follow-up.
16. **Inbound:** the anchor holds only `out` rules. Lima's host agent exposes no forward (template), and attack case
    WVM-09 tries the user-network `expose` service; a port opened that way would belong to the slot account and be
    reachable only as far as the main ruleset passes inbound traffic (the device's own policy).
17. **Admission by organization RAM share and by disk (spec 11 §7) is not built yet**; admission counts VMs per
    task/organization/owner and RAM. Open for stage (b); the threat model says so.
18. **Attack suite** `docs/platform/atk/WVM.md` with the in-VM probe script `images/workvm/attack-probes.sh` (WVM-01 …
    WVM-10, scripted) and a red-team prompt (model-driven); written, not yet run on a device with the host filter.

19. **Review of the diff (fresh-context subagent, 2026-11-23) – dispositions.**
    - H1 (a failing check never stopped running VMs): `WorkVms.guard_pass` runs every minute in the daemon task and
      tears down every live VM when the boundary check fails (reason `failed`); the watcher enables pf again when
      it finds it disabled. Fixed.
    - H2 (the slot's own SSH proxy answers on lo0 as the slot account and was blocked): rule
      `pass out quick on lo0 proto tcp from 127.0.0.1 port P to 127.0.0.1 user U no state`. Fixed (to be confirmed
      on the first device, WVM-01).
    - H3 (ICMP outside `user` rules): ICMP echo of the whole device passes only on the tunnel interface and is
      dropped on every other interface except lo0. This also drops the device's own pings to the LAN; remote access
      (TCP) is untouched. Fixed; #15 is superseded.
    - M4 (floating states survive a disconnect): the pass rules keep state `if-bound`. Fixed.
    - M5 (no notice without internet): not fixed; the boundary computes `internet`, the session and the UI do not
      show it yet (stage d). Open.
    - M6 (waits beyond the guest's 33 s budget): the runner waits only what remains of the guest's budget minus a
      reserve (3 s, browse 8 s). Fixed. An implicit VM start longer than the budget still ends the first call with
      `timeout`; the VM is there for the next call (stage b, warm pool).
    - M7 (shared `/workspace` of the task VM): a subagent works only in its own session VM (implicitly created with
      scope `session`); main sessions keep the task VM. Fixed.
    - M8 (a failed wait blocked the queue): the ticket leaves the queue on every exit. Fixed.
    - M9/M10 (controls claimed but not built): pipes not fed, disk admission, log truncation, organization RAM share:
      the documents now say "not built" (#17, #20).
    - L11 (oversized run event dropped), L12 (a failed `limactl delete` leaves an orphan until restart), L13 (check
      of skipped interfaces other than lo0 and of anchors before ours): open, recorded.
    - L14 (root watcher runs `warp-cli` from `/usr/local/bin`): the watcher runs the binary inside the root-owned app
      bundle. Fixed.
    - L15 (IPv6 egress compared with the IPv4 site address): `--site-ip6`. Fixed.
    - L16 (idle time-out 4 h vs. 30 min): default 1800 s as in spec and harness. Fixed.
    - L17 (site name in the ADR quote): replaced. Fixed.
20. **Not built:** dummynet rules feeding the per-slot pipes (macOS grammar unverified), disk admission and log
    truncation, organization RAM share, the no-internet notice, a user-initiated release (UI-1), full host values of
    slot-account VMs in the monitoring (MON-3). Built in attempt 4: warm pool (#21), monitoring (MON-1…3), session
    panel (UI-1…3), a real run on a test device (report KOR-29).
21. **Warm pool (attempt 4):** a separate table `runner.workvm_pool` instead of unassigned `runner.workvm` rows: the
    work VM row keeps its NOT NULL attribution (task, organization, workgroup) and the runtime of a row is exactly
    the time a task had the VM (KONZEPT idea 46). Slots are taken under one lock across both tables. Only the
    standard size is pooled (the common case; a larger pool entry would hold RAM most requests do not need). The
    pool keeps the RAM of one more standard VM free so a cold request never waits behind the pool, and gives a slot
    back to a waiting request. The pool lives in migration 0012 (not yet on the integration branch).
UI-1. **Session panel without a release button:** the web cannot request operations – `POST /v1/device/operations`
    is the device's route (`device:session`), and the runner executes only operations its session requested. A
    button would need a user-origin operation path in hub and runner (rights, audit, signature of the user instead
    of the device), which is larger than the panel; the user ends the VM by stopping the session
    (`session_ended`), the session by `workvm.release`, and the idle teardown covers the rest. The panel says so.
UI-2. **Countdown only for explicit requests:** the panel counts down while a `workvm.request` operation row of the
    session is open (from its `created_at`, target 10 s, "delayed" after it). An implicit request inside
    `workvm_exec` writes no operation row; the panel shows `running` with the `created` event.
UI-3. **The panel lists the session's runs itself** (latest per job, newest first, with the stored output tail –
    KONZEPT idea 27: logs and exit codes retrievable in the interface) by reusing the `workvm.run` chat item; live
    CPU/RAM/disk/network stay in the monitoring view.
MON-1. **Work VMs in the monitoring as entity kind `workvm`** (contracts: `common.json#/$defs/workvm_id`, `metric_sample`
    of the outbox with optional `session_id`, overview list `workvms`, series and live frames accept `workvm`). The hub
    keeps no row per work VM (it sees only IDs it already knows: device, session); the work VM is attributed to its
    session by `session_id` in each sample, which must run on the pushing device, and a work VM ID stays with the device
    and session of its newest sample (`403 forbidden` otherwise). Hub migration `0010_workvm_metrics` (gapless order of
    the hub's migrations forces 0010 on this branch; renumber on rebase if another 0010 lands first). Rollout: hub
    before runner (an older hub refuses a `workvm` sample and with it the whole `metrics` item).
MON-2. **Overview: device → VM → session → work VM.** The web lists a session's work VMs below the session (CPU, RAM,
    disk, network, runtime, charts); a work VM disappears 15 minutes after its last sample (same grace as an ended
    session). Series of a work VM are readable while its raw samples exist (48 h): the 5-minute rollups carry no
    session, so an older range of a work VM is `404` – the metering of KONZEPT idea 46 uses the `workvm.state` runtime,
    not the series.
MON-3. **Runner measures only from outside** (`agent_runner.metrics.workvm`): the host probes of the workgroup VMs over
    each slot's `limactl list`. With slot accounts the runner cannot read their 0700 Lima homes or (without root)
    `lsof` their processes, so CPU, RSS, disk blocks and network may be null there; sizes, state and the runtime since
    creation are always sent. Nothing runs inside the work VM for the monitoring (addendum 1). Open: a root-owned
    read-only probe per slot (like the limactl wrapper) for full host values; Prometheus textfile series for work VMs.
22. **Packet filter without the main ruleset (stage b, operations finding 1, 2026-11-24).** The Cloudflare client
    hooks its anchor `com.cloudflare.warp_svc` into the running main ruleset and keeps pf enabled; the installer of
    stage a (an `anchor` line in `/etc/pf.conf`, then `pfctl -f /etc/pf.conf`) would have dropped the client's
    anchor, and a client reload could drop ours (fail-open). It was never run on a device. Now:
    - the rules live in Apple's wildcard anchor as `com.apple/000.agent.workvm` (`000.` sorts before Apple's own
      children, which pf evaluates in name order) (loaded alone with `pfctl -a … -f`;
      evaluated through `anchor "com.apple/*"`, which the macOS main ruleset holds before the client's line - the
      approach INF-07 uses for the test device's network guard). `/etc/pf.conf` and the main ruleset are never
      changed or loaded; `uninstall` only removes the lines of the old installer from the file;
    - the watcher checks effectiveness every 2 s (pf enabled - `-E`, never `-d`; `com.apple/*` as the first `anchor`
      line with no `pass quick` before it; our anchor loaded and the first child of `com.apple`). Without an effective hook it kills every process of the slot
      accounts (`pkill -KILL -u`) each round and writes `unhooked`; it never repairs the main ruleset itself. The
      runner's check reports `not_hooked` and tears the VMs down; requests answer "not ready";
    - `install --plan` (read-only, exit 1 without hook), `install` refuses without hook, `uninstall [--keep-volume]`
      removes everything and reports what is left (exit 1);
    - the device-wide ICMP rules of #19 H3 are gone (the operator asked for no device-wide rules the boundary does
      not need); residual: ICMP from a slot's host agent to the ranges the client passes directly (threat model);
    - the dummynet pipes are gone (never fed, #20); bandwidth shaping stays open;
    - the client is `/usr/local/bin/warp-cli --accept-tos status` (operator facts); its interface name is read from
      the route, never fixed; a self-update or reconnect closes the tunnel route (fail-closed) and the watcher opens
      again by itself;
    - `images/workvm/host-rehearsal.sh` proves the assumptions on a test device with the client (root): plan, one
      slot, checks, disconnect/reconnect, service restart, uninstall, state compared with before, a deadline timer
      that uninstalls by itself; it refuses a device that already has an installation.
23. **Review of attempt 4 (fresh-context subagent, 2026-11-23) – dispositions.** H1 duplicate hub migration →
    renumbered. H2 a waiting request dropped a pool entry that was still starting → only `ready` entries are dropped,
    removal takes the row atomically (`DELETE … RETURNING`) and only that caller deletes the VM; an entry removed
    while it started takes its VM along. M3 the head of the queue now takes a ready pool entry. M4 one pool entry per
    pass, an immediate next pass. M6 a work VM ID two devices drew drops only that VM's samples, not the item. M7 the
    panel counts the VMs the session ran a job in. Open: M5 (a task VM disappears from the overview 15 min after its
    requesting session ended while other sessions use it), M8 (a real concurrency test of two `pool_assign`), L9 (a
    head needing more RAM than the pool frees still empties the pool), L10 (unhealthy pool entry without an event -
    intended), L11 (the monitoring round has no time limit for `limactl list`), L12 (hub before runner only documented),
    L13 (migration 0012 was extended in place before it was merged - now merged, so later changes need a new file).
24. **Existing workgroup VMs keep their golden (operations finding 3).** The work VM tools reach a session only with
    the new golden. A lighter way than ending paused sessions would be an MCP server registration the guest reads at
    every session start from the runner (no golden change); not built here (KOR-33 changes resume).
25. **Review of stage b (fresh-context subagent, 2026-11-24) – dispositions.** Fixed: the rehearsal's timer survives a
    hang-up (`trap '' HUP`, `nohup`) and reconnects the client; our anchor is named `000.agent.workvm` and must be
    the first child of `com.apple` (watcher `unordered`, check `not_first`); `uninstall` stops the slot processes
    before and after flushing the anchor; the watcher runs every 2 s (the window after the client disables pf); one
    hook rule everywhere (`com.apple/*` is the first `anchor` line, no `pass quick` before it - watcher, check,
    egress check, rehearsal); the rehearsal refuses any trace of an installation (files, anchor, volume, slot
    accounts), compares anchors and groups before/after, checks LAN and loopback while disconnected and compares a
    leak with the site address; `uninstall` also reports the template, sample, state file and a remaining volume.
    Documented: ICMP falls through to the client's rules; the watcher never repairs the main ruleset (ADR-059).
    Open: `uninstall` follows the configured slot count (a lowered count leaves accounts - run it with the count of
    the installation); a reboot of the test device is not part of the rehearsal; no tests for the shell scripts
    beyond syntax and shellcheck.
26. **Open review points of #23, done (2026-11-24).** M5: an ended session stays in the monitoring overview while a
    work VM it requested still sends samples (the task VM the task's other sessions use). M8: a test runs three
    concurrent `pool_assign` against two ready entries (two different entries taken, one `None`). L9: a waiting
    request drops a ready pool entry only when the RAM of all ready entries would make it fit. L11: `limactl list`
    of the monitoring round is bounded (10 s; a hanging listing leaves that slot's host values empty). Still open:
    L10 (intended), L12 (hub before runner, documented), L13 (in-place change before merge, past).
27. **No web search tool in the session sandbox; search goes through the work VM's browser.** The package asks for
    the session's web search "where the model access allows it". Claude Code's `WebSearch` is a provider-side tool,
    but it is one of the tools the session sandbox denies (attack case ATK-05, requirements R-10.4/R-13.3, E2E-23);
    the package keeps the existing sandbox cases in force, and `WebFetch` would reach the network from the sandbox
    itself. The work VM already gives the session full internet and a browser (`workvm_browse`, a search engine page
    included), so nothing the session needs is missing. Enabling `WebSearch` is a separate decision on the sandbox's
    tool list (one line in `agent_guest.driver.DISALLOWED_TOOLS` and `sandbox.settings.DENIED_TOOLS`, ATK-05 and
    E2E-23 turned around) and stays open.
28. **Runbook** `docs/platform/runbooks/work-vms.md`: rehearsal, installation with `--plan`, operation, faults,
    removal.
29. **ICMP and the restart check (operator addendum 2026-11-24 06:15).** The Cloudflare client passes ICMP to the
    private networks, and pf's `user` cannot attribute ICMP. The work VMs' ICMP never reaches the host stack: Lima's
    user network (gvisor-tap-vsock) does not forward ICMP out of its virtual network. `egress-check.sh` now checks
    UDP to the gateway for the slot account (`lan_udp`), reports the slot account's own ICMP socket for information
    (`lan_icmp_slot_socket`, no slot process pings), and with `--vm` checks ICMP (with a documentation address to
    detect faked replies), TCP and UDP from inside the VM to the site network. No device-wide ICMP rule (it would
    touch the device's own traffic). `images/workvm/after-reboot-check.sh` checks after a restart that the volume
    is mounted again (the open assumption of a custom `addVolume` mount point), the homes, the watcher, the anchor
    and the hook; the rehearsal runs it once as a baseline.
