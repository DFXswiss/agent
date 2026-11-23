# ADR-029: M0 measurements of the session start and LIVE-03 (addendum to ADR-009 and ADR-010)

Status: Accepted (2026-11-21)

## Context
ADR-009 left the Claude Code start inside the sandbox and the clone path end to end unmeasured ("the clone path margin
is thin") and asked M0 to measure them; `09-milestones.md` makes a clone path above 9 s a reason to revise the M1 plan.
ADR-010 left open whether a Claude subscription token works through the guest model gateway and whether the usage
endpoint accepts it; LIVE-03 was to check that already in M0 with a read-only current access token. Work package
PRB-01 measured both on the runner device; the evidence is `docs/platform/measurements/M0.md` of the agent repository with raw data.

## Decision
- **ADR-009 addendum – Go for M1 without plan revision.** Clone path from `ensure_vm` to `prompt_accepted`
  (runner VM code, real Lima, real guest, model double): p95 6.32 s, max 6.32 s (10 runs); warm pool p95 0.56 s.
  Steps against `05-runner.md` §6.1: VM ready 5.89 s (≤ 6 s), sandbox + credential 0.05 s (≤ 0.3 s), TUI up and prompt
  accepted 0.38 s (≤ 2 s + 0.5 s). The boot (`limactl start` with the token parameter) is 92 % of the clone path.
  Claude Code paints its TUI 0.24 s after `systemd-run` under Landlock (median), Landlock adds about 0.01 s. The
  hub round trips and the credential choice are not part of these numbers and have 3.7 s left within the 10 s.
- **ADR-010 addendum – LIVE-03 passed.** With the current OAuth access token of a subscription profile (read-only),
  the gateway injection `Authorization: Bearer` + `anthropic-beta: oauth-2025-04-20` against `https://api.anthropic.com`
  answered a Haiku-class turn of Claude Code inside the sandbox with HTTP 200, and `GET /api/oauth/usage` accepted the
  same token (HTTP 200, windows `five_hour` and `seven_day` with `utilization` and `resets_at`). Profile files and
  keychain attributes were unchanged, the artefacts held no token. Without a hub in M0 the token went to the guest
  directly instead of through a test org (07-testing.md §2.4); the gateway path is the same. Quota polling stays feature-detected because a
  long-lived setup token (the credential that `claude.login` stores) was not tested: creating one needs a login, which
  ADR-011 rules out for live tests.
- **Observation for the session start**: Claude Code 2.1.330 sent no `SessionStart` HTTP hook before the prompt (only
  `UserPromptSubmit`, then `Stop`). The `prompt_delivering` transition (TUI up) is therefore taken from the transcript or
  the pane, or the session moves from `agent_starting` directly to `working` when the prompt was on the command line.

## Consequences
- M1 keeps the plan of ADR-009 (warm pool, clone fallback, control token as Lima parameter, argv prompt); PERF-01/02
  in M1 measure with the hub included. If they show less margin, the levers are Lima's readiness polling, cloud-init's
  per-clone rerun and the pool size (M0.md).
- RUN-12/SCN-05 may rely on the subscription injection; the usage parser can expect `five_hour`/`seven_day` objects
  for access tokens and must still treat 401/403 as `quota_unavailable`.
- The probes in `scripts/probes/` (agent repository) stay as the measurement method for SCN-01/PFM-01 and LVT-01.

## Alternatives considered
Measuring only inside the golden build (no clone, no control path), measuring through the hub (not available in M0),
creating a setup token for LIVE-03 (needs a login; forbidden).

## Deviation from DESIGN.md
None beyond ADR-001.
