# ADR-066: A GitHub operation without a connection is held and the connection is requested in the chat

Status: Accepted (2026-11-23)

## Context
- The owner's first real task ("look at the newest issue in <org>/<repo>") ended with an explanation of what the
  session could not do. GitHub was not connected; the session never asked for an operation.
- The owner decided (2026-11-23): in such a case the only correct answer is to start the GitHub login - detected
  deterministically, without explanation, without a question, and without the user repeating the task (PROJECT.md
  decision 10 c; as a pattern for other missing preconditions).

## Decision
- The runner, not the model, detects it: when the formal check or the pre-check of a `github.*` operation fails only
  with `github_not_connected`, the operation is held locally before registration (state `received`, at most 15 min)
  instead of being rejected. The runner writes one `session.status` `{state:"waiting",
  waiting_for:"github_connection", operation_id, operation_type, repo?, until}`; it carries only values the platform
  checked, never text of the session.
- The web shows the task owner a card in the task's chat that starts the connection (device flow or existing token,
  ADR-065). When the owner's `github` credential arrives, the held operation goes on by itself (dedupe,
  registration, execution); the session gets the result through `operation.finished` as for any pending operation.
  At `until` it is rejected with `github_not_connected` and audited like every rejection.
- Flood limits: at most 3 held operations per session, at most one `waiting` event per session and reason per 60 s.
- The session knows the state from the start: the context block of the harness names the GitHub connection
  ("connected as <login>" / "not connected") and the rule "request the operation anyway; do not explain what is
  missing, do not offer workarounds". `github.request_connection` starts the connection when no other type fits.
- A repository outside an (optional) workgroup list stays `repo_not_allowed` and is rejected at once: there is no
  approval card (ADR-054, PROJECT.md decision 14).

## Consequences
- A task that needs GitHub works on the first attempt for a user who connects when asked.
- Held operations are invisible to the hub until they are registered; the hub only sees the sealed `session.status`.
  A hold ends after 15 min at the latest, so nothing waits forever.
- An older web client does not know the `waiting` state and shows nothing; the operation then ends after 15 min as
  before. An older runner never holds.

## Alternatives considered
- Leaving the detection to the model (harness text only): not deterministic; the first task showed that it fails.
- Rejecting at once and letting the session retry after the user connected: the user would have to repeat the task.

## Deviation from DESIGN.md
- None.
