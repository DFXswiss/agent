# ADR-032: Runner session extension points and the profile store

Status: Accepted (2026-11-21)

## Context
RUN-04 starts sessions (05-runner.md §6.1), consumes the guest journal (§6.4) and selects the model access (§10.1).
Three later packages need to plug into that code without editing it (ADR-027 §4):
- RUN-05 delivers the verified first prompt of a new main session and reacts to the guest's `prompt_accepted` /
  `prompt_refused` records (`prompt_delivery`);
- RUN-12 adds the access kinds `subscription` and `api_key` to the selection ("strategy registry" in the package
  plan);
- the harness context of a session (§8) needs the confidential names of the org, workgroup, task and task owner,
  which arrive only inside events; the inbox (ADR-039) hands them over once, and the runner schema of RUN-01 has no
  place to keep them, so a session started after a daemon restart would not know them.

## Decision
- **Discovered extension points** (areas in sorted order, `registry.discover`):

  | Module and attribute | Value |
  |---|---|
  | `<area>/session_prompt.py:FIRST_PROMPT` | `first_prompt(context, session) -> InitialPrompt | None`; at most one provider. Without one a session starts without an initial prompt (it waits in `prompt_delivering` for the prompt delivery). |
  | `<area>/session_state.py:OBSERVERS` | sequence of `StateObserver(name, handle)`; `handle(journal, conn, session, body)` sees every guest `session.state` record of a known session after its transition was written, in the journal page's transaction. |
  | `<area>/model_access.py:STRATEGIES` | mapping access kind -> `strategy(request, catalog) -> Choice | None`; a kind at most once; `platform` is built in. |

  The journal-kind handlers themselves use the existing point `<area>/journal.py:HANDLERS` (ADR-027 §4): the
  session area registers `session.*`, `usage` and `guest.error`; operations, queries and pings add theirs.
- **Profile store**: migration `0002_profile_event.sql` adds `runner.profile_event` (kind, org, object, event ID,
  signed `ts`, the author's verified signing key, the sealed event). The session area's inbox consumer keeps the
  latest `org.profile`, `wg.profile`, `member.profile` and `task.meta` per object ("latest" by the signed header `ts`,
  then the event ID, never the arrival order, ADR-039). The plaintext is never stored: a session start opens the
  stored events with the scope keys of the store, only into memory. RUN-08 (platform queries) may read the same
  table.

Note (RUN-12): the access kinds are a closed list and all three are built into `model/selection.py` since RUN-12, so
the extension point `<area>/model_access.py:STRATEGIES` could only ever conflict and was removed
(decisions/RUN-12.md item 3); the other two points are unchanged.

## Consequences
- RUN-05, RUN-08 and RUN-12 add files in their own areas; the session area stays unchanged.
- A later migration takes the next free number (ADR-027 §5); the RUN-01 store and CLI tests count the migration files
  instead of assuming exactly one.
- A name that has not arrived yet is shown as `unknown` in the harness context; the start pulls once when the task
  or org name is missing and never waits for it.

## Alternatives considered
Calling RUN-05 code directly from the start (ownership conflict, import cycle), a hook argument on `SessionService`
set by later packages (needs wiring code in a shared place), storing decrypted names (plaintext at rest, a new
storage-key purpose in the crypto copy), keeping names only in memory (lost on every daemon restart).

## Deviation from DESIGN.md
None beyond ADR-001.
