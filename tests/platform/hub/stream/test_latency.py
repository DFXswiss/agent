"""U-HUB-stream (latency): hub receive → browser frame on loopback, measured over 200 events stored one by one
(03-api.md section 5: p95 < 300 ms on LAN; PERF-03 measures the whole chain end to end)."""

from __future__ import annotations

import statistics
import time

from tests.platform.hub.events.support import Scene
from tests.platform.hub.stream.support import LiveHub, browser_key

EVENTS = 200
P95_LIMIT_SECONDS = 0.3


def test_event_latency_p95_below_300_ms(scene: Scene, live: LiveHub) -> None:
    _, session = scene.start_session()
    stream = live.stream(browser_key(scene.reader), org=scene.org)
    assert stream.frame().event == "hello"  # type: ignore[union-attr]
    latencies = []
    for _ in range(EVENTS):
        started = time.perf_counter()
        [stored] = scene.append(scene.device, scene.session_event(scene.device, session))
        frame = stream.frame()
        assert frame is not None
        assert frame.data["scope_seq"] == stored.scope_seq
        latencies.append(frame.at - started)
    p95 = statistics.quantiles(latencies, n=20)[-1]
    p50 = statistics.median(latencies)
    print(f"stream latency over {EVENTS} events: p50 {p50 * 1000:.1f} ms, p95 {p95 * 1000:.1f} ms")
    assert p95 < P95_LIMIT_SECONDS
