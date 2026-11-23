# KOR-30 decisions

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
