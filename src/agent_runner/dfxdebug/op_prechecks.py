"""Pre-check of `dfx.debug` (`<area>/op_prechecks.py:PRECHECKS`; ADR-102 section 3).

A query whose task owner did not release a debug sign-in to the session's workgroup, or that someone else than the
task owner instructed, is rejected before it is registered as accepted (`access.signin_for`: `rights_denied`,
`not_found`, `hub_unreachable`) - nothing reaches the DFX API. The executor checks again (the release may end in
between).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_runner.dfxdebug import access
from agent_runner.operations.extensions import PrecheckRequest, PrecheckResult


async def debug_check(request: PrecheckRequest) -> PrecheckResult:
    await access.signin_for(request.context, request.operation, request.session)
    return PrecheckResult()


PRECHECKS: dict[str, Callable[..., Any]] = {"dfx.debug": debug_check}
