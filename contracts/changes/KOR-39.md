# KOR-39 – situation view: attention categories per task (ADR-064)

Additive contract changes (ADR-027 section 3). Every task gets one attention record (`03-api.md` section 4.6.0)
that the hub derives from task, session and operation metadata; the web situation view groups the tasks of an
organization by it (Needs your decision, Stuck, Running, Done).

- `enums/attention.json` (data): the four categories with their list order (`since_asc` for `needs_decision`, longest
  waiting first; `since_desc` otherwise) and their closed reason codes (ADR-064 section 1). A later package may add
  reasons; a client shows an unknown reason with the generic text of its category.
- `common.json`: `attention_category` (the four categories), `attention_reason` (code text
  `^[a-z][a-z0-9_]{0,63}$`, deliberately not an enum so that added reasons stay readable by older clients),
  `q_limit_attention` (query text 1–100).
- `api/objects.json`: `attention_row` `{task_id, category, reason, ref_kind, ref_id, since, seq, subagents_total,
  subagents_active, updated_at}` (`ref_kind` `operation`, `session` or null; `seq` ≥ 1 grows with every change) and
  `attention_item` `{task, attention}`.
- `api/INDEX.json` (section `4.6`, owner KOR-39, `tests/platform/contracts/spec/endpoints-kor39.json`):
  - `GET /v1/orgs/{org}/attention` (`orgs-attention`, right `org:member`, read resource `org.attention`): query
    `category` (required), `mine` (`0`/`1`), `since`, `before`, `limit` (1–100, default 50) →
    `{items: [attention_item] (≤ 100), next_before}`.
  - `GET /v1/orgs/{org}/attention/summary` (`orgs-attention-summary`, right `org:member`, read resource
    `org.attention.summary`): query `since` → `{needs_decision, needs_decision_mine, blocked, working, done_since}`.
- `enums/audit-actions.json` and `audit/`: `read.org.attention`, `read.org.attention.summary` (empty detail).
- Stream frame `attention` of `GET /v1/stream` (`ws/sse/attention.json`, `ws/sse/objects.json` `data_attention` and
  `frame_attention`, `ws/INDEX.json`): the changed `attention_row`, for browsers whose user reads the task's
  workgroup. Clients of before KOR-39 ignore an unknown frame event.
- `api/objects.json` `notification_row`: kind `task.attention` (to the task owner when the record enters
  `needs_decision` or `blocked`, `03-api.md` section 4.9) with `ref` `<category>:<reason>:<seq>` or
  `<category>:<reason>:wg` (workgroup-wide cause); `org_id`, `wg_id` and `task_id` are set.
- Operation types `human.question` and `human.approval` (`03-api.md` section 9, ADR-064 section 3): params
  `{question}` (1–4000 characters), result `{}`, no dedupe fields (`ops/INDEX.json`, `ops/objects.json`,
  `ops/human.*.{params,result}.json`, the `operation.params` body union of `events/objects.json`). A main session asks
  a person; the task waits in `needs_decision/question` or `needs_decision/approval` until the next prompt answers.
  Runners of before KOR-39 reject the types as `unknown_type`.

Fixtures cover every new schema and the changed unions (frame, notification, operation params, notification list).
