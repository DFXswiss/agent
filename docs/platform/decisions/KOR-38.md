# KOR-38 – decisions

Central concept file in the repository ([`../KONZEPT.md`](../KONZEPT.md), rule E-23).

1. **Place: `docs/platform/KONZEPT.md`, not the repository root.** The legacy guard
   (`tests/platform/test_legacy_untouched.py`) refuses every added or changed file outside the platform areas, so a
   root `KONZEPT.md` and pointers from the top-level `README.md` and `AGENTS.md` are not allowed without widening
   that guard. The file sits at the top of `docs/platform/`, outside the parts shared with agent-core
   (`test_docs.py` `SHARED`), so the merge of the repositories (INF-06) finds no second copy. Pointers: the entry
   page `README.md` ("Read first"), `spec/00-overview.md`, `spec/01-requirements.md` and the agent guide
   `agents.md` (with its byte copy `src/agent_platform/guide.md`).
2. **Part A is the wording of the full concept version, with one marked cut.** Only anchors (`<a id>`), heading
   levels and the server names of §3 bullet 8 changed (replaced by "[…]", explained in part D); a script compared
   the rest line by line with the source. Internal names in quotations of part B are cut the same way.
3. **Anchor scheme.** `k-gp`, `k-N`, `k-N-bM`, `k-13-N`, `k-idee-N`, `k-idee-46-bM`, `k-16-bM`, `e-NN`, `a-N`,
   `d-…`. Anchors are explicit HTML anchors, so they do not change when a heading is reworded.
4. **Requirement references use the numbers of the full version.** The Ref column of `spec/01-requirements.md` was
   written against the short version of 21 November 2026; it now links each row to its anchor, with the map
   short §1–§12 = full §1–§12 (short §10 bullet 5 = full §10 bullet 3 sentence 2, short bullets 6–9 = full 5–8),
   short §13 = idea 26, short §14.N = full §13.N, short §15 = full §14. The map is stated in part A.
5. **ADRs 001–056 keep their bare section numbers.** They are decision records written against the short version;
   rewriting about sixty references in records would change history and widen the difference to agent-core. The
   test resolves their bare numbers through the map; ADRs from 057 on must cite the concept by link to an anchor.
6. **New rows do not use `R-` IDs.** The traceability parser of the E2E suite (agent-core) takes every
   `| R-n.m |` row as a requirement that needs E2E tests in `traceability.json`. Ideas 27–47 (block R-16, IDs
   `K-…`) and the rules (block R-E, IDs `E-NN`) are status rows; the package that builds one adds `R-` rows with
   tests.
7. **Rule selection.** Part B holds the product rules of the client; the development workflow (subscriptions,
   development devices, who merges, schedules) stays out. E-09 and E-10 share one quotation of the client (18:52),
   cut where it continues with the egress (and its fixed address, withdrawn at 20:40). Two rules were added beyond the list of the package text
   because they are product rules of the same evening: E-07 (operator subscriptions) and E-24 (no access layer in
   front of the platform).
8. **Hygiene extended to names written as two words.** `name_findings` also checks each pair of neighbouring words
   written together, so a server name spelled with a blank (as in the concept) is found.
9. **Shared documents only in this repository.** The package changes the shared `README.md`, `spec/00-overview.md`
   and `spec/01-requirements.md` here only (package text: agent only, INF-06 unites the repositories). With a
   checkout of agent-core next to this one, `test_shared_documentation_equals_the_other_repository` reports these
   files; it already failed before this package (`spec/03-api.md`, `spec/06-web.md`).
