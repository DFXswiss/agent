# HOT-33 – contract changes

ADR-027 section 3. Additive only. Decisions: `docs/platform/decisions/HOT-33.md`.

- `enums/operation-error-codes.json` (and `common.json#/$defs/operation_error_code`): `repo_read_only` - the GitHub
  identity may only read the repository (roles read, triage) and no fork of it can be used (the repository does not
  allow forks and the identity has none, GitHub refused to create one, or the connection may not write the fork).
  Never held; `repo_not_accessible` stays for a repository the connection does not reach.
- `ops/objects.json`: the results of `github.create_branch`, `github.commit`, `github.push` and
  `github.create_pull_request` take an optional `fork` (`repo`): the fork of the acting identity the write went to
  (for a pull request: the fork its head branch is in). Absent when the repository itself was written.
