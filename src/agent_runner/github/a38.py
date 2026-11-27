"""The A38 check of `github.comment` with `a38_review` (FIX-02; 05-runner.md section 12.1, 03-api.md section 9).

A session that posts a review comment for the A38 guard (`docs/a38.md`) asks for it in one call:
`github.comment {repo, number, body, a38_review: true}`. Before posting, the runner asks the guard's own parser
(`agent_cli.a38_review.check_comment`, shipped with this runner, the parser the pinned guard action runs) whether
the guard would count the comment once it is posted by this account now. Only then is it posted; otherwise the
operation fails `params_invalid` with the guard's reasons and nothing is posted.

- `check(ctx, client, repo, number, body)`: reads the posting account (`GET /user`), then runs `check_comment` in a
  worker thread (the guard's code is synchronous) with `GuardApi`, which answers the guard's `request` and `paginate`
  through the operation's GitHub client on the runner's event loop. Durations of every step from the moment the
  operation reached the runner (`Operation.created_at`, "comment text ready") are kept in `Checked.timings` and the
  result's `a38.timings_ms`, and logged as `runner.github a38_review ...` also when the check refuses.
- A GitHub failure the guard would read as "unavailable" stays a failure of the operation where a retry may succeed
  (`Transient`: network, 5xx, rate limits), so the pipeline retries it instead of reporting a wrong reason.
- An account that cannot be read (an installation token has no `/user`) fails closed: `upstream_error` with
  `NO_ACCOUNT`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agent_cli.a38_errors import GuardError, RateLimitError
from agent_cli.a38_review import CommentCheck, check_comment
from agent_runner.github import ops
from agent_runner.github.client import GitHubClient, GitHubError
from agent_runner.operations.extensions import OperationContext, OperationFailed

CALL_TIMEOUT_S = 120.0
"""Bound of one guard request through the runner's client (the client has its own shorter timeout)."""
MAX_PAGES = 30
"""The guard reads at most 3000 changed files (`readme_only.GITHUB_PR_FILES_CAP`)."""
MESSAGE_LIMIT = 1024
REFUSED = "The A38 guard would not count this comment, so it was not posted: "
NO_ACCOUNT = (
    "The A38 check needs the posting GitHub account (GET /user), which this connection cannot read; the guard counts a"
    " review comment only from the pull request's author."
)
STEPS = ("queued", "account", "check", "post", "total")


class Transient(RateLimitError):
    """A GitHub failure where a retry may succeed; a `RateLimitError`, so the guard's helpers pass it on instead of
    reading it as an unreadable fact."""

    def __init__(self, error: GitHubError) -> None:
        super().__init__("transient GitHub failure", retry_at=None)
        self.error = error


class GuardApi:
    """The synchronous GitHub interface the guard calls (`request`, `paginate`), answered through `client` on `loop`
    from a worker thread. A refusal with an HTTP status is returned as that status, as the guard's own client does."""

    def __init__(self, client: GitHubClient, loop: asyncio.AbstractEventLoop) -> None:
        self.client = client
        self.loop = loop

    def _call[T](self, make: Callable[[], Awaitable[T]]) -> T:
        async def run() -> T:
            return await make()

        try:
            return asyncio.run_coroutine_threadsafe(run(), self.loop).result(timeout=CALL_TIMEOUT_S)
        except GitHubError as error:
            if error.retryable:
                raise Transient(error) from None
            raise

    def request(self, method: str, path: str, **_kwargs: object) -> tuple[int, Any, dict[str, str]]:
        try:
            answer = self._call(lambda: self.client.request(method, path))
        except GitHubError as error:
            if error.status is None:
                raise GuardError(f"GitHub request failed: {error.kind}") from None
            return error.status, None, {}
        return answer.status, answer.data, dict(answer.headers)

    def paginate(self, path: str, *, hard_limit: int) -> list[Any]:
        items = self._call(lambda: self.client.items(path, max_pages=MAX_PAGES))
        if len(items) > hard_limit:
            raise GuardError(f"pagination exceeded bound ({hard_limit})")
        return items


@dataclass
class Checked:
    """A comment the guard would count: its run, head, merge signal and the step durations so far."""

    check: CommentCheck
    timings: dict[str, int] = field(default_factory=dict)

    def result(self) -> dict[str, Any]:
        found: dict[str, Any] = {"run": self.check.run, "head": self.check.head, "timings_ms": dict(self.timings)}
        if self.check.signal is not None:
            found["signal"] = self.check.signal
        return found


def _ms(seconds: float) -> int:
    return max(0, round(seconds * 1000))


def _since(created_at: datetime) -> int:
    return _ms((datetime.now(UTC) - created_at).total_seconds())


def refusal(reasons: list[str], check_ms: int) -> str:
    """The failure message: the guard's reasons, printable ASCII, at most `MESSAGE_LIMIT` characters."""
    text = REFUSED + "; ".join(reasons) + f". Check took {check_ms} ms."
    text = "".join(char if " " <= char <= "~" else "?" for char in text)
    return text if len(text) <= MESSAGE_LIMIT else text[: MESSAGE_LIMIT - 3] + "..."


async def poster_id(client: GitHubClient) -> int:
    try:
        answer = await client.request("GET", "/user")
    except GitHubError as error:
        if error.retryable or error.kind == "unauthorized":
            raise
        raise OperationFailed("upstream_error", message=NO_ACCOUNT) from None
    return ops.integer(ops.obj(answer.data).get("id"))


async def check(ctx: OperationContext, client: GitHubClient, repo: str, number: int, body: str) -> Checked:
    """The guard's verdict on `body` before it is posted (module docstring); raises `OperationFailed` when the guard
    would not count it."""
    created_at = ctx.operation.created_at
    timings = {"queued": _since(created_at)}
    began = time.monotonic()
    poster = await poster_id(client)
    account_done = time.monotonic()
    timings["account"] = _ms(account_done - began)
    api = GuardApi(client, asyncio.get_running_loop())
    posted_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    try:
        verdict = await asyncio.to_thread(
            check_comment, api, repo=repo, number=number, body=body, poster_id=poster, posted_at=posted_at
        )
    except Transient as transient:
        raise transient.error from None
    timings["check"] = _ms(time.monotonic() - account_done)
    if verdict.reasons:
        timings["total"] = _since(created_at)
        ctx.context.log.write(
            f"runner.github a38_review operation_id={ctx.operation.id} posted=false run={verdict.run}"
            f" reasons={len(verdict.reasons)} "
            + " ".join(f"{step}_ms={value}" for step, value in timings.items())
            + "\n"
        )
        raise OperationFailed("params_invalid", message=refusal(verdict.reasons, timings["check"]))
    return Checked(verdict, timings)


def finish(ctx: OperationContext, checked: Checked, post_began: float) -> dict[str, Any]:
    """Record the post and the total after the comment was posted; the `a38` part of the result."""
    checked.timings["post"] = _ms(time.monotonic() - post_began)
    checked.timings["total"] = _since(ctx.operation.created_at)
    ctx.context.log.write(
        f"runner.github a38_review operation_id={ctx.operation.id} posted=true run={checked.check.run} reasons=0 "
        + " ".join(f"{step}_ms={checked.timings[step]}" for step in STEPS)
        + "\n"
    )
    return checked.result()
