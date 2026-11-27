# HOT-28 – contract changes

ADR-027 section 3 (additive). A continuation the platform writes itself keeps the acting user of the turn it continues
(ADR-070, ADR-081 section 2, ADR-083).

- New audit action `session.continued` (group `session`) in `enums/audit-actions.json` and
  `enums/outbox-audit-actions.json`, with the detail schema `audit/session.continued.json` and its outbox body in
  `outbox/objects.json`: the runner handed a session the continuation prompt it writes itself for a turn it
  interrupted - `cause` `credit` (credit pause, `billing.pause`) or `model` (model pause, ADR-081 sections 4 and 5) -,
  the continuation's `prompt_id` and `triggered_by`, the acting user of the interrupted turn the continuation keeps
  (null when that turn had none). Target: the session.
- An older hub refuses the unknown action as a content refusal of that one item (`request_invalid`); the outbox
  sequence continues.
- Fixtures: `outbox/audit/valid/session-continued.json`, `outbox/audit/valid/session-continued-nobody.json`,
  `outbox/audit/invalid/session-continued-cause.json` and `outbox/audit/invalid/session-continued-without-person.json`.
