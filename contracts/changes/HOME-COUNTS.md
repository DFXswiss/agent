# HOME-COUNTS – start page: the overview of the open tasks

Additive contract changes (ADR-027 section 3). The start page shows three numbers over the open tasks the user reads:
"In progress", "Waiting for a person" and "No agent". The hub reads them from the attention records (KOR-39,
`agent_hub.attention.derive.OVERVIEW`); there is no second state logic:

| overview   | attention records                         |
|------------|-------------------------------------------|
| `working`  | category `working`                        |
| `waiting`  | category `needs_decision`                 |
| `no_agent` | category `blocked`, and `done/idle`       |

A closed task (`done/closed`) is not open and counts nowhere; every other task counts in exactly one number.

- `api/orgs-attention-summary.response.json`: the counts `open_working`, `open_waiting` and `open_no_agent` (required,
  over the readable workgroups like the other counters).
- `api/orgs-attention.request.json`: the query `overview` (`working`, `waiting`, `no_agent`) instead of `category`
  lists the open tasks of one overview; exactly one of `category` and `overview` is required. `waiting` is ordered like
  `needs_decision` (`since` ascending), the others `since` descending; the cursor `before` continues in that order.

Clients of before this change ignore the added counts; an older hub answers `request_invalid` (`/category`) for a list
with `overview`.
