# HOT-09 – contract changes

ADR-027 section 3 (additive).

## `ops/objects.json#/$defs/result_github_list_issues`

- `per_page` and `page` count issues: pull requests are left out and no longer leave a page short or empty while
  issues follow (description changed).
- New optional `note` (string, at most 500 characters): present when the flood limit of the runner ended the reading
  before the page was complete (`truncated` is then `true` and `next_page` `null`); it says how to reach the issues
  beyond it.
