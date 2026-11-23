# ADR-024: Read scope of sessions (KONZEPT §8)

Status: Accepted (2026-11-21)

## Context
KONZEPT §8: AI sessions may read and discover via platform APIs everything they are authorised for in the context of their assigned rights: members, workgroups, tasks, status and whiteboards; no blanket insight into foreign workgroups; read access is logged. Devices hold keys only for assigned workgroups (+ org key, ADR-007); a VM serves exactly one workgroup.

## Decision
- A session's rights are those of the **task owner**, evaluated at call time, intersected with the session's workgroup.
- Readable via MCP (`03-api.md` §8.1): own organization (workgroup **names**, members, org whiteboard), own workgroup (tasks, chat messages, workgroup and task whiteboards, operations, statuses). Other workgroups: names only, even if the owner could read them.
- Every MCP read is journaled, sent as `platform.read` event and as a `read.session.<tool>` audit row. Update
  (KOR-03): a read whose arguments the tool refused or that failed inside the tool has the audit row only, no
  `platform.read` event (`03-api.md` §8).

## Addendum: default workgroup
KONZEPT §8 says members have write access in the default workgroup "standardmässig". Decided for v1: this right is **not reducible** (no `ungrant` on the default workgroup); a member who must not write leaves the org. Reason: the default workgroup is the zero-configuration entry point of every member and its key distribution relies on "all members".

## Consequences
- No cross-workgroup data leaves a workgroup VM; behaviour is deterministic.
- A task that needs data from another workgroup must be created in that workgroup.

## Alternatives considered
Full owner rights across workgroups (needs other workgroups' keys on the device and in the VM), task-only scope (contradicts "discover" in §8).

## Deviation from DESIGN.md
None.
