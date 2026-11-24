# HOT-09 – contract changes

ADR-027 section 3 (additive).

## `ops/objects.json#/$defs/result_github_list_issues`

- `per_page` and `page` count issues: pull requests are left out and no longer leave a page short or empty while
  issues follow (description changed).
- New optional `note` (string, at most 500 characters): present when the flood limit of the runner ended the reading
  before the page was complete (`truncated` is then `true` and `next_page` `null`; it says how to reach the issues
  beyond it), or when the page did not fit one result and lost its last issues (ask with a smaller `per_page`).

## Guest journal (`guest/journal/INDEX.json`, `record.json`, `session.tools_ready.json`, fixtures)

- New kind `session.tools_ready` (session level) with body `{tools, at}`: the session's agent listed the platform
  tools on `/mcp` for the first time in this run of the agent. The guest delivers the first prompt only after it; the
  runner measures it as the start step `tools_ready` and stores or sends nothing. An older runner ignores the kind.
- `platform.refused`: `method_not_found` has a branch of its own with an optional `method` (a request method of the
  protocol by name, else `other`); a guest before HOT-09 sends no `method`.
- `guest.error` code `tools_unavailable`: the agent did not list the tools within `AGENT_GUEST_TOOLS_SECONDS`; the
  guest starts it again (at most twice) and then fails the start `agent_start_failed`. It reaches the task as
  `session.error` (web text in `guest-error.*.json`).
