"""Spend of the platform key (05-runner.md section 10.6, 08-monitoring.md section 3; ADR-010).

On a platform device with a usable `OPENROUTER_KEY_FILE`, `SpendPoller.tick(now)` (task `quota`) reads `GET
<MODEL_UPSTREAM_OPENROUTER>/v1/key` (i.e. `https://openrouter.ai/api/v1/key`) every 900 s with `Authorization: Bearer
<key>`, 8 s timeout, no redirects, and keeps the spend of `parsers.spend_snapshot` in memory. After `429` the next
read waits `max(Retry-After, 300 s)`. The key is read from its file for the request only and never logged.

`spend_metrics(context)` is the interface to the metrics of RUN-15 (`textfile.metrics_of(..., spend=...)`): the gauge
`agent_runner_device_model_spend_usd{window}` with the windows `daily`, `weekly`, `monthly` and `total` (USD, as the
key reports them), operators only (textfile sink, never the hub). None while no fresh snapshot exists: on any other
device, before the first successful read, and when the last successful read is older than `STALE_S` (a key that
stopped working must not show an old value forever).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from agent_runner.model.provision import read_platform_key
from agent_runner.quota.parsers import UsageInvalid, spend_snapshot
from agent_runner.quota.reader import ClientFactory, default_client
from agent_runner.quota.subscription import POLL_S, RATE_LIMIT_MIN_S, retry_after

STALE_S = 3 * POLL_S
METRIC_WINDOWS = ("daily", "weekly", "monthly", "total")
ATTRIBUTE = "quota_spend_poller"


@dataclass
class SpendPoller:
    context: Any
    upstream: str
    key: Callable[[], str | None]
    client: ClientFactory = default_client
    wall: Callable[[], float] = time.time
    next_at: float = 0.0
    fetched_at: float | None = None
    spend: dict[str, Any] = field(default_factory=dict)

    def line(self, text: str) -> None:
        self.context.log.write(f"runner.quota {text}\n")
        self.context.log.flush()

    @property
    def url(self) -> str:
        return self.upstream.rstrip("/") + "/v1/key"

    async def tick(self, now: float) -> None:
        if now < self.next_at:
            return
        self.next_at = now + POLL_S
        key = self.key()
        if key is None:
            return
        try:
            async with self.client() as client:
                response = await client.get(
                    self.url, headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}
                )
        except httpx.HTTPError as exc:
            self.line(f"spend_failed error={type(exc).__name__}")
            return
        status = response.status_code
        if status == 429:
            self.next_at = now + max(retry_after(response.headers.get("retry-after"), now), RATE_LIMIT_MIN_S)
            self.line("spend_rate_limited")
            return
        if status != 200:
            self.line(f"spend_failed status={status}")
            return
        try:
            self.spend = spend_snapshot(response.json())
        except (UsageInvalid, ValueError) as exc:
            detail = exc.field if isinstance(exc, UsageInvalid) else "json"
            self.line(f"spend_invalid field={detail}")
            return
        self.fetched_at = now

    def metrics(self, now: float) -> dict[str, float] | None:
        if self.fetched_at is None or now - self.fetched_at > STALE_S:
            return None
        return {window: float(self.spend[window]) for window in METRIC_WINDOWS}


def spend_poller(context: Any, wall: Callable[[], float] = time.time) -> SpendPoller | None:
    """The poller of a platform device with a configured key file (built once per daemon), else None."""
    found = getattr(context, ATTRIBUTE, None)
    if isinstance(found, SpendPoller):
        return found
    identity, config = getattr(context, "identity", None), context.config
    if identity is None or identity.device.kind != "platform" or config.openrouter_key_file is None:
        return None
    path = config.openrouter_key_file
    created = SpendPoller(
        context=context, upstream=config.model_upstream_openrouter, key=lambda: read_platform_key(path), wall=wall
    )
    setattr(context, ATTRIBUTE, created)
    return created


def spend_metrics(context: Any) -> dict[str, float] | None:
    """`{window: USD}` of the platform key for `agent_runner_device_model_spend_usd`, None without fresh data."""
    found = getattr(context, ATTRIBUTE, None)
    if not isinstance(found, SpendPoller):
        return None
    return found.metrics(found.wall())
