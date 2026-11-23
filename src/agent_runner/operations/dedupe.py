"""Dedupe tag of an operation (04-crypto.md section 4.10; 03-api.md section 9; `contracts/ops/dedupe-fields.json`;
ADR-012).

`dedupe_slot(...)` gives the `dedupe` member of the registration (`{epoch, tag}`) or None:

- with an idempotency key the fields are `{"idempotency_key": key, "task": task_id}` (every type);
- otherwise the type's field set (`crypto.dedupe.dedupe_fields`, normalised: repository lower case, branch names
  without `refs/heads/`, SHAs lower case, texts as `sha256_hex`) built from the effective parameters plus the
  requesting session (`github.checkout` `session`, `subagent.request` parent `session`) and the pre-check's dedupe
  fields (`github.checkout` `ref` = resolved commit SHA, `github.commit` `parent_sha` and `tree_sha`);
- types without dedupe fields (`github.list_repositories`, `github.get_pull_request`, `task.set_title`) have no slot.

The key is `dk_e = HKDF(WK_e, "dedupe")` of the workgroup key of the epoch the params event is sealed with (the
current epoch), so the hub compares tags of the same epoch only.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_runner.crypto.dedupe import DEDUPE_TYPES, dedupe_fields, dedupe_tag, idempotency_fields
from agent_runner.crypto.kdf import dedupe_key

SESSION_FIELD_TYPES = frozenset({"github.checkout", "subagent.request"})


def fields_for(
    op_type: str,
    params: Mapping[str, Any],
    *,
    session_id: str,
    task_id: str,
    idempotency_key: str | None,
    extra: Mapping[str, object],
) -> dict[str, object] | None:
    """The normalised field set of the operation, None for a type without duplicate protection."""
    if idempotency_key is not None:
        return idempotency_fields(idempotency_key, task_id)
    if op_type not in DEDUPE_TYPES:
        return None
    source: dict[str, object] = {**params, **extra}
    if op_type in SESSION_FIELD_TYPES:
        source["session"] = session_id
    return dedupe_fields(op_type, source)


def dedupe_slot(
    op_type: str,
    params: Mapping[str, Any],
    *,
    session_id: str,
    task_id: str,
    idempotency_key: str | None,
    extra: Mapping[str, object],
    workgroup_key: bytes,
    epoch: int,
) -> dict[str, object] | None:
    fields = fields_for(
        op_type, params, session_id=session_id, task_id=task_id, idempotency_key=idempotency_key, extra=extra
    )
    if fields is None:
        return None
    return {"epoch": epoch, "tag": dedupe_tag(dedupe_key(workgroup_key), op_type, fields)}
