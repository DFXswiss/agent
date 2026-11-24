# Threat model: work VM on request (ADR-059, spec 11)

## Assets
1. Credentials of the platform and of tenants: model tokens and subscription tokens, GitHub tokens, device signing and
   scope keys, hub keys, the runner's PostgreSQL, the guest control tokens of workgroup VMs.
2. Plaintext of other tasks and organizations (workgroup VMs, runner store, other work VMs).
3. The integrity of the session sandbox: harness, hooks, gateway and platform tools of every session.
4. The execution device itself and the site's internal network.
5. Third parties on the internet (abuse from the VM).

## Attacker
Anything running in a work VM as root: the session's own commands, code from the internet the session installs,
code of the checked-out repository, a prompt injection from a web page the session reads. The model itself may be
manipulated. The attacker fully controls the VM's kernel, network stack, Lima guest agent and `workvm-job` helper.

## Boundary and controls
| Threat | Control | Verified by |
|---|---|---|
| VM reaches the host's loopback services (`host.lima.internal` = the host's 127.0.0.1 in Lima's user network): runner PostgreSQL, forwarded guest sockets, local daemons, other slots' SSH proxies | One macOS account per slot; pf allows the slot account exactly one loopback target (its own pinned SSH port, which leads into its own VM) and refuses every other loopback connection | `firewall render` unit tests; ATK WVM-01 (real VM) |
| VM reaches the host through its own public/LAN/IPv6 addresses (macOS delivers them over lo0) | Address-independent `block … on lo0 user U` (macOS routes the host's own addresses over lo0); the only pass to the internet is on the tunnel interface | WVM-02 |
| VM reaches the LAN, other VMs, metadata services | Blocked table (RFC 1918, CGNAT, link-local, multicast, ULA, documentation, reserved); everything not on the tunnel interface is refused (the client's default split-tunnel excludes send private ranges to the physical interface, where nothing passes) | WVM-03, WVM-04 |
| VM uses gvisor-tap-vsock services in the host agent (the user-network gateway, `/services/forwarder/expose`) to open host ports | Template without port forwarding and guest agent forwards; host agent runs as the slot account, so an exposed port would be the slot's own; ATK case checks that port 80 of the user-network gateway and host address gives nothing | WVM-09 |
| VM reaches a workgroup VM or the session sandbox | Workgroup VMs expose nothing but host unix sockets of the runner account (not readable by slot accounts) and loopback (refused); the session sandbox only accepts 127.0.0.1:7700 inside its own VM | WVM-05 |
| VM traffic leaves with the site's address | The only egress of a slot account is `pass out on utunN` (the Cloudflare client's tunnel), loaded by the root watcher only while the client is connected; client down = routes on the physical interface = blocked at once; DNS to Cloudflare's resolvers through the tunnel, no host resolver | WVM-10, `images/workvm/egress-check.sh` (device), watcher tests |
| Credentials of the platform in the VM | None placed: no Lima `param`, no cloud-init secret, no proxy variables (`propagateProxyEnv: false`), no env passed to jobs except what the session gives; the runner's SSH access uses the slot's Lima key on the host (0700 home); GitHub stays with the host-side executors. Credentials the session itself chooses to use in its VM are its business (addendum 1): no scanning, no monitoring | WVM-06 (search for platform-placed credentials) |
| Attack on the runner through answers of the VM (`workvm-job` output, `limactl` output) | Bounded reads (header 4 KiB, body ≤ limit), closed header schema, output only ever returned as text to the session | unit tests of `workvm.jobs` |
| Attack on a shared workgroup guest through a crafted archive from the VM (`from_vm`) | The runner parses the VM's archive on the host in a bounded way and re-packs one canonical tar of regular files and directories; the guest import checks again (no links, no specials, no `..`, no protected names, size limits) | unit tests of `workvm.sync` |
| Prompt injection into the session from VM output or synced files | Same class as reading any untrusted content; protected instruction files never enter a workspace; the session sandbox stays closed whatever the session decides | existing ATK-34, ATK-40 |
| Another task's session uses this VM | VM bound to `task_id` (scope task) or `session_id` (scope session); every call resolves the VM from the session of the journal that carried it (the runner also checks that the session runs in that workgroup VM) | `test_a_session_of_another_task_never_reaches_the_task_vm` |
| Request abuse: many or large VMs | Size cap, limits per task/org/owner, RAM budget, queue with time-out; organization RAM share and disk admission **not built yet** (decisions #17) | runner tests, WVM-08 |
| Host disk exhaustion (sparse VM disks, Lima logs) | Work VM homes on an own APFS volume with a quota (built); allocated disk in admission and log truncation **not built yet** (decisions #20) | WVM-07 |
| VM traffic reaches internal resources through the device's Cloudflare identity (private routes of the organization's tunnels, Access policies admitting enrolled devices) | Private and CGNAT ranges (incl. Cloudflare's private hostname range) are in the blocked table; Access policies must not admit the device token or "any enrolled device" (operator check) | WVM-03, report |
| Abuse towards third parties from Cloudflare's addresses | Port 25 refused (host filter); per-slot state and connection-rate caps; no bandwidth shaping yet (decisions #20); no content inspection, no destination logging (addendum 1) | firewall tests |
| Data exfiltration of what the session put into the VM | **Not preventable by design** (free internet is required); leaves through Cloudflare | documented (ADR-059) |
| Escape from the VM to the host (hypervisor bug) | Out of scope of the platform; Apple Virtualization framework; a host-agent compromise yields only that slot account (no other slot's disk, no runner secret); since HOT-18 `_config` belongs to the slot account (Lima writes it at start), so such a compromise can also leave an `override.yaml` for that slot's later work VMs | — |

## Residual risks
- **Accepted risk (decision of the operator, 2026-11-24, decisions #35): the host filter matches a slot by `user`,
  and pf does not attribute every packet to its socket's account.** The rehearsal reached the device's listening
  ports over loopback and over the site address from a slot account, intermittently, in every client state. A
  program in a work VM can reach, with single connections, the device's loopback services, the site network and,
  while the client is disconnected, the internet with the site's address. Work VMs run on this filter; a boundary by
  interface is left to a later package.
- ICMP is not matched by pf's `user`, the anchor has no device-wide rules (decisions #22), and the Cloudflare client
  passes ICMP to the private networks. The boundary for ICMP is therefore Lima's user network: gvisor-tap-vsock does
  not forward a VM's ICMP out of its virtual network (it may answer echo requests itself). `egress-check.sh --vm`
  (`vm_lan_icmp`) and WVM-03 verify this on every device and Lima release; should a release forward ICMP, a ping
  sweep of the site network would become possible (TCP and UDP stay blocked) and the device needs an ICMP rule.
  An attacker who escapes the VM into the slot account's host agent (out of scope above, but the account is all they
  get) can send ICMP from that account to the site network: pf cannot attribute ICMP to an account and the client
  passes it. Accepted: ICMP carries no service access; a device-wide ICMP rule would touch the device's own traffic
  (decisions #29). `egress-check.sh` reports this path (`lan_icmp_slot_socket`, `info`).
- The hook is Apple's `anchor "com.apple/*"` line in the running main ruleset. A component that replaces the main
  ruleset without it opens nothing for long: the watcher stops every slot process within one round (2 s) and the
  runner tears the VMs down; in that window a slot account's new connections fall through to the client's rules.
- When the client disables pf (disconnect, uninstall), the slot accounts are unfiltered until the watcher's next
  round enables it again (at most 2 s); without the tunnel their traffic then goes nowhere useful, but the site
  network is reachable in that window.
- Data the session places into the VM may leave the VM (by design).
- Lima's own SSH needs one loopback connection per slot (its pinned port); a bug in Lima's host-agent proxy on that
  port affects only that slot.
- Cloudflare sees the destinations of the traffic (it is the egress provider); our side logs none.
- The pass rule names the tunnel interface; if the client's interface changed while another `utun` (another VPN)
  took the old name, slot traffic routed there would pass until the watcher's next round (5 s). The device runs no
  other VPN for work VM traffic (the cloudscale relay's endpoint is excluded from the tunnel and reached directly).
- That the client's tunnel carries host-originated sockets of other accounts and that `warp-cli status` reads as
  expected must be verified on each client and macOS release (`images/workvm/egress-check.sh` is part of the
  installation check).
- The VM can reach every public address, including public addresses of the operator's own services; they must
  authenticate like for any internet client.
