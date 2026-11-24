"""The two state files of an installed runner package (ADR-074 sections 3 and 6).

- `state.json` (0644, public): what the device's owner and `agent-platform device add|remove` may read - version,
  enrollment, device ID, daemon state, golden state, update state. No secret, no content of a task.
- `maintenance.json` (0600, root): the maintenance daemon's memory - the owner the runner runs as, the
  directories created below the owner's home, the previous version, a pending update, failed attempts and bad
  versions, when a version was first seen, the last feed check.

Both are written atomically (temporary file in the same directory, `os.replace`).
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

STATE_VERSION = 1


def write_json(path: Path, document: dict[str, Any], mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        os.fchmod(handle.fileno(), mode)
        json.dump(document, handle, sort_keys=True, indent=1)
        handle.write("\n")
    os.replace(temporary, path)


def read_json(path: Path) -> dict[str, Any] | None:
    """The JSON object in `path`; None when it is missing, unreadable or no object."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return document if isinstance(document, dict) else None


@dataclass
class Maintenance:
    previous: str | None = None
    owner: str | None = None
    created: list[str] = field(default_factory=list)
    pending: dict[str, Any] | None = None
    attempts: dict[str, int] = field(default_factory=dict)
    bad: list[str] = field(default_factory=list)
    first_seen: dict[str, str] = field(default_factory=dict)
    last_check: str | None = None
    feed: str = "unchecked"

    @classmethod
    def load(cls, path: Path) -> Maintenance:
        found = read_json(path) or {}
        known = {name: found[name] for name in cls.__dataclass_fields__ if name in found}
        loaded = cls(**known)
        valid = (
            (loaded.previous is None or isinstance(loaded.previous, str))
            and (loaded.owner is None or isinstance(loaded.owner, str))
            and isinstance(loaded.created, list)
            and all(isinstance(item, str) for item in loaded.created)
            and (loaded.pending is None or isinstance(loaded.pending, dict))
            and isinstance(loaded.attempts, dict)
            and isinstance(loaded.bad, list)
            and isinstance(loaded.first_seen, dict)
            and (loaded.last_check is None or isinstance(loaded.last_check, str))
            and isinstance(loaded.feed, str)
        )
        return loaded if valid else cls()

    def save(self, path: Path) -> None:
        write_json(path, asdict(self), 0o600)


def write_state(path: Path, fields: dict[str, Any]) -> None:
    write_json(path, {"v": STATE_VERSION, **fields}, 0o644)
