# Platform documentation (agent.dfx.swiss)

Entry page for the platform packages built on the integration branch `ad5f2026-agent-platform` of both
repositories: the hub, the web client, the contracts and the E2E suite in **agent-core**; the runner, the guest agent,
the golden image, the test doubles and the monitoring assets in **agent**. The legacy code and its documentation
(`DESIGN.md`, `*-PROTOCOL.md`, the top-level README) stay unchanged; this directory is the platform's own
documentation and the same in both repositories except for `decisions/`, `acceptance/` and the result files of each
repository.

**Read first: [KONZEPT.md](KONZEPT.md)** – the concept (47 ideas) and the binding rules of the client, in German.
It is the first source for everyone, human or AI agent, working on agent.dfx.swiss; specification, ADRs and code
must not contradict it, and every new rule of the client is entered there first.

| Document | Content |
|---|---|
| [KONZEPT.md](KONZEPT.md) | Concept and binding rules: the full concept with anchors per idea and bullet (part A), the client's rules E-01 … with date and wording (B), interpretations until the client decides (C), maintenance and open deviations (D) |
| [PLATFORM-DESIGN.md](PLATFORM-DESIGN.md) | Design overview: principles, components, trust boundaries, data flows, the DESIGN.md sections it replaces, coexistence, known limits |
| [spec/](spec/00-overview.md) | Implementation specification 00–10 (overview, requirements, data model, API, crypto, runner, web, testing, monitoring, milestones, work packages) |
| [spec-deviations.md](spec-deviations.md) | As-built register: where the implementation refines or deviates from the specification, with the deciding package |
| [adr/](adr/README.md) | Architecture decision records 001–043 |
| [decisions/](decisions/README.md) | Small decisions per work package of this repository |
| [acceptance/](acceptance/) | Acceptance evidence per package and milestone of this repository |
| [runbooks/](runbooks/README.md) | Operator runbooks: root key, device certification, golden image, Claude Code upgrade gate, runner, hub, monitoring patch, export/restore, local development and tests |
| [manual-checks/](manual-checks/README.md) | PRF device matrix and manual browser checks per release |
| [CUTOVER.md](CUTOVER.md) | Cutover plan to production (plan only, not executed): roles, owner prerequisites, RP ID, steps with checks and rollback, retirement of the legacy hub |

## Quick start for developers

See [runbooks/local-development-and-tests.md](runbooks/local-development-and-tests.md): set up a `.venv` with
`pip install -e ".[platform-test]"`, point `AGENT_HUB_TEST_POSTGRES` (agent-core) or `AGENT_RUNNER_TEST_PG` (agent)
to a disposable PostgreSQL, and run `make platform-ci`. The specification is binding; where code and specification
differ, the register [spec-deviations.md](spec-deviations.md) says which one holds.

## Rules for these documents

- Both repositories are public: no host names, internal addresses, accounts or secrets; devices are named by role
  (hub device, runner device, second device). `tests/platform/test_hygiene.py` scans this directory.
- Every relative link resolves and every ADR, runbook and manual check is listed in its index
  (`tests/platform/test_docs.py`).
- `spec/` and `adr/` are kept identical in both repositories; a change is made in both in the same work package.
