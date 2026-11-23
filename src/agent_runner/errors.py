"""Error type of the runner core (docs/platform/spec/05-runner.md section 1.2).

Every failure the CLI reports is a closed error code plus, where useful, the name of the offending item (a
configuration variable, a migration, a task). Neither ever carries a value, a path's content, a token or
database error text, so printing an error can never leak a secret.
"""

from __future__ import annotations

import re

_CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_.:/-]{1,200}$")


class RunnerError(Exception):
    """A runner failure with a closed code and an optional item name (never a value)."""

    def __init__(self, code: str, name: str | None = None) -> None:
        if not _CODE_RE.fullmatch(code):
            raise ValueError("error code must be lower snake case")
        if name is not None and not _NAME_RE.fullmatch(name):
            raise ValueError("error name must be a short identifier")
        self.code = code
        self.name = name
        super().__init__(code if name is None else f"{code} {name}")
