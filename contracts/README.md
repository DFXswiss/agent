# contracts (copy)

Copy of `contracts/` of the `agent-core` repository, the single source of truth for every cross-component format
(`docs/platform/spec/07-testing.md` section 4, ADR-027 section 3). **Never edit files here by hand.** The copy is
updated by `scripts/sync-contracts.sh <agent-core checkout>` (`make sync-contracts AGENT_CORE=…`); the merge of
an agent-core change that touches `contracts/` syncs it automatically.

`VERSION` is the SHA-256 over the lines `<sha256 of the file>  <path>\n` of every file here except `VERSION`,
sorted by POSIX path; `tests/platform/test_contracts_version.py` recomputes it, and the end-to-end orchestrator
refuses to start when both checkouts differ (CON-04).
