# Platform documentation (agent.dfx.swiss)

Entry page for the platform packages of the repository `DFXswiss/agent`: the hub, the web client, the contracts, the
E2E suite, the runner, the guest agent, the golden image, the test doubles, the monitoring assets and the agent tool.
They were built in two repositories (hub, web, contracts and E2E in the former **agent-core**, the rest in **agent**);
agent-core was merged into agent with its complete history and without path changes
([ADR-061](adr/061-one-repository.md)). Documents written before the union still say "agent-core" or "agent" for
the former repository; the path is the same in the united one. The legacy code and its documentation (`DESIGN.md`,
`*-PROTOCOL.md`, the legacy parts of the top-level README) stay unchanged; this directory is the platform's own
documentation.

**Read first: [KONZEPT.md](KONZEPT.md)** – the concept (47 ideas) and the binding rules of the client, in German.
It is the first source for everyone, human or AI agent, working on agent.dfx.swiss; specification, ADRs and code
must not contradict it, and every new rule of the client is entered there first.

| Document | Content |
|---|---|
| [KONZEPT.md](KONZEPT.md) | Concept and binding rules: the full concept with anchors per idea and bullet (part A), the client's rules E-01 … with date and wording (B), interpretations until the client decides (C), maintenance and open deviations (D) |
| [PLATFORM-DESIGN.md](PLATFORM-DESIGN.md) | Design overview: principles, components, trust boundaries, data flows, the DESIGN.md sections it replaces, coexistence, known limits |
| [spec/](spec/00-overview.md) | Implementation specification 00–10 (overview, requirements, data model, API, crypto, runner, web, testing, monitoring, milestones, work packages) |
| [spec-deviations.md](spec-deviations.md) | As-built register: where the implementation refines or deviates from the specification, with the deciding package |
| [adr/](adr/README.md) | Architecture decision records |
| [decisions/](decisions/README.md) | Small decisions per work package (one file per package; packages that changed both former repositories have both notes in it) |
| [acceptance/](acceptance/) | Acceptance evidence per package and milestone |
| [runbooks/](runbooks/README.md) | Operator runbooks: root key, device certification, golden image, Claude Code upgrade gate, runner, hub, monitoring patch, export/restore, local development and tests |
| [manual-checks/](manual-checks/README.md) | PRF device matrix and manual browser checks per release |
| [CUTOVER.md](CUTOVER.md) | Cutover plan to production (plan only, not executed): roles, owner prerequisites, RP ID, steps with checks and rollback, retirement of the legacy hub |

## Quick start for developers

See [runbooks/local-development-and-tests.md](runbooks/local-development-and-tests.md): set up a `.venv` with
`pip install -e ".[platform,hub,platform-test]"`, point `AGENT_HUB_TEST_POSTGRES` (hub tests) and
`AGENT_RUNNER_TEST_PG` (runner tests) to a disposable PostgreSQL, and run `make platform-ci`. The specification is binding; where code and specification
differ, the register [spec-deviations.md](spec-deviations.md) says which one holds.

## Rules for these documents

- Both repositories are public: no host names, internal addresses, accounts or secrets; devices are named by role
  (hub device, runner device, second device). `tests/platform/test_hygiene.py` scans this directory.
- Every relative link resolves and every ADR, runbook and manual check is listed in its index
  (`tests/platform/test_docs.py`).
- `spec/` and `adr/` are kept identical in both repositories; a change is made in both in the same work package.
