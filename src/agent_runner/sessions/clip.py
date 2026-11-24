"""Clipping of tool and output records beyond the event bound (ADR-086 section 3; 04-crypto.md section 6.4 E8).

The guest cuts `session.tool` fields (`input`, `result_summary`) and the `session.output` text at 64 Ki characters,
but the runner encrypts the **escaped** body JSON: a control character becomes six bytes (`\\u001b`), so a record can
exceed the ciphertext bound of 256 KiB and was skipped with a fact line. `clip(kind, body)` returns such a body
clipped field by field instead: the largest clippable field first, cut to the longest prefix that lets the body fit,
followed by the marker `[clipped: <n> more characters]` (a field that cannot make the body fit becomes its marker when
that is shorter, and the next field is clipped); an output also gets `truncated: true`. A body that fits, a
kind without clippable fields, or a body that is no JSON object of strings is returned unchanged (the seal checks then
decide as before). The full content stays in the session's transcript in the guest.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.event import MAX_CT_BYTES

AEAD_TAG_BYTES = 16
MAX_BODY = MAX_CT_BYTES - AEAD_TAG_BYTES
FIELDS: Mapping[str, tuple[str, ...]] = {
    "session.tool": ("input", "result_summary", "input_summary"),
    "session.output": ("text",),
}
MARKER = "\n[clipped: {n} more characters]"


def _size(body: Mapping[str, Any]) -> int | None:
    try:
        return len(body_json(body))
    except CryptoError:
        return None


def _marked(value: str, keep: int) -> str:
    return value[:keep] + MARKER.format(n=len(value) - keep)


def _longest(fits: Callable[[int], bool], high: int) -> int | None:
    """The largest `keep` in `0..high` with `fits(keep)` (monotonic), None if not even 0 fits."""
    if not fits(0):
        return None
    low = 0
    while low < high:
        middle = (low + high + 1) // 2
        if fits(middle):
            low = middle
        else:
            high = middle - 1
    return low


def clip(kind: str, body: Mapping[str, Any], limit: int = MAX_BODY) -> Mapping[str, Any]:
    """`body`, or a copy whose clippable fields are cut so that its body JSON is at most `limit` bytes."""
    names = FIELDS.get(kind)
    size = _size(body)
    if names is None or size is None or size <= limit:
        return body
    out = dict(body)
    if kind == "session.output":
        out["truncated"] = True
    texts = [name for name in names if isinstance(out.get(name), str)]
    for name in sorted(texts, key=lambda name: -len(body_json(out[name]))):
        value: str = out[name]

        def fits(keep: int, name: str = name, value: str = value) -> bool:
            return len(body_json({**out, name: _marked(value, keep)})) <= limit

        # a clipped field is never longer than before: the contract bounds the length of every field
        keep = _longest(fits, max(0, len(value) - len(MARKER.format(n=len(value)))))
        if keep is not None:
            out[name] = _marked(value, keep)
            return out
        marker = _marked(value, 0)
        if len(body_json(marker)) < len(body_json(value)):
            out[name] = marker
    return out
