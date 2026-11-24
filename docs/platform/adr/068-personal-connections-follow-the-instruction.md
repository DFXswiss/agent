# ADR-068: A personal connection follows the person who gave the instruction

Status: Accepted (2026-11-24)

Implements [A15](../KONZEPT.md#a-15) (every instruction carries the verified person who gave it; GitHub actions run
only with identities that person may use), [Idee 36](../KONZEPT.md#k-idee-36) (shared sessions, interventions
attributed to each person) and [E-19](../KONZEPT.md#e-19) (exclude the class, not the case). Amends ADR-015
(operations used the task owner's credential) and ADR-053.

## Context
- A personal connection must never be used on behalf of another person. Two ways allowed it:
  1. The runner ran every `github.*` operation with the connection of the **task owner**
     (`github/ops.py owner_client(context, session.owner_id)`), while every member with write access may write
     into a task of the workgroup (shared sessions, Idee 36). Whoever wrote in another person's task acted with that
     person's GitHub account.
  2. ADR-053 let the operator approve several users for the operator's GitHub token, which stored that token as each
     user's own credential.
- The runner knows who instructs: every prompt is a signed event whose author the inbox verified (signature,
  `wg:write` at `lh`); only users author prompts (hub rule `prompt`: USER). The guest journals, in order,
  `session.state prompt_accepted {prompt_id}` when a prompt starts a turn, then the turn's `operation.request`
  records, then `session.state idle`. Pings never start a turn (they are noticed by hooks during one).

## Decision
- **Acting user.** An operation that needs a personal connection uses the connection of the acting user: the
  verified author of the prompt that started the session's current turn. Never the task owner's as such, never
  anybody else's. The rule is the same for every personal credential kind a session's operations can use; the
  inventory today has one, `github`.
- **Turn.** The runner stores the author of every verified prompt (`prompt_event.author_id`). The `prompt_accepted`
  of a main session sets the turn's acting user when no turn is open; another or an unknown author in the same open
  turn (queued prompts of several people) leaves the turn **without** acting user until the next turn; `idle` closes
  the turn. A child session (`subagent.request`) takes the acting user of the request that started it. A user's own
  agent signs as that user and so counts as that user.
- **Stamp at receipt.** `operation.request` is stamped with the acting user in the journal transaction
  (`operation.actor_id`); the stamp never changes, so a held operation runs, if at all, with the connection of the
  person who asked for it, whoever writes later.
- **One place.** `credentials/acting.py` decides and is the only module that opens a personal credential for an
  operation (`acting.opened`, `acting.cached_version`); `github/ops.py actor_client` builds on it. A structural test
  scans the source for any other opener and for any read of the session owner in the GitHub and operation areas,
  and runs every registered `github.*` pre-check and executor without acting user against the GitHub double (no
  request may reach GitHub).
- **No acting user, no connection.** Such an operation is rejected at once with `github_not_connected` and a fixed
  text; it is not held, since nobody could be asked and its stamp never changes. "Rather one waiting card too many
  than a foreign account."
- **Held for the right person.** The `waiting` card (ADR-066) carries `for_user` = the acting user; the web asks only
  that person to connect and shows everybody else, the task owner too, whom it waits for. A body without
  `for_user` (an older runner) is addressed to the task owner as before.
- **Record.** The `operation.result` event names whose connection an executed operation used (`connection_of`);
  log lines `held` and `no_acting_user` name the operation.
- **Operator's GitHub for exactly one user.** `OPERATOR_GITHUB_USERS` names exactly one user, the operator; more is
  `config_invalid` `OPERATOR_GITHUB_USERS:one_user_only` (the daemon does not start; doctor reports the same error).

## Consequences
- A task in which only its owner writes behaves exactly as before: the owner's prompts make the owner the acting
  user; the stamp is one indexed read per operation request in a transaction that already reads the session.
- Someone who writes in another person's task works with their own GitHub account or, without one, is asked to
  connect it; the owner's account is never used for them.
- Operations journaled before this change and the turns of a wiped store have no acting user and use no connection
  until the next prompt.
- The model access of a session is not a personal connection in this sense: it is chosen at the session's start and
  its consumption is carried by the organisation (A15). Following the instructing person for it as well, and an
  intersection of rights beyond personal connections, stay with KOR-45/KOR-46.
- The harness line still names the GitHub login of the task owner at the session's start (a fact for the agent; no
  token is used).

## Alternatives considered
- Refuse writes of other members in a task: contradicts Idee 36 (shared sessions).
- Decide per operation from the latest prompt delivered at execution time: a held operation would change its
  person when someone else writes meanwhile; the stamp at receipt is exact.
- Hold an operation without acting user: nobody could resolve it; rejecting at once tells the session at once.
