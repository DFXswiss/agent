
## Hub

1. Migration is `0011_session_context.sql`, not 0013: `agent_hub.db.migrate` refuses gaps, so 0013 cannot migrate on
   this branch. It is renumbered to the next free number on rebase (its content does not depend on the number).
2. New error code `quota_exceeded` (413) with the additive error detail `quota {scope: object|org, limit, used}`.
3. Hub-side audit `session.context_saved {chunks, size, pruned}` on commit (actor device); the runner sends
   `session.context_restored {object_id}` / `session.context_fallback {reason}` (they need `session_id`).
4. `session_row.context_restore`/`context_reason` and the device-state `context` are optional in the contracts
   (additive, old consumers unaffected); the hub always sends the two session fields.
5. The metric `agent_hub_session_context_total{result,reason}`: the hub has no Prometheus exposition, so it is a log line
   `metric agent_hub_session_context_total result=… reason=…` plus a process counter; the audit rows are the durable count.
6. An upload expires 6 h after its last chunk (not after its creation), so a slow large upload is not cut off.
7. Subagent sessions cannot upload (`400 request_invalid` at `/session_id`).
8. A pull-request tag stays linked to the first task; the same tag from another task is `409 id_conflict`.
9. `hub.pr_link` is the only table whose name matches the "no pull-request table" guards (ADR-014); the
   tests allow it explicitly because it holds only an opaque HMAC tag.
10. Review 1 decision 6: a chunk refused for a quota writes the hub audit row `session.context_refused {reason,
    scope, limit, used, requested}` (actor the device, result `denied`) in a transaction of its own after the refused
    one was rolled back; every refusal is one row (a runner keeps its local copy and does not retry a `413`).
11. Review 1 decision 10: "newest" is (`session.created_at` at the hub, object ID) descending in the list of a task,
    the device-state `context` and the pruning; `completed_at` and device clocks no longer order anything. A late
    commit of an earlier session is kept until the next commit of the task prunes it (never the one just committed).
12. `session.context_restored` carries `workspace` (`full|partial|omitted|empty`) and `stale`; the outbox handler maps
    `partial`/`omitted` to `context_reason` `workspace_partial`/`workspace_omitted` (closed list
    `common.json#/$defs/context_reason`; the constraint `session_context_reason` allows them only with `restored`).
    The audit detail checker gained `boolean` leaves for `stale`.
13. The web consumes `contracts/vectors/context.json` (`web/src/core/crypto/context.ts`): the browser holds the
    workgroup key and can compute the pull-request tag for `GET /v1/workgroups/{wg}/pr-links/{epoch}/{tag}`.
14. ADR-058 links the concept with absolute URLs into the agent repository: `KONZEPT.md` exists only there until the
    repositories are merged (INF-06), and the ADR must resolve in both checkouts.
