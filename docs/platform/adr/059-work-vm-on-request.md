# ADR-059: Work VM on request – the session asks for its own Ubuntu VM and may do anything in it

Status: Accepted (2026-11-23) · Decision of the client · Specification: `docs/platform/spec/13-work-vm.md` ·
Threat model: `docs/platform/security/work-vm-threat-model.md`

## Context
A session on the platform refused to open a web page: it has no shell and no web access ([KONZEPT §10](../KONZEPT.md#k-10), 05-runner.md
§4). On 2026-11-23 the client decided, verbatim:

> "ich will dass meine ki sessions alles tun können, die sollen vollständig produktiv sein! zwingend! die Session
> selbst ist nur eine session, aber wenn ein ki agent sowas wie eine software ausführen will oder so muss der ki agent
> eine ubuntu vm anfordern können, dann bekommt er zugriff auf eine separate isolierte ubuntu vm in einem eigenen lim
> vm und er kannt damit alles machen, ich meine wirklich alles, keine einschränkungen ,ich will hier maximale
> produktivität!"

and before that "du solltest doch eine ubtuntu vm benutzen können" and "natürlich soll die ki selber ausführen".
KONZEPT.md does not contain this (§10 forbids the session every execution; a VM on request does not occur). The
decision is new and binding; KONZEPT.md stays unchanged as a file, this ADR and `spec-deviations.md` record the
change.

## Decision
1. **The session stays a session.** It runs as before in the workgroup VM inside its two-layer sandbox (ADR-008)
   with harness, hooks, gateway and platform tools. Nothing is executed there; Bash and every other execution tool
   stay denied in the session sandbox.
2. **Request through a platform operation.** `workvm.request` (and `workvm.release`) are operations of 03-api.md §9:
   own record, formal check, registration at the hub, audit. The runner of the device that runs the session executes
   them.
3. **Own, separate, isolated Ubuntu VM.** A work VM is its own Lima instance (Ubuntu 24.04 LTS, `vz`) from the work
   image, never the workgroup VM and never shared with another task.
4. **Anything inside.** Commands run as root, with full internet, long-running and background processes, services,
   containers, a browser. There is no command filter, no allow-list, no confirmation and no hook on what runs inside
   the VM. The yardstick is an experienced developer with a fresh Ubuntu machine.
5. **Tight outside.** The VM reaches the internet only, and only **through Cloudflare** (Cloudflare's shared egress
   addresses, addendum 3), never with the public address of the site of the execution device (fail-closed: no
   Cloudflare path, no internet). It reaches neither the execution device, nor other VMs, nor private, link-local, loopback or metadata
   addresses. The **platform** puts no credential into the VM: no model token, no GitHub token, no platform key; code
   enters through the platform's checkout and the workspace mirror and leaves through the controlled GitHub
   operations. What the session itself stores or uses inside its VM (a token the user hands it, a login at a
   service) is its own business: no prevention, no monitoring, no scanning inside the VM (decision of the client,
   addendum 1 below).
6. **How the session works in it – MCP tools plus a mirrored workspace.** Platform tools run commands
   (`workvm_exec`, `workvm_output`, `workvm_kill`), use the browser (`workvm_browse`), report state (`workvm_status`)
   and move files (`workvm_sync`). The session workspace is mirrored to `/workspace` in the VM before every command
   (changed files only), so the file tools of the session and the commands in the VM see the same tree;
   `workvm_sync` with `from_vm` brings results (formatted sources, generated files, screenshots) back into the
   workspace, where the GitHub operations commit them. **Running tests and using the browser need no detour**: when
   the session has no VM, `workvm_exec` and `workvm_browse` obtain one (standard size, task scope) on the way; the
   explicit `workvm.request` stays for other sizes and for a session-scoped VM.
7. **One VM per task for the main session, an own VM per subagent.** The main session uses the task's VM (scope
   `task`); it may also ask for `{"scope": "session"}`. A platform subagent ([KONZEPT idea 26](../KONZEPT.md#k-idee-26)) always gets a VM of
   its own (scope `session`, assigned to it, its own limits and attribution): its workspace differs from its
   parent's, and a VM's `/workspace` mirrors exactly one workspace, so sharing would run one session's tests on the
   other's files (review finding M7). A subagent that should test its parent's state receives the files through its
   own workspace.
8. **Temporary ([KONZEPT ideas 27](../KONZEPT.md#k-idee-27), 28, 44; addendum 2).** A work VM lives as long as the session needs it: it is torn
   down on `workvm.release`, after `WORKVM_IDLE_SECONDS` without a call or running job (default 1800 s; since
   addendum 4 the idle VM is stopped and gives its slot back, default 600 s), when its
   session ends (scope `session`) or when no session of the task is active any more (scope `task`), when the task is
   closed, and always also after errors. There is no resume: after a failure of the device or the runner every left
   over work VM is deleted; the next command obtains a fresh one. The session may request a new VM at any time.
9. **Every run is kept (idea 27).** Command, state, exit code, start, end, duration and the output (tail of 64 KiB,
   total size) of every job are stored as the encrypted event `workvm.run` of the session ([KONZEPT §5](../KONZEPT.md#k-5): the hub sees
   ciphertext only), when the job starts and when it ends – also at teardown and after a failure (state `lost`) – and
   are readable in the web client with the session. Lifecycle (`created`, `deleted` with reason) and the runtime are
   the encrypted event `workvm.state`.
10. **Runtime is measured to the second (idea 46).** Every work VM's runtime (start to teardown) is recorded per VM
   with task, session, workgroup and organization in the runner (`runner.workvm`, seconds) and in the `deleted`
   `workvm.state` event, shown in the web client. Billing and credit (ideas 46/47) are not part of this package; the
   measurement is complete and attributable for them (a later package moves the per-day sums to the hub as metadata,
   like the token usage of ADR-037).
11. **Capacity and scale (idea 31, §16).** Work VMs share the device's RAM budget with workgroup VMs (05 §3.4) and
   have own limits per task, organization and owner. A request that does not fit waits in a queue (the operation stays
   `executing`, the MCP call answers `pending`, the session is pinged when the VM is ready) instead of failing. One
   device carries as many work VMs as RAM allows (a 512 GiB device with 32 GiB reserve: 60 standard work VMs, fewer
   when workgroup VMs run); the target picture of hundreds of parallel agents with test and browser VMs needs several
   execution devices: the hub already assigns workgroups to devices (ADR-013), and work VMs are local to the device of
   the requesting session, so capacity grows with every added device; placement of a work VM on another device than
   its session is not part of this package (it would need the workspace mirror across devices).

### Addendum 4 (2026-11-26): idle work VMs give their slot back
**Finding in operation (2026-11-26).** The platform device allows 4 work VMs at a time (`WORKVM_SLOTS=4`). All 4 slots
were assigned, but only 2 VMs computed; the other two held 5.0 and 2.8 GB at 0–1 % CPU. Sessions waited for a slot
for their tests ("Die Work-VM wartet noch auf Zuteilung (Status pending)"). In the morning the work VMs of closed
tasks kept their slots; five parallel tasks waited more than eight minutes until the slots were freed by hand. The
hardware had room (CPU about 17 %, 96 % of the RAM free): the bottleneck was the slot assignment alone. Causes in the
code: a stopped VM held its slot (database rule "one live VM per slot"), an idle VM held its slot for 30 minutes
before it was deleted, a VM whose session end or task close the runner missed waited for that idle time as well, and
a row whose instance was gone or whose start was interrupted held its slot until the next restart.

**Decision.** The slot assignment of this ADR (decision 11, `agent_runner/workvm/`) stays the only mechanism; no
second one. A slot counts only while its work VM is needed, and how many work VMs run is decided by the free
resources, not by a fixed number (points 7-9):
1. **At once** when the task is closed or its session ends (decision 8, unchanged: reconcile step `workvm`,
   observer of the session states).
2. **After the idle time** (`WORKVM_IDLE_SECONDS`, now default 600 instead of 1800, settable 60–604800): no tool call,
   no running job and no call in progress for that long - measured by the runner's own activity record, not by CPU.
   The VM is **stopped**, not deleted (`limactl stop`, `workvm.state` `stopped` with reason `idle`): its disk stays in
   its slot's Lima home, it holds neither the slot nor RAM, and the next call or `workvm.request` starts it again with
   everything it had (decision 8's "temporary" now means: a stopped VM is deleted after
   `WORKVM_KEEP_STOPPED_SECONDS`, default 86400, settable 600–2592000, when its session ends, when its task is closed,
   or when the disk quota needs its space - each visible as `workvm.state` `deleted` with its reason). Nothing is lost
   silently: a VM that cannot start again is deleted with reason `failed` and the call gets a new one; the session's
   workspace and checkpoints (ADR-072, HOT-24) are untouched by all of this.
3. **Slots only for VMs that are not stopped.** The database rule becomes "one VM per slot that is not stopped"
   (migration `0038_workvm_slots`). A stopped VM starts again only in its own slot (its disk lives in that slot
   account's Lima home and the slot's pinned SSH port is the only one the host filter opens for it); a new VM prefers
   the free slot with the fewest stopped VMs, so that slot is mostly free when it is needed.
4. **Queue.** Waiting requests take a slot that becomes free at once (the release wakes them, the 2 s poll stays as a
   fallback) and in the order they came in; a stopped VM whose own slot another VM holds cannot start anyway and does
   not hold up the requests behind it. The waiting requests are mirrored in `runner.workvm_wait` for the status.
5. **Reconciliation.** At start (unchanged: every VM left over is deleted, [KONZEPT idea 44](../KONZEPT.md#k-idee-44); now also the mirror of the
   waiting requests is cleared) and every minute (`slot_pass`): each row is compared with the instances Lima lists for
   its slot - an instance that is gone, a start nobody finishes, a VM that no longer runs, a stop that was interrupted
   - and with the sessions of the device (a task or session VM whose sessions all ended goes, `session_ended`). A slot
   can therefore never stay held.
6. **Visible.** The chat shows the `stopped`/`idle` event as one quiet line ("Arbeits-VM angehalten, ungenutzt –
   startet bei Bedarf neu"), the metrics `agent_runner_workvm_slots{state="held"|"free"}`,
   `agent_runner_workvm_requests_waiting`, `agent_runner_workvm_stopped` and what the admission sees free (RAM, CPU
   load, disk; 08-monitoring.md §3), and `agent-runner status` the fact `runner.status.workvm` with the same values.

7. **No fixed number of work VMs** (decisions of the client the same day: "4 Plätze, alle belegt, künftig 16 – das
   verstehe ich nicht, warum einen deckel", then "diese anderen deckel alle restlos entfernen, der platz soll für das
   gebraucht werden was halt gebraucht wird"). `WORKVM_SLOTS` and `WORKVM_MAX_PER_TASK`/`_OWNER`/`_ORG` are removed
   (ADR-075 addendum 2: an older `runner.env` that sets them starts, the values are ignored and named once). A device
   admits a new work, browser or test run VM as long as its free resources last - RAM above the host's base need
   (4 GiB, each VM with its host share, ADR-075 addendum 4) that the workgroup VMs and the work VMs leave, the host's
   1-minute load leaving about 1-2 cores free (or `CPU_LOAD_MAX_PERCENT`), the disk quota `WORKVM_DISK_QUOTA_GIB` - and its turn has come (point 8). Short of any of them
   the request waits in the queue as before. The slot accounts stay - they are the isolation boundary of the host
   filter (one account, one pinned SSH port per VM) - but their number follows the RAM: as many as the RAM above the
   reserve could ever hold standard work VMs (at most 100, the accounts' port range), so they never limit before the
   resources do. A device offers work VMs once `agent-runner workvm install` created them.
8. **One admission per device, fewest active first.** The work VMs use the device's one admission (05-runner.md §3.8,
   `agent_runner/admission.py`) that also admits the workgroup VMs and later CI VMs, in one transaction (one lock). No
   kind has a rank or a fixed share (final rule of the client, 2026-11-26: "immer der von dem es am wenigsten aktive
   gibt"): when room becomes free or requests compete, the waiting request of the kind with the fewest active instances
   on the device goes first, on a tie the older one; a kind alone may use the whole device. The running task and
   session VMs and the warm work VM pool count as *yielding* in the capacity
   report, so the hub's free workgroup slots (`vm_slots_free`) are not reduced by them and sessions are placed as if
   they were not there; when a workgroup VM then needs their RAM, the VM area asks for it
   (`<area>/capacity.py:RECLAIM`) and the work VMs give way at once - the pool first, then idle VMs (no job and no call
   in progress; stopped, `idle`, their disks stay and they start again when there is room), least recently used first.
   **Running work is never ended silently**: a VM with a job or a call in progress keeps running, and the session
   waits for room or is placed again by the hub (ADR-049). The warm workgroup pool never makes them give way (a warm
   VM is speculative). Run VMs of `test.run` and browser VMs belong to their run or browser session and do not yield.
   Hub traffic: new VMs are admitted only below the CPU bound, and each slot account's traffic stays capped by the
   host filter (`BANDWIDTH_MBIT`).
9. **Another device of the workgroup.** A work VM still runs on the device of its session (decision 11: the workspace
   mirror is local); a request that does not fit waits. Because work VMs no longer reduce the free workgroup slots and
   the device reports its work VMs (`work_vms_running`), the hub keeps placing new sessions on the devices with room.
   Placing a work VM on another device than its session stays outside this ADR.

Specification: 05-runner.md §3.7 and §3.8, 13-work-vm.md §2, §6.1 and §7, 16-ci-runners.md §6.2 and §6.6. Not changed:
the guest and the harness text (it still says "after 30 minutes without use"; the rule it gives - keep what must
survive in the workspace - stays true).

**Alternatives considered.** Deleting idle VMs earlier (no stop): loses what the session installed and makes the
next command wait for a fresh clone. Moving a stopped VM's disk to another slot's account: a copy across macOS
accounts, slower than waiting for the own slot. Idle by CPU: a session that thinks for a while before its next
command would lose its VM; a busy service would hold a slot nobody uses. A second watchdog beside the slot
assignment: two mechanisms that can disagree (excluded by the task).

### Addendum 2 (complete concept, 2026-11-23 19:30)
The complete [KONZEPT](../KONZEPT.md#k-titel) (47 ideas) describes temporary test VMs (idea 27: "Lint, Prettier, Unit- und
Integrationstests werden ausschliesslich über die Plattform beantragt … Claude Code führt keine Tests selbst aus")
and temporary browser VMs (idea 28: "… den die Session über kontrollierte Schnittstellen steuert. Aktivitäten sind
nachvollziehbar und live einsehbar. Nach Gebrauch wird die VM abgebaut"). The work VM is their general form as the
client specified it on 2026-11-23 ("er kann damit alles machen … keine Einschränkungen"); where they differ, the
younger statement of the client applies. **Resolution:** the session still runs nothing in its own sandbox; execution
happens in the requested VM, and the way there is the platform's controlled interface (the tools above, the
operations, the runner as the only component that reaches the VM). "Keine Einschränkungen" concerns what is allowed
*inside* the VM. Everything the session does through that path is visible live and kept (decision 9) – as
transparency for the user, not as control inside the VM (addendum 1 stays: nothing is monitored or prevented there).

### Addendum 1 (decision of the client, 2026-11-23 18:52, verbatim)
> "wenn der agent sich entscheidet in der vm selbst einen zugangstoken für irgendwas hinzuzufügen, dann ists halt so.
> ist nicht die idee der sache aber ich sehe es nicht als meine aufgabe seine aktivitäten in der vm zu überwachen,
> allerdings sollten wir schauen dass er nicht mit meiner ip ins internet geht sondern mit eigener eigenen ip welche
> wir über clpudeflar routen, dafür sollten wir extra bei cloudeflar eine zusätzliche statische ip registrieren, das
> macht sinn"

Consequences: no destination logging, no in-VM agents or scans; what the session does through the platform's
execution path stays visible in its chat and terminal (product transparency, [KONZEPT §6](../KONZEPT.md#k-6)). Requirements for the
Cloudflare side: one issue written for the operator (work-package report `KOR-29-cloudflare-bedarf`).

> **Not pursued (2026-11-25, [E-10](../KONZEPT.md#e-10)):** the dedicated static egress address asked for in
> addendum 1 is not procured, and no offer is requested for it. The client withdrew it on 2026-11-23 (addendum 3) and
> confirmed on 2026-11-25 that Cloudflare is used only on its free plans ("nein, wir bleiben bei den kostenlosen
> modellen"). The wording above stays as history.

### Addendum 3 of the client (2026-11-23): the Cloudflare client on the device, shared egress addresses
The operator answered the Cloudflare issue with a different design than the one first asked for: no dedicated
egress address, no Enterprise plan, no Mesh node and no gateway VM. The **Cloudflare One client runs on each
execution device itself** (macOS, enrolled unattended with a service token); the device's traffic, including what
the VMs send through the host, leaves with **Cloudflare's shared egress addresses**. The client, verbatim:

> "wir brauchen keine fixe ip für ubuntu vm, es geht nur darum dass sie nicht die normale ip von [dem] standort
> nimmt"

(The site's name in the quote is replaced by "[dem]"; device and site names stay out of this repository.)

So the requirement is: never the site address, fail-closed. The platform's part is the host packet filter: a slot
account may leave **only on the tunnel interface of the client** (`utunN`); a root watcher on the device loads that
one pass rule while the client is connected and the fail-closed rules otherwise. When the client disconnects, the
routes leave the tunnel and the pass rule stops matching at once, without waiting for the watcher. The device's own
traffic (runner, hub connection, remote access) is not touched by the anchor (it only matches the slot accounts),
so the filter can never lock the production device out. Cloudflare keeps logging DNS, HTTP and network on its side
(its decision, to answer abuse reports); on the device and in the VM nothing is monitored.

### The outside boundary in detail (spec 11 §6)
- **One macOS account per work-VM slot.** Lima's user-mode network turns every connection of a guest into a socket
  of its Lima host agent on the host, and makes the host's loopback reachable as `host.lima.internal`; a firewall inside the
  VM is worthless (the session is root there). Each work VM therefore runs under its own slot account
  (`<prefix>wvm00` … `wvmNN`, created at installation), so the host packet filter can tell work VMs apart: pf rules
  per account (`user <uid>`) allow exactly one loopback connection target (the slot's own pinned Lima SSH port, which
  leads into the slot's own VM) and TCP/UDP out on the **Cloudflare client's tunnel interface** (except special and
  private ranges and port 25), and refuse everything else.
- **Egress watcher** (root LaunchDaemon, root-owned script, no session code): while `warp-cli status` reports
  connected and the route of the VMs' resolver goes through a `utunN`, it loads the anchor with the pass rule on that
  interface, otherwise the fail-closed anchor (addendum 3). DNS of the VMs goes to Cloudflare's public resolvers
  through the same tunnel; the host resolver is not used.
- SSH over vsock; Lima's host-agent proxy on the pinned port is the only loopback target of a slot account.
- No mounts, no port forwards, `plain`-style template (no guest agent port forwarding), no SSH agent,
  `propagateProxyEnv: false`, no host resolver.
- Fail-closed: outside `TEST_MODE` work VMs start only when the slot accounts exist, pf is enabled with the
  `com.apple/000.agent.workvm` anchor exactly as rendered (with or without the egress rule), and the main ruleset
  evaluates `com.apple/*` before the Cloudflare client's anchor and every `pass quick`; checked before every start and
  every minute; without the client the VMs have no internet, and running work VMs are stopped if the host filter is
  gone. The watcher stops every process of the slot accounts within one round (2 s) when the hook is missing or our
  anchor is not evaluated first. It never repairs the main ruleset itself: that would need a reload of the main
  ruleset, which the client owns. If a component replaces the main ruleset for good without Apple's lines, the work
  VMs stay off; the operator sees it in `agent-runner workvm status` (`not_hooked`) and in the sessions' "not ready"
  answers.
- **The main ruleset is never loaded** (operations finding 1, decisions #22): the Cloudflare client hooks its anchor
  into the running main ruleset, so the work VM rules live in Apple's wildcard anchor (`com.apple/000.agent.workvm`,
  loaded alone) and `/etc/pf.conf` stays untouched. Rejected: an own `anchor` line in `/etc/pf.conf` plus
  `pfctl -f /etc/pf.conf` (drops the client's anchor; the client may in turn drop ours, fail-open); appending our line
  to the running main ruleset (would need a reload of the whole main ruleset, same problem).
- The runner reads VM output only as bounded bytes; archives from the VM are re-packed by the runner before they
  reach a guest import.

## Consequences
- The session can now install, build, test, run services and use a browser; the harness tells it so and forbids
  saying it could not execute something.
- Residual risk (recorded honestly): whatever the session puts into the VM (checked-out code, content of prompts,
  files of the workspace, credentials it chooses to use there) can leave the VM to the internet. Untrusted code with
  internet access runs there by design. The mitigation is that no secret of the platform, the device or another
  tenant is ever placed there by the platform, and that the traffic leaves through Cloudflare, not from the site.
- The work VMs' traffic carries the device's Cloudflare identity (the enrolled service token). Cloudflare Access
  policies of the organization must therefore never admit "any enrolled device" or that token to internal
  applications; private network routes through the organization's tunnels are blocked by the host filter (private and
  CGNAT ranges), but an Access policy on a public hostname would not be (operator check, report).
- The host packet filter, the slot accounts and the egress watcher are new installation steps
  (`agent-runner workvm install`, root) next to the Cloudflare client, which the operator installs and enrolls; a
  device without them offers no work VMs (`workvm_unavailable`).
- Work VMs count against the same RAM as workgroup VMs; a busy device answers with a queue position instead of an
  error.
- Pull requests still need a commit through `github.commit`: changes made in the VM must be synced back first.

## Alternatives considered
- **Execution in the session sandbox itself** (allow Bash in the session): breaks ADR-008's two layers, puts
  untrusted processes next to the session token, the gateway and the harness, and the internet into the workgroup
  VM shared by other sessions. Rejected.
- **Claude Code's own Bash tool redirected into the VM** (a forwarding shell via `CLAUDE_CODE_SHELL`): Claude Code
  snapshots the shell, writes the working directory to local temporary files and expects them back; the Landlock
  launcher would have to allow a second executable in the sandbox. Fragile against Claude Code updates and weakens
  the sandbox. Rejected in favour of MCP tools with a Bash-like contract.
- **Firewall inside the work VM**: the session is root there and can remove it. Rejected as the boundary (kept
  nowhere, to not pretend).
- **One shared account for all work VMs**: pf cannot tell the VMs apart; Lima's own SSH needs a loopback connection
  that would then also open other VMs' SSH proxies to every VM. Rejected for per-slot accounts.
- **Gateway VM with a Cloudflare Mesh node** (the first design): Cloudflare does not document shared egress for a
  subnet forwarded by a Mesh node and the dedicated address needs an Enterprise plan; replaced by the client on the
  device (addendum 3). A paid plan or add-on is not pursued at all ([E-10](../KONZEPT.md#e-10), free plans
  only). **A connector in every work VM**: the session could switch it off and read its key.
  **Egress through Cloudflare Tunnel (cloudflared)**: exits with the site's address. Rejected.
- **Direct internet with the site address** (also as a fallback): refused by the client.
- **Subagents sharing the task VM**: the shared `/workspace` would mix the workspaces of parent and subagent.
  Rejected (decision 7).
- **Shared work VM per workgroup**: sessions of different tasks (and owners) would see each other's processes.
  Rejected.
- **Containers on the workgroup VM**: not isolated from the session sandbox's VM, and rootless containers are blocked
  by Ubuntu's user-namespace restriction (ADR-008). Rejected.

## Deviation from [KONZEPT](../KONZEPT.md#k-titel) and DESIGN
[KONZEPT §10](../KONZEPT.md#k-10) ("darf keinerlei Programme … selbst ausführen", "keine beliebige Shell-Ausführung") is superseded for the
work VM by the client's decision above; inside the session sandbox it still holds. [KONZEPT §9](../KONZEPT.md#k-9) (no git by Claude Code)
holds for GitHub: the VM has no GitHub credential; `git` inside the VM is a local tool. Recorded in
`spec-deviations.md`.

[KONZEPT §11](../KONZEPT.md#k-11) ("jede … Operation erhält einen eigenen Datenbankeintrag"): commands in the work VM are deliberately
**not** operations – the client's decision 11 ("keine Einschränkungen") rules out a formal check, approval or own
operation row per command. What §11 and idea 27 want from a record is kept outside the VM all the same: every command
is one `workvm.run` record (command, exit code, start, end, duration, output tail of 64 KiB and total size), written
when it starts and when it ends, also at teardown and after a failure (`lost`), stored encrypted in the task's log and
attributed to task and session (decision 9); the request and the release of the VM are operations with their own row
and audit entry (decision 2). Not kept: output beyond the last 64 KiB (it stays readable with `workvm_output` while
the VM lives). A separate `test.run` operation with a throw-away VM per run ([KONZEPT idea 27](../KONZEPT.md#k-idee-27), AUD-04 W2) is a later
package; the work VM stays free beside it.

[KONZEPT ideas 27](../KONZEPT.md#k-idee-27)/28 against the original package text ("lifetime = task"): the concept is binding (addendum 2,
AUD-04 W2). A work VM is torn down after use (release, idle) and at the latest when the task is closed – closing a
task stops its sessions, and the VM of a task goes when no session of the task is active (`session_ended`) – also
after errors (start failure, failed boundary check, restart). It is never kept for a later task.
