"""Organizations: genesis, membership-log appends, rights projections and the public rights API of the hub.

03-api.md section 4.3, 04-crypto.md sections 4.7, 5.3, 5.10, 6.1, 02-data-model.md sections 1.3 and 2.2. The rules
of the log itself (L1-L8, the rotate set R, W1-W3) are the reference implementation `agent_hub.log`; this package
applies them to the hub's tables, keeps the projections (`org_member`, `wg`, `wg_access`, `wg_device`, `scope_epoch`,
the head and `rotation_pending` of `org`) in the same transaction, and answers the rights questions of every other hub
area (`agent_hub.orgs.rights`).
"""
