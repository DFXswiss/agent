"""Context checkpoints of main sessions and pull-request links (ADR-059, 03-api.md sections 4.6 and 6.1).

The hub stores the encrypted checkpoints of a task (`hub.context_object`, `hub.context_chunk`) and the opaque
pull-request tags (`hub.pr_link`) without any key: `store` holds the rules, `routes` the device and browser API,
`outbox` records the restore outcome a runner reports on the session row, `jobs` removes stale uploads and
`vectors` writes the shared test vectors of `agent_hub.crypto.context`.
"""
