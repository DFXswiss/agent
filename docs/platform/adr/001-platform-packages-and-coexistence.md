# ADR-001: Platform packages beside the legacy code

Status: Accepted (2026-11-21)

## Context
- `PROJECT.md` decision 1: the platform is built in the existing repositories (`agent-core` for hub and web, `agent` for the runner) on integration branches; legacy code stays untouched until cutover.
- `agent-core` today is a single-process hub (SQLite + PostgreSQL Team Record, GitHub OAuth, plaintext replica). `agent` is a CLI/daemon with a local PostgreSQL store. Both are governed by `agent/DESIGN.md`, which refuses several core ideas of KONZEPT (see ADR-002, 003, 004, 016).
- DESIGN.md says drifting implementation loses "until a pull request changes it" (line 5); §24.10 and §25.21 require changes by pull request with an explicit human decision.

## Decision
- New packages: `agent-core`: `src/agent_hub/`, `web/`, `contracts/`, `e2e/platform/`, `tests/platform/`; `agent`: `src/agent_runner/`, `src/agent_guest/`, `src/agent_doubles/`, `images/`, `deploy/monitoring/`, `contracts/` (hash-checked copy), `tests/platform/`.
- Integration branch `ad5f2026-agent-platform` in both repositories, branched from `develop`; work packages branch from it as `<8-hex id>-<slug>` and open draft PRs against it. No merge to `develop` in this work.
- Legacy modules are never modified. Read-only imports are limited to the list in `00-overview.md` §5; everything else is copied and then counts as new code.
- Separation: own process (`python -m agent_hub`, port 8788), own database `agent_hub` (schema `hub`), env prefixes `AGENT_HUB_*`, `AGENT_RUNNER_*`, `AGENT_GUEST_*`, runner home `~/.local/share/agent-runner`, launchd label `swiss.dfx.agent-runner`, URL space `/v1/`, separate test tree, coverage configuration and CI workflows (`platform-*.yml`).
- `docs/platform/PLATFORM-DESIGN.md` (both repos) states that for the platform packages this specification and its ADRs replace DESIGN.md §20, §24, §25 and the further sections listed in ADR-002, ADR-003, ADR-004 and ADR-016. The owner decision is recorded in `PROJECT.md`; DESIGN.md's change rule is met by documenting the deviation in the same PR series.

## Consequences
- Legacy CI, deployment and users are unaffected; both systems can run side by side on one machine.
- Some helpers exist twice (copied) until cutover; copies name their origin in the module docstring.
- Cutover (M4) is a separate, owner-approved step.

## Alternatives considered
- Extending the legacy hub in place: blocked by the plaintext replica design, SQLite store and in-memory fan-out; would break production behaviour.
- New repositories: rejected by the owner (`PROJECT.md`).

## Deviation from DESIGN.md
Yes – procedural: DESIGN.md is not amended on `develop`; the deviation is recorded in `PLATFORM-DESIGN.md` on the integration branches (line 5, §24.10, §25.21).
