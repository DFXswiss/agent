"""Copied from agent-core src/agent_hub/crypto/dedupe.py at 0346281bb510.

Dedupe tags (04-crypto.md section 4.10, new; field sets of 03-api.md section 9).

`tag = b64u(HMAC-SHA256(dk_e, P || "dedupe\\0" || canonical({"type": type, "fields": fields})))` with
`dk_e = HKDF(WK_e, info=P||"dedupe")`. `dedupe_fields` builds the normalised field set of one operation type so that
the runner (Python) and the web client (TypeScript) produce the same tag for the same operation:

| Type | Fields (normalised) |
|---|---|
| `github.checkout` | `repo`, `ref` (resolved commit SHA, lowercase), `session` |
| `github.create_branch` | `repo`, `branch` |
| `github.commit` | `repo`, `branch`, `parent_sha`, `tree_sha` |
| `github.push` | `repo`, `branch`, `commit_sha` |
| `github.create_pull_request` | `repo`, `head`, `base` |
| `github.comment` | `repo`, `number`, `body` = sha256_hex(UTF-8 body) |
| `subagent.request` | `session` (parent), `prompt` = sha256_hex(UTF-8 purpose || 0x00 || UTF-8 prompt) |

Repository names are lowercased, branch names lose a leading `refs/heads/`, SHAs are lowercased. A branch or ref
name that is not printable ASCII is replaced by `sha256:<hex>` of its UTF-8 bytes (canonical JSON is ASCII-only).
With an idempotency key the field set is `{"idempotency_key": key, "task": task_id}` instead.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping

from cryptography.hazmat.primitives import hashes, hmac

from agent_runner.crypto import fields as field_checks
from agent_runner.crypto.encoding import P, b64u_encode, canonical, is_printable_ascii, require_bytes, require_int
from agent_runner.crypto.errors import fail
from agent_runner.crypto.kdf import KEY_BYTES, LABEL_DEDUPE

_HEADS = "refs/heads/"
_UNICODE_RE = re.compile(r"[^\ud800-\udfff]*")


def sha256_hex(text: str) -> str:
    """Lowercase hex SHA-256 of the UTF-8 text (text that is not valid Unicode is `header_invalid`)."""
    if not _UNICODE_RE.fullmatch(text):
        fail("header_invalid")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _text(params: Mapping[str, object], name: str) -> str:
    value = params.get(name)
    if not isinstance(value, str) or not value:
        fail("header_invalid")
    return value


def _ascii(value: str) -> str:
    return value if is_printable_ascii(value) else "sha256:" + sha256_hex(value)


def _repo(params: Mapping[str, object]) -> str:
    return _ascii(_text(params, "repo")).lower()


def _branch(params: Mapping[str, object], name: str) -> str:
    return _ascii(_text(params, name).removeprefix(_HEADS))


def _sha(params: Mapping[str, object], name: str) -> str:
    return _ascii(_text(params, name)).lower()


def _session(params: Mapping[str, object]) -> str:
    return field_checks.uuid(params.get("session"))


_BUILDERS: dict[str, Callable[[Mapping[str, object]], dict[str, object]]] = {
    "github.checkout": lambda p: {"repo": _repo(p), "ref": _sha(p, "ref"), "session": _session(p)},
    "github.create_branch": lambda p: {"repo": _repo(p), "branch": _branch(p, "branch")},
    "github.commit": lambda p: {
        "repo": _repo(p),
        "branch": _branch(p, "branch"),
        "parent_sha": _sha(p, "parent_sha"),
        "tree_sha": _sha(p, "tree_sha"),
    },
    "github.push": lambda p: {"repo": _repo(p), "branch": _branch(p, "branch"), "commit_sha": _sha(p, "commit_sha")},
    "github.create_pull_request": lambda p: {
        "repo": _repo(p),
        "head": _branch(p, "head"),
        "base": _branch(p, "base"),
    },
    "github.comment": lambda p: {
        "repo": _repo(p),
        "number": require_int(p.get("number"), 1),
        "body": sha256_hex(_text(p, "body")),
    },
    "subagent.request": lambda p: {
        "session": _session(p),
        "prompt": sha256_hex(_text(p, "purpose") + "\x00" + _text(p, "prompt")),
    },
}
DEDUPE_TYPES = frozenset(_BUILDERS)
"""Operation types with duplicate protection (03-api.md section 9); the others have no dedupe fields."""


def dedupe_fields(op_type: str, params: object) -> dict[str, object]:
    """The normalised field set of `op_type` from its parameters plus `session` (checkout, subagent parent)."""
    builder = _BUILDERS.get(op_type)
    if builder is None or not isinstance(params, Mapping):
        fail("header_invalid")
    return builder(params)


def idempotency_fields(key: str, task_id: str) -> dict[str, object]:
    """`{"idempotency_key": key, "task": task_id}` for callers that pass an idempotency key."""
    if not isinstance(key, str) or not key:
        fail("header_invalid")
    return {"idempotency_key": _ascii(key), "task": field_checks.uuid(task_id)}


def dedupe_text(op_type: str, fields: Mapping[str, object]) -> bytes:
    """`P || "dedupe\\0" || canonical({"type": op_type, "fields": fields})`."""
    return P + LABEL_DEDUPE + canonical({"type": op_type, "fields": dict(fields)})


def dedupe_tag(dk: bytes, op_type: str, fields: Mapping[str, object]) -> str:
    """The b64u HMAC-SHA256 tag the hub compares (it never learns `dk` or the fields)."""
    mac = hmac.HMAC(require_bytes(dk, KEY_BYTES), hashes.SHA256())
    mac.update(dedupe_text(op_type, fields))
    return b64u_encode(mac.finalize())
