# SCN-07 decisions (agent repository: M2e scenarios E2E-15/E2E-17)

The scenarios themselves live in agent-core (`e2e/platform/specs/e2e-15-audit.spec.ts`,
`e2e-17-monitoring.spec.ts`, decisions in agent-core `docs/platform/decisions/SCN-07.md`). This repository gets one
component fix that E2E-17 needs.

1. **Offline mode of the monitoring test stack (`deploy/monitoring/test-stack/`).** E2E runs are hermetic
   (07-testing.md §5.2): the orchestrator's egress sampler watches every Lima host agent of a run, and all guest TCP
   traffic of a vz VM leaves through it. The MON-01 stack downloaded its three archives from inside the VM, and a
   plain Ubuntu 24.04 cloud image contacts Canonical at boot (measured on the runner device: an HTTPS connection to
   the pollinate entropy server within the first five seconds, besides NTP). Both would be egress violations in
   E2E-17. Fix: `fetch.sh DIR` downloads the pinned archives once on the host, before the run (like the shared
   golden cache that `golden-cache.sh ensure` builds before every E2E run); `up.sh --archives DIR` mounts DIR
   read-only, `provision.sh` copies the archives from there through the new `archive.sh` (one implementation of the
   cache check for host and VM, never a download in offline mode) and a Lima boot script, enabled by the parameter
   `AGENT_MONITORING_OFFLINE=1`, drops every packet the guest opens itself except loopback, name lookups and NTP
   from the first boot on (cloud-init `bootcmd`, before `network-online.target`); answers on connections the host
   opened (Lima's SSH, port forwards) pass. No address literal is needed (the repository allows none but loopback and
   documentation networks). `check.sh` fails when the drop is missing (fail closed). Measured on the runner device:
   create, boot, provision and three further minutes with only NTP and DNS leaving the host agent.
   Without `--archives` the stack behaves exactly as before (the manual MON-01 flow).
2. **Firewall rather than masking services.** Masking pollinate, snapd, motd-news, apt and fwupd timers would have
   to track every image update; one output policy is independent of the image's services and also covers the
   Prometheus/Grafana binaries (Grafana's update checks are already disabled by `provision.sh`).
3. **Base image.** `limactl create` uses Lima's cached `template:ubuntu-24.04` image; if the cache is empty Lima
   downloads it during the run. That download runs under the run's egress sampler (a descendant of the route's
   command) and fails the run visibly; the runner device has the image cached since MON-01.
