# Architecture decision records

All ADRs are accepted as of 2026-11-21 and binding together with `docs/spec/`.

| # | Title | KONZEPT | Deviates from DESIGN.md |
|---|---|---|---|
| [001](001-platform-packages-and-coexistence.md) | Platform packages beside the legacy code | – (PROJECT.md) | yes (procedural: line 5, §24.10, §25.21) |
| [002](002-hub-as-coordinator.md) | The hub coordinates work | Grundprinzip, §2, §3, §11, §13 | yes (§3, §20, §24.3, §25.1, §25.7, §25.18) |
| [003](003-zero-knowledge-hub.md) | Zero-knowledge hub | §5 | yes (§1, §2, §6, §9, §11, §13, §24.2, §25.9, §25.11) |
| [004](004-invitation-identity-without-keys-file.md) | Identity by invitation code, passkey and membership log | §1, §2 | yes (§5, §7, §24.2, §25.3, §25.15) |
| [005](005-invitation-code-lifecycle.md) | Invitation code lifecycle | §1 proposals | no |
| [006](006-passkey-prf-key-management.md) | Passkey-based key management | §5, §14.1 | no |
| [007](007-devices-receive-org-key.md) | Execution devices receive the org key | §8, §12 | no |
| [008](008-session-isolation-in-shared-vm.md) | Session isolation inside the workgroup VM | §3, §10, §14.2 | no |
| [009](009-vm-start-under-ten-seconds.md) | VM start below ten seconds | §3, §14.3 | no |
| [010](010-model-access-and-gateway.md) | Model access and model gateway | §4, §6, §14.4 | no |
| [011](011-claude-subscription-login.md) | Claude subscription login | §4 | no |
| [012](012-operations-contracts-and-idempotency.md) | Operations, formal checks and idempotency | §10, §11, §14.5 | partly (§19.3 kept in spirit) |
| [013](013-device-assignment-and-offline-sync.md) | Device assignment, scheduling and offline sync | §3, §6, §7, §14.6 | yes (via ADR-002) |
| [014](014-pull-request-model-deferred.md) | Pull-request data model deferred | §11, §14.7 | no |
| [015](015-github-app-device-flow-rest-only.md) | GitHub App, device flow, REST only | §9 | yes (§19.1, github-accounts) |
| [016](016-stored-read-only-terminal.md) | Stored, read-only terminal view | §6, §13 | yes (§17, §19.4, §23, §25.9, §25.10) |
| [017](017-subagent-limits.md) | Limits for controlled subagents | §13 | no |
| [018](018-monitoring-reuse.md) | Monitoring reuses the existing stack | §7 | no |
| [019](019-web-stack-and-csp.md) | Web stack, CSP and reproducible build | §5, §6 | no |
| [020](020-notifications-in-app-and-session-pings.md) | In-app notifications and content-free session pings | §12 | no |
| [021](021-single-agent-software-v1.md) | Claude Code as the only agent software in v1 | §4 | no |
| [022](022-runner-local-stores.md) | Runner and guest local stores | §6 | no |
| [023](023-platform-root-key.md) | Offline platform root key | §1, §3 | no (beyond 004) |
| [024](024-session-read-scope.md) | Read scope of sessions | §8 | no |
| [025](025-interactive-tui-with-gateway-metrics.md) | Interactive TUI with gateway-based metrics | §6 | no |
| [026](026-platform-device-entitlement.md) | Entitlement to platform devices | §1, §3, §4 | no (beyond 002/004) |
| [027](027-work-package-conventions.md) | Work-package conventions (parallel implementation) | – (PROJECT.md) | no (beyond 001) |

New decisions get the next free number; a superseded ADR keeps its file and is marked `Superseded by ADR-NNN`.
