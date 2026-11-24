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
| [028](028-runner-sync-extension-points.md) | Runner sync extension points (connect hooks, heartbeat sections) | §6 | no |
| [029](029-m0-measurements-and-live03.md) | M0 measurements of the session start and LIVE-03 (addendum to 009 and 010) | §3, §4 | no |
| [030](030-outbox-receipts-and-handler-chains.md) | Outbox receipts and outbox handler chains | §6, §14.6 | no |
| [031](031-budget-check-extension-point.md) | Budget check of operation registration as an extension point | §11, §13 | no |
| [032](032-runner-session-extension-points.md) | Runner session extension points and the profile store | §6, §8 | no |
| [033](033-runner-prompt-delivery.md) | Runner prompt delivery (store, routing, resume and restarts) | §6 | no |
| [034](034-test-doubles-on-guest-loopback.md) | Test doubles reach the guest on loopback through a reverse SSH tunnel | – (07 §5.2) | no |
| [035](035-credential-notices-and-device-request-chains.md) | Credential-change notices and device-request chains | §4, §5, §9 | no |
| [036](036-test-mode-goldens-for-e2e.md) | E2E runs use TEST_MODE goldens with root for the test hooks | 07 §5.2, §9 | no |
| [037](037-usage-area-outbox-bases-and-daily-spend.md) | Outbox base handlers claimed by other areas; daily platform spend per device | §6, §11 | no |
| [038](038-runner-subagent-control.md) | Runner control of subagent sessions (start, one-shot end, final text, forwarded reads) | §13 | no |
| [039](039-runner-inbox-consumers.md) | Runner inbox consumers (verified events to other areas) | §6 | no |
| [040](040-platform-device-founder-pinning.md) | Platform devices pin org founders on first use | §5 | no |
| [041](041-runner-operation-pipeline.md) | Runner operation pipeline and its extension points | §10, §11 | no |
| [042](042-metric-naming-for-promtool.md) | The VM slot total is exported as `agent_runner_device_vm_slots` | 08 §3, §7 | no |
| [043](043-invitation-proof-at-registration.md) | Registration through an invitation requires a proof of the code (`inv_sig`) | §1 | no |
| [044](044-session-token-peer-binding.md) | Session tokens are bound to the UID of the connecting process (numbered 043 in agent until DOC-01) | 05 §4.4, §4.6; 07 §2.3 | no |
| [045](045-final-attack-gate-hardening.md) | Hardening from the final attack gate: instruction files, empty /run, no executable memfd | 05 §4.2, §4.3, §4.5, §5.3; 07 §2.3 | no |
| [046](046-hub-columns-against-the-visibility-list.md) | Every hub column is classified against the list of what the hub sees | 04 §7; 07 §2.1 | no |
| [047](047-outbox-scope-queues.md) | Scope queues keep an event the runner cannot seal from holding back the device's other items (runner, agent repository) | §6, §14.6 | no |
| [048](048-outbox-push-pacing-start-to-start.md) | Outbox pushes are paced start to start (100 ms); the hub paces each device with a token bucket | §6, §14.6 | no |
| [049](049-capacity-refusal-attempts.md) | A session the device refuses for lack of capacity gets a new attempt of the task, placed away from the refusing device until it reports a free slot | §3, §14.3 | no |
| [050](050-hedged-outbox-push.md) | An outbox push whose answer is late is sent once more on another connection; the first answer counts | §6, §14.6 | no |
| [051](051-login-reads-the-terminal-screen.md) | The subscription login reads the terminal screen of `claude setup-token` (cursor positioning, input field, error while running) | §4 | no |
| [052](052-operator-subscriptions.md) | Operator subscriptions: sessions of approved organizations use the operator's Claude subscriptions on a platform device (runner only, bound to token files) | §4 | no |
| [053](053-operator-github-account.md) | Approved users connect GitHub at once with the operator's token on a platform device; second way beside ADR-015 | §9 | yes (as ADR-015) |
| [054](054-repository-list-optional.md) | The workgroup repository list is optional: without one every repository the GitHub connection reaches is allowed (owner's decision, replaces the default of concept §9) | §9 | no |
| [055](055-registration-without-recovery-code-step.md) | Passkeys must deliver PRF (refused at registration, when added and in a recovery, the registration withdrawn); the registration has no recovery-code step (unset recovery slot) | §5, §14.1 | no |
| [056](056-delegated-agent-access.md) | Delegated agent access: the user's own AI agent pairs with one passkey gesture (loopback handover secret, sealed identity, scoped allow-list) and sets the account up, incl. the transfer of the local Claude Code login | §1, §4, §5, §8 | no |
| [057](057-workspace-imports-are-the-scanner-baseline.md) | Platform imports (`files/import`: checkout, work-VM results) are the workspace scanner's baseline; dependency and build directories are not scanned; bulk passes and changes beyond a per-session window become one `session.file` summary | §10 | no |
| [058](058-admins-read-every-workgroup.md) | Organization admins read every workgroup by inheritance (right and keys, history included; delivered by catch-up, revoked by rotation; credentials and platform sessions keep explicit access); key handling stays out of the user interface | §8, §15 idea 30 | no |
| [059](059-work-vm-on-request.md) | Work VM on request: the session asks for its own isolated Ubuntu VM and may do anything inside it; internet only through the device's Cloudflare client, fail-closed (client's decision) | §3, §9, §10, §11, ideas 27, 28, 44, 46 | no |
| [060](060-direct-admission-of-existing-accounts.md) | Admins admit existing accounts directly (`admit`); identities anchored in the platform log (`user_certify`, invitation identity proof), no credential key without consent, runner capability gate | §1, §5, §8 | no |
| [061](061-one-repository.md) | One repository: agent-core is merged into agent (history kept, no path changes); one distribution with a separate `hub` extra; one coverage gate | §5, §6; 07 §3, §4 | no |
| [062](062-prepaid-credit-and-usage-billing.md) | Usage billing with prepaid credit: DFX tokens and DFX VM runtime charged by a versioned price list in USD, credit per organization set by the operator, controlled stop by suspend, CHF equivalent of every booking for the books (owner's decisions 15, 18, 19) | §15 ideas 45–47, §16 | no |
| [063](063-audit-completeness-and-volume.md) | The audit is complete by construction: the request pipeline settles every write (own row, else `request.write`; refusals `request.refused` after the rollback); reads recorded before the answer with repeats counted (`audit.repeat`); the session's own events summarised per session and scope (`event.append_batch`) | §6, §8, §10 | no |
| [064](064-fail-closed-hooks-and-session-socket-isolation.md) | Hooks fail closed: the gateway forwards agent-loop requests only during a confirmed turn, a failed decision hook ends the session (`hook_unavailable`); sessions cannot bind, listen or autobind sockets; guest errors become `session.error` events | §3, §6, §10 | no |
| [065](065-github-existing-token.md) | Connect GitHub with an existing token (GitHub CLI login, personal access token) checked by the execution device; second way beside ADR-015 | §9 | yes (as ADR-015) |
| [066](066-held-github-operations.md) | A `github.*` operation without a connection is held up to 15 min and the connection is requested in the task's chat | §9, §10 | no |

New decisions get the next free number; a superseded ADR keeps its file and is marked `Superseded by ADR-NNN`.
