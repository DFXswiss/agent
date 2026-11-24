# KOR-30 – decisions

This package changed both former repositories `agent` and `agent-core`, which were unified into `agent` (INF-06). Both notes follow unchanged, their headings one level lower: first the note of the former `agent` repository, then the note of the former `agent-core` repository.

## KOR-30 decisions

1. **Empty means unrestricted, not "absent" vs. "empty".** No settings event and an event with `repositories: []`
   both allow every repository the connection reaches. The settings form cannot distinguish "never saved" from
   "saved without entries" for the user, and an empty list never allowed anything before, so no setup relies on it.
2. **Runner only.** The hub never opens `settings.workgroup` (sealed); the web client only edits and shows the list;
   the guest forwards operations. The evaluation lives in `WorkgroupSettings.allows` (formal check step 3) and in the
   `allowed` flag of `github.list_repositories` (pre-check data, then executor). Both change in the runner; no
   contract changes (the `allowed` field and `repo_not_allowed` stay).
3. **Pre-check data unchanged.** `github.list_repositories` still passes the sorted list (`allowed`); the executor
   treats an empty list as "every repository". An operation pre-checked by the previous runner and executed after the
   update therefore also gets the new behaviour.
4. **ADR number 054**: the next free number on `ad5f2026-live` (the ADR index must have no gaps). Parallel packages that also use 054 (KOR-22) renumber when they land after this one.

## KOR-30 decisions (web client)

1. **Texts only, for the next hub release.** The field keeps its keys (`wg-settings.repositories.*`) and its
   behaviour; label, hint and the empty view now say that without entries every repository of the GitHub
   connection is allowed (ADR-054). The `repo_not_allowed` error text says the workgroup is limited and names both
   ways out (add the repository, or clear the list). Both locales of the client (`de`, `en`) change together.
2. The runner change (agent: decisions/KOR-30.md) works without this release; until it is deployed the deployed client
   still shows "No repository is allowed." for an empty list, which is then only a wrong text.
