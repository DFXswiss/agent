# KOR-26B – contract changes

ADR-027 section 3 (additive).

## `api/workgroup-tasks.request.json`: optional query parameter `order`

`GET /v1/workgroups/{wg}/tasks` accepts `order=created` (the default and the previous behaviour: `created_at DESC,
id DESC`) or `order=activity` (`last_activity_at DESC, id DESC`). The cursor `next_before` keeps its form (b64u of
canonical `{"at", "id"}`) and holds the time of the chosen order. The right stays `wg:read`; the response is
unchanged. The web task overview (all tasks of an organization across the workgroups the user can read) merges the
lists of the workgroups by latest activity and needs each list paged in that order (decisions KOR-26B). Fixtures:
`valid/order-activity.json`, `invalid/unknown-order.json`. An old client never sends the parameter; the web client
is delivered by the same hub, so a new client never meets an old hub.
