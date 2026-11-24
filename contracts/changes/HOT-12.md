# HOT-12 – names for every member (ADR-067)

Additive.

- Event kind `member.name` (org scope, author a user, right `org_admin`, header `wg` empty, `ref` = `subject`: a user of
  the org or a user invitation of its log at `lh`): `enums/event-kinds.json` (new header check `subject`),
  `events/kinds.json` (`device_pull: false` – runners do not pull it), `events/member.name.json`,
  `events/objects.json#/$defs/body_member_name` (`{display_name}`, 1–100 characters), `common.json` kind enums.
- Vectors (`vectors/log-events.json`): positive `member_name`, `member_name_invitation`; negative
  `E5_member_names_a_member` and `header_member_name_ref_{text,device,unknown,device_invitation}`. Hub, runner copy
  and web run them through `check_event`.
- `api/org-invites-list.response.json`: optional `registered_user` (UUID or null; absent on older hubs).
- Audit detail `event.append.kind` accepts `member.name`.
