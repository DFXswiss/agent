"""Rate limits of 03-api.md section 1 with the fixed clock: anonymous 30/min per source, 600/min per browser session
key, 6000/min per device key, `429 rate_limited` with `Retry-After`."""

from __future__ import annotations

import pytest

from agent_hub.http.limits import (
    ANONYMOUS_PER_MINUTE,
    DEVICE_PER_MINUTE,
    FAILED_PER_MINUTE,
    ROOT_PER_MINUTE,
    SESSION_PER_MINUTE,
    RateLimiter,
)
from tests.platform.hub.conftest import FixedClock, HubHarness


def test_the_specified_limits() -> None:
    assert FAILED_PER_MINUTE == 30
    assert (ANONYMOUS_PER_MINUTE, SESSION_PER_MINUTE, DEVICE_PER_MINUTE, ROOT_PER_MINUTE) == (30, 600, 6000, 600)


def test_bucket_burst_refill_and_retry_after(clock: FixedClock) -> None:
    limiter = RateLimiter(clock)
    assert all(limiter.hit("anonymous", "a") is None for _ in range(30))
    assert limiter.hit("anonymous", "a") == 2
    assert limiter.hit("anonymous", "b") is None
    clock.advance(1)
    assert limiter.hit("anonymous", "a") == 1
    clock.advance(1)
    assert limiter.hit("anonymous", "a") is None
    assert limiter.hit("anonymous", "a") == 2
    clock.advance(3600)
    assert all(limiter.hit("anonymous", "a") is None for _ in range(30))
    assert limiter.hit("anonymous", "a") is not None


def test_a_clock_going_back_adds_no_tokens(clock: FixedClock) -> None:
    limiter = RateLimiter(clock)
    for _ in range(30):
        limiter.hit("anonymous", "a")
    clock.advance(-60)
    assert limiter.hit("anonymous", "a") == 2


@pytest.mark.parametrize(("kind", "limit"), [("session", 600), ("device", 6000), ("root", 600)])
def test_key_limits(clock: FixedClock, kind: str, limit: int) -> None:
    limiter = RateLimiter(clock)
    assert all(limiter.hit(kind, "k") is None for _ in range(limit))
    assert limiter.hit(kind, "k") == 1
    assert limiter.hit("anonymous", "k") is None


def test_bucket_count_is_bounded(clock: FixedClock) -> None:
    limiter = RateLimiter(clock, max_buckets=3)
    for name in "abcd":
        limiter.hit("anonymous", name)
    assert len(limiter) == 3
    for _ in range(29):
        limiter.hit("anonymous", "d")
    limiter.hit("anonymous", "b")
    limiter.hit("anonymous", "e")
    assert limiter.hit("anonymous", "d") is not None


def test_anonymous_limit_per_source(hub: HubHarness) -> None:
    for _ in range(30):
        assert hub.anon.get("/v1/health").status_code == 200
    response = hub.anon.get("/v1/config")
    assert response.status_code == 429
    assert response.json() == {"error": "rate_limited"}
    assert response.headers["retry-after"] == "2"
    hub.clock.advance(2)
    assert hub.anon.get("/v1/health").status_code == 200


def test_static_files_are_not_limited(hub: HubHarness) -> None:
    for _ in range(40):
        assert hub.anon.get("/assets/missing.js").status_code == 404


def test_signed_requests_use_the_key_limit(hub: HubHarness) -> None:
    for _ in range(30):
        hub.anon.get("/v1/health")
    assert hub.anon.get("/v1/health").status_code == 429
    browser = hub.browser()
    assert browser.get("/v1/health").status_code == 200
    key = browser.key
    assert key is not None
    limiter = hub.app.state.hub.limiter
    for _ in range(SESSION_PER_MINUTE - 1):
        limiter.hit("session", key.key_id)
    response = browser.get("/v1/health")
    assert response.status_code == 429
    assert response.headers["retry-after"] == "1"
    assert response.extensions["hub_signature"] is True
    assert hub.browser().get("/v1/health").status_code == 200


def test_device_limit(hub: HubHarness) -> None:
    device = hub.device()
    key = device.key
    assert key is not None
    limiter = hub.app.state.hub.limiter
    for _ in range(DEVICE_PER_MINUTE):
        limiter.hit("device", key.key_id)
    assert device.get("/v1/health").status_code == 429


def test_failed_signatures_count_against_the_source(hub: HubHarness) -> None:
    # After 30 failures the source gets 429 before its body is read or its key looked up; unsigned requests and
    # valid signatures of other keys from the source keep their own limits.
    impostor = hub.keys.session()
    headers = {"Agent-Key": impostor.key_id, "Agent-Time": "2026-11-21T12:00:00Z"}
    headers["Agent-Nonce"] = "AAAAAAAAAAAAAAAAAAAAAA"
    headers["Agent-Signature"] = "A" * 86
    for _ in range(30):
        assert hub.base.get("/v1/health", headers=headers).status_code == 401
    response = hub.base.get("/v1/health", headers=headers)
    assert response.status_code == 429
    assert "agent-hub-signature" not in response.headers
    assert hub.root.get("/v1/health").status_code == 429
    assert hub.anon.get("/v1/health").status_code == 200


def test_blocked_takes_no_token(clock: FixedClock) -> None:
    limiter = RateLimiter(clock)
    assert limiter.blocked("failed", "a") is None
    for _ in range(30):
        limiter.hit("failed", "a")
    assert limiter.blocked("failed", "a") == 2
    clock.advance(2)
    assert limiter.blocked("failed", "a") is None
    assert limiter.hit("failed", "a") is None
