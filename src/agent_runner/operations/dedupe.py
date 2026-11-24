"""Dedupe tag of an operation (04-crypto.md section 4.10; 03-api.md section 9; `contracts/ops/dedupe-fields.json`;
ADR-012).

`dedupe_slot(...)` gives the `dedupe` member of the registration (`{epoch, tag}`) or None:

- the type's field set (`crypto.dedupe.dedupe_fields`, normalised: repository lower case, branch names
  without `refs/heads/`, SHAs lower case, texts as `sha256_hex`) built from the effective parameters plus the
  requesting session (`github.checkout` `session`, `subagent.request` parent `session`) and the pre-check's dedupe
  fields (`github.checkout` `ref` = resolved commit SHA, `github.commit` `parent_sha` and `tree_sha`);
- `github.create_issue` (KOR-25): `repo`, `title` and `body` as `sha256_hex` (an issue left without body counts as
  the empty text), so the same issue is not opened twice within the window of `contracts/ops/dedupe-fields.json`;
- types without dedupe fields (`github.list_repositories`, `github.get_pull_request`, the reading and connection types
  of KOR-25, `github.update_issue` - setting values is idempotent -, `task.set_title`) have a slot only with an
  idempotency key: `{"idempotency_key": key, "task": task_id}`, which the hub holds for 24 h.

The content rule of a type with dedupe fields applies whether or not the session passes a key (KOR-28): the key never
replaces the fields, so a new key cannot repeat the same operation. It can only mark a repetition as the same request:
a reuse of the key for other content of the same task and type within the type's window (`WINDOWS`) is refused
locally (`register.build`, `records.key_conflict`).

The key is `dk_e = HKDF(WK_e, "dedupe")` of the workgroup key of the epoch the params event is sealed with (the
current epoch), so the hub compares tags of the same epoch only.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from agent_runner.crypto.dedupe import DEDUPE_TYPES, dedupe_fields, dedupe_tag, idempotency_fields
from agent_runner.crypto.kdf import dedupe_key

SESSION_FIELD_TYPES = frozenset({"github.checkout", "subagent.request"})
WINDOWS: Mapping[str, int] = MappingProxyType(
    {
        "github.checkout": 600,
        "github.create_branch": 86400,
        "github.commit": 86400,
        "github.push": 86400,
        "github.create_pull_request": 604800,
        "github.comment": 86400,
        "github.create_issue": 86400,
        "subagent.request": 600,
    }
)
"""Seconds a type's dedupe slot is held (`contracts/ops/dedupe-fields.json` `window_seconds`)."""


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
    if op_type not in DEDUPE_TYPES:
        return None if idempotency_key is None else idempotency_fields(idempotency_key, task_id)
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
