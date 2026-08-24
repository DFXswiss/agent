# Contributing

- Branch from `develop`. Never push to `develop` or `main`.
- Open a draft pull request. A human merges.
- Sign commits with the GitHub identity that owns the commits.
- Public repository: English for commits, pull requests, and comments.
- Do not name private repositories, internal hostnames, or internal infrastructure.
- Add or update tests in the same change.
- Tests exist to find and document product defects. A green suite is not the goal. Every product defect found while testing belongs in the target repo’s tracker (`BUGS.md` unless that repo names another file) and in a test that asserts the *correct* behaviour, marked expected-fail until the product is fixed. Do not encode broken behaviour as a passing assertion. A defect that exists only in chat is not found.
- Run `pytest` before you push. Tests need PostgreSQL (`AGENT_TEST_PG` or a local `initdb`).

## Pull request text

Four sentences of summary, then details if needed.
