# KOR-36 – contract changes

ADR-027 section 3 (additive).

## `api/org-invites-list.response.json`: `opened_at`

Each listed invitation may carry `opened_at`: the time of the first answered `POST /v1/invites/lookup` of the
invitation (taken from the hub's `invite.lookup` audit rows), or null if nobody looked it up yet. It lets the inviting
admin see whether a link was opened at all ("open / opened / registered / accepted"). The hub answers and audits every
lookup already, so the field reveals nothing new. The property is optional (not in `required`): answers of older hubs
stay valid. New fixture `fixtures/api/org-invites-list.response/valid/opened.json`.
