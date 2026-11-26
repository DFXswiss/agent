# KOR-56 – release of a workgroup to DFX support (ADR-096)

Additive; older clients keep working (they never meet the new entries unless an organization releases a workgroup).

- `common.json`: membership-log bodies `log_body_support_grant {wg, access}` and `log_body_support_revoke {wg}`;
  platform role `support`; event kind `support.info`.
- `enums/event-kinds.json`: right `wg_release` and the kind `support.info` (scope `wg`, author `user`); events of a
  support person are ordinary `user` kinds of the workgroup (`prompt`, workgroup whiteboard) checked against the
  support roster of the platform log.
- `events/support.info.json`, `events/objects.json#/$defs/body_support_info`; `events/kinds.json`: `support.info` is
  not delivered to devices (`device_pull` false).
- API (03-api.md section 4.14): `GET /v1/support/releases`, `GET /v1/orgs/{org}/support`,
  `PUT /v1/platform/support/{user}`; `GET /v1/orgs/{org}/log` also for `org:support`.
- Notification kinds `support.release`, `support.access`, `support.people`.
- Audit actions `log.support_grant`, `log.support_revoke`, `support.people`, `support.write`, `support.name`,
  `read.support.releases`, `read.org.support`.
- Vectors: support sections in `log.json`, `log-wraps.json`, `log-rotation.json`, `log-events.json` and negatives in
  `platform-log.json` (the vector platform log carries two more users, `sam` and `tom`, and three support roles).
