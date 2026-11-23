# KOR-13 – contract changes

ADR-027 section 3 (defect fix of a description text; no schema change).

## `api/device-outbox.request.json`: description of the push pacing

The description named the old limit "at most one push per 250 ms". KOR-13 (ADR-048) replaces it: runners start a push
at most every 100 ms (start to start) and the hub paces the pushes of each device with a token bucket of one push per
100 ms and a reserve of 10 pushes (`429 rate_limited`, `Retry-After: 1` beyond it). The text now says so. The schema
itself is unchanged (the item and size limits stay; pacing is a hub check, decisions CTR-03 item 8), so every fixture
stays valid or invalid for its stated reason.
