# HUB-10 – contract changes

## Audit action `outbox.rejected` (additive, defect fix)

`03-api.md` section 6.1 requires that an outbox item rejected for content reasons is "acknowledged with
`status:"rejected"` and logged", and the package asks for an audit row. The closed list of audit actions had no action
for it: the item-specific actions (`event.append`, `session.<state>`, ...) describe successful effects and do not exist
for every item kind (`vm_state`, `usage`, `quota`, `metrics`, `wrap`).

- `enums/audit-actions.json`: new action `outbox.rejected` (group `outbox`).
- `audit/outbox.rejected.json`: detail `{origin_seq, item_kind, code}` (the `origin_seq` of the item, its
  `item_kind` of `enums/outbox-item-kinds.json`, the hub error code that `results[].error` carries). Metadata only.
  Fixtures `fixtures/audit/outbox.rejected/{valid,invalid}`.

The hub writes the row with actor and device = the pushing device, target `outbox_item`/`<origin_seq>`, result
`rejected`. Runners never send this action (it is not in `enums/outbox-audit-actions.json`).
