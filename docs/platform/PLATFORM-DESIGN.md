# Platform design (agent.dfx.swiss)

Status: binding for the platform packages on the integration branch `ad5f2026-agent-platform` (ADR-001).

## Scope

The platform packages are new code beside the legacy code of both repositories:

| Repository | Platform packages |
|---|---|
| `agent-core` | `src/agent_hub/`, `web/`, `contracts/`, `e2e/platform/`, `tests/platform/`, `docs/platform/` |
| `agent` | `src/agent_runner/`, `src/agent_guest/`, `src/agent_doubles/`, `images/`, `deploy/monitoring/`, `contracts/` (hash-checked copy), `tests/platform/`, `docs/platform/` |

For these packages the implementation specification in [`spec/`](spec/00-overview.md) and the decisions in
[`adr/`](adr/README.md) are the design. Legacy modules (`agent_core`, `agent_cli`) are not modified; the only
allowed read-only imports are listed in [`spec/00-overview.md` §5](spec/00-overview.md#5-repository-layout).

## Relation to DESIGN.md

`DESIGN.md` of the `agent` repository stays unchanged on `develop`. **For the platform packages, this
specification and its ADRs replace the following sections of DESIGN.md:**

| DESIGN.md section | Replaced by |
|---|---|
| §20 Refused: hub as a coding control plane | ADR-002 (hub as coordinator), ADR-001 |
| §24 Team record and key identity, in particular §24.2, §24.3, §24.10 | ADR-002, ADR-003, ADR-004, ADR-001 |
| §25 Central operation, in particular §25.1, §25.3, §25.7, §25.9, §25.10, §25.11, §25.15, §25.18, §25.21 | ADR-002, ADR-003, ADR-004, ADR-016, ADR-001 |
| §3 hub-owned lifecycle and leases | ADR-002 |
| §1, §2 (hub role "full replica"), §6, §9, §11, §13 | ADR-003 (zero-knowledge hub) |
| §5, §7 (GitHub OAuth pairing, device token) | ADR-004 (invitation identity without keys file) |
| §17, §19.4, §23 (terminal input frames, ephemeral terminal bytes) | ADR-016 (stored read-only terminal) |

§19.1 (execution on the device) is kept in spirit. The owner decision to build the platform in these repositories
despite the refusals of DESIGN.md is recorded in the project brief; DESIGN.md's change rule (line 5, §24.10, §25.21)
is met by documenting the deviation here, on the integration branches, in the same series of changes that is
reviewed as draft pull requests before any human merge.

## Coexistence

| Aspect | Legacy | Platform |
|---|---|---|
| Process | `python -m agent_core` (port 8787) | `python -m agent_hub` (default port 8788) |
| Env prefix | `AGENT_CORE_*` | `AGENT_HUB_*`, `AGENT_RUNNER_*`, `AGENT_GUEST_*` |
| Database | SQLite + PostgreSQL schema `record` | PostgreSQL database `agent_hub`, schema `hub` |
| URL space | `/api/…`, `/sync/…`, `/auth/…`, `/pair…`, `/health` | `/v1/…`, `/`, `/assets/…`, `/v1/health` |
| Runner home | `~/.local/share/agent` | `~/.local/share/agent-runner` |
| launchd label | `swiss.dfx.agent` | `swiss.dfx.agent-runner` |
| Tests | `tests/` | `tests/platform/` (own `pytest.ini`, coverage configuration, 100 % gate) |
| CI | existing workflows | `.github/workflows/platform-*.yml` |
| Gates | `pytest` | `make platform-ci` (agent also `make platform-ci-linux`) |

## Documents

- [`spec/`](spec/00-overview.md): implementation specification 00–10.
- [`adr/`](adr/README.md): architecture decisions; new ones take the next free number.
- [`decisions/`](decisions/README.md): small decisions per work package.

References in the specification to workspace documents (project brief, concept, spikes, host notes) point to
material that is not part of this repository.
