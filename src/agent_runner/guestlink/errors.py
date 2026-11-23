"""Error classes of the guest control client (docs/platform/spec/03-api.md section 7, 05-runner.md section 3.3).

Every failure of a control call is a `GuestError` with a closed code and the operation as name (the `RunnerError`
contract: never a value, never a response body):

| Class | Code | Meaning |
|---|---|---|
| `GuestUnreachable` | `guest_unreachable` | socket missing or refusing, connection broken, or the call timed out |
| `GuestUnauthorized` | `guest_unauthorized` | HTTP 401: the control token does not match the guest's |
| `GuestRefused` | `guest_refused` | any other HTTP error; `error` is the guest's code (`already_assigned`, ...) |
| `GuestProtocolError` | `guest_protocol` | a response that is not the documented JSON shape |

Callers decide what a failure means: three `GuestUnreachable` health probes in a row make a VM unhealthy
(05-runner.md section 3.3); `GuestRefused` with `already_assigned` means the VM belongs to another workgroup.
"""

from __future__ import annotations

import re

from agent_runner.errors import RunnerError

_ERROR_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class GuestError(RunnerError):
    """A failed control call; `operation` names the call, `status` is the HTTP status when there was one."""

    def __init__(self, code: str, operation: str, *, status: int | None = None) -> None:
        super().__init__(code, operation)
        self.operation = operation
        self.status = status


class GuestUnreachable(GuestError):
    def __init__(self, operation: str) -> None:
        super().__init__("guest_unreachable", operation)


class GuestUnauthorized(GuestError):
    def __init__(self, operation: str) -> None:
        super().__init__("guest_unauthorized", operation, status=401)


class GuestRefused(GuestError):
    """An HTTP error answer; `error` is the guest's error code, `unknown` when the body had none."""

    def __init__(self, operation: str, status: int, error: object) -> None:
        super().__init__("guest_refused", operation, status=status)
        self.error = error if isinstance(error, str) and _ERROR_RE.fullmatch(error) else "unknown"


class GuestProtocolError(GuestError):
    def __init__(self, operation: str) -> None:
        super().__init__("guest_protocol", operation)
