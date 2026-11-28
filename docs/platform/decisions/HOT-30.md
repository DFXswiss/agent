# HOT-30 – Decisions

A task could not change the title or the text of its own pull request: `github.update_issue` refuses a pull request
(`The number is a pull request, not an issue.`), and no other type wrote to a pull request. Every review round that
asked for a better description waited for a person.

1. **One type for the writes a pull request needs: `github.update_pull_request`** with `title`, `body`, `draft` and
   `labels`, at least one of them (`params_invalid` otherwise). The name follows `github.create_pull_request`,
   `github.get_pull_request` and `github.list_pull_requests`. The other writes of the usual flow were checked:
   - comment on a pull request: `github.comment` already serves issues and pull requests (KOR-25), unchanged;
   - draft to ready and back: new, `draft` of this type;
   - labels: new, `labels` of this type (GitHub keeps a pull request's labels on its issue: `PATCH
     /repos/{r}/issues/{n} {labels}`, the list replaces every label);
   - merge and approve stay out: the parameters are a closed object without `state`, `merge`, `merge_method`, `event`
     or `head_sha`, and the executor sends no `PUT …/merge`, no review and no ref update. Merging stays
     `github.merge` (class `merge`, its own rights profile, ADR-094); approving is no operation.
2. **Same rules as every `github.*` type.** Formal checks (closed parameters, the workgroup's repository list:
   `repo_not_allowed`), identity resolution and pre-check `identity.resolved(precheck.repo_access)` (ADR-078:
   `repo_not_accessible`, `github_not_connected`), registration at the hub (audit), class `write` (a `development`
   profile may use it, `read_only`, `review` and `merge` may not). No new error code.
3. **Repository, not authorship.** The type applies to pull requests of the repositories the task may use, as the
   package asks; it does not check who opened the pull request (the repository list and the identity's access are the
   boundary, as for `github.comment`).
4. **No dedupe fields.** Setting a title, a text, a draft state or labels again changes nothing further, like
   `github.update_issue` (KOR-25 #9); a repetition within a turn is held off by the idempotency key as for every type.
5. **Draft state through GraphQL.** GitHub's REST API ignores `draft` on `PATCH …/pulls/{n}`; only the GraphQL
   mutations `convertPullRequestToDraft` and `markPullRequestReadyForReview` change it. The client gets one method
   `graphql` (same headers, rate gate and refusals; GraphQL `errors` become `not_found` for `NOT_FOUND`, `forbidden`
   for `FORBIDDEN`, `unprocessable` otherwise) and the endpoint `<api>/graphql`, or `<host>/api/graphql` for a GitHub
   Enterprise Server API URL `<host>/api/v3`. The mutation is sent only when the state differs from the one read
   first. This is the runner's only GraphQL request (spec-deviations, 03 §9).
6. **The result is read after the change** (`GET …/pulls/{n}`): `{number, url, title, state, draft, labels}`, so the
   session sees what GitHub holds, also when GitHub ignored part of a request.
7. **GitHub double:** `PATCH …/pulls/{number}` (`title`, `body`, `state`; a client that sends `draft` there is a
   failure, since GitHub ignores it) and `POST /graphql` with the two mutations (node of the pull request, write right
   of pull requests checked like a REST write; a closed pull request is `UNPROCESSABLE`; any other query is a failure).
8. **Guest 0.8.0.** The guest checks operation types against its bundled contracts, so the new type needs a new golden
   (as KOR-74 for `mail.find`); an older guest refuses it locally as `unknown_type`. The harness text
   (`images/harness/RULES.md`) names the type with an example.
