"""The state document every double serves at ``GET /_double/state``.

Common keys: ``double`` (its name), ``failures`` (readable texts, one per violated expectation or unexpected
request), ``requests`` (requests since the last reset or scenario switch), ``done`` (every expected request
arrived) and ``ok`` (``done`` and no failure). A double adds its own keys (the model double: ``scenario``,
``consumed``, ``remaining``).
"""

from __future__ import annotations

from typing import Any


def state_document(double: str, *, done: bool, failures: list[str], requests: int, **details: Any) -> dict[str, Any]:
    """The state document; ``ok`` holds only when everything expected happened and nothing unexpected did."""
    return {
        "double": double,
        **details,
        "failures": list(failures),
        "requests": requests,
        "done": done,
        "ok": done and not failures,
    }
