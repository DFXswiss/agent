# Operator runbooks

Procedures for operating the platform packages (09-milestones.md WP-M3-07). Devices are named by role only: hub
device, runner device, second device. The cutover to production is a separate plan: [CUTOVER.md](../CUTOVER.md).

| Runbook | Covers |
|---|---|
| [platform-root-key.md](platform-root-key.md) | Platform root key: creation offline, safekeeping, operator commands, bootstrap invitations, compromise, loss and re-keying |
| [device-certification.md](device-certification.md) | Platform device enrollment and certification, org device enrollment, decertification and removal |
| [golden-image-release.md](golden-image-release.md) | Building, rolling out and retiring the golden image |
| [claude-code-upgrade-gate.md](claude-code-upgrade-gate.md) | Changing the pinned Claude Code version: attack suite, model-double E2E, PERF-01 |
| [runner-installation.md](runner-installation.md) | Runner installation as LaunchDaemon, configuration, `doctor`, operator markers, failing inbox consumers, uninstall |
| [operator-subscriptions.md](operator-subscriptions.md) | The operator's Claude subscriptions for approved organizations on a platform device: token files, approval, `doctor`, log and metrics (ADR-052) |
| [model-access.md](model-access.md) | Model access with fallback and visible quotas, DFX access on own hardware: release order, placing the provider's management key, checks (ADR-080) |
| [hub-operation.md](hub-operation.md) | Hub process (one per database), configuration, migrations, rate limits behind NAT, audit verification |
| [monitoring-patch-handover.md](monitoring-patch-handover.md) | Rendering and handing over the monitoring patch for the infrastructure repository |
| [export-restore.md](export-restore.md) | Org export and restore, projection rebuild, runner store wipe (DR-01) |
| [local-development-and-tests.md](local-development-and-tests.md) | Setup, `make platform-ci`, `make platform-ci-linux`, `e2e/platform/run.sh` |
| [operator-github.md](operator-github.md) | The operator's GitHub token on a platform device: setup, atomic replacement, approving and withdrawing users |
| [agent-account-takeover.md](agent-account-takeover.md) | Account taken over through a stolen agent key: disconnect all, security activity, operator-assisted recovery (ADR-056) |
| [credit.md](credit.md) | Billing: rollout without stopping anyone, setting credit (command and database), the daily USD/CHF rate, the export for the books, operations check (ADR-062) |
| [own-device-release.md](own-device-release.md) | Release key, build, signing and notarisation, publishing the runner package of own devices, update behaviour on the devices (ADR-074) |
| [work-vms.md](work-vms.md) | Work VMs on a runner device: rehearsal on a test device, installation (`install --plan`), operation, faults (hook, order, egress), removal (ADR-059) |
