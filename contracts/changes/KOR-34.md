# KOR-34 – contract changes

ADR-027 section 3. No schema, entry type, field or wire format changes; only the shared vectors (`vectors/log.json`,
`log-events.json`, `log-rotation.json`, `log-wraps.json`) change, because the derived rule changed (ADR-058: every
active admin reads every workgroup of its org, inherited read; R covers it; catch-up gives inheriting admins every
epoch).

## Not purely additive: `vectors/log.json`, main log

The main log removed admin carol without rotating W1, which carol now reads by inheritance; under the new rule the hub
and every verifier refuse that batch (`rotation_required`). Entries 17–21 are reordered (same count and types:
ungrant, remove, rotate W1, rotate org, rotate W0) so that the chain stays valid; the expected states follow. Every
log that was valid before stays valid for the new verifiers (decisions KOR-34 12).

## New cases

Demotion (R = workgroups read only as admin), self-demotion (pending), promotion (wraps of every held epoch),
explicit → inherited change, admin wraps in both directions (W1/W2), the credential scope (explicit access only) and
catch-up with history for inheriting admins. Hub, runner (copy) and web run the same file.
