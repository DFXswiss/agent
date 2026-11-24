"""U-HUB-stream (HEAD, HOT-19): `HEAD /v1/stream` on a real uvicorn server answers the status and headers of the
browser live channel without a body and ends at once; its read is recorded as for the `GET`, and the stream it opened
is forgotten (its body never starts, so the body's own cleanup never runs)."""

from __future__ import annotations

from typing import cast
from urllib.parse import urlencode

import httpx

from tests.platform.hub.conftest import SigningKey
from tests.platform.hub.events.support import Scene
from tests.platform.hub.stream.support import LiveHub, signed_headers, wait_for

# the time of each answer, and the framing of a body the server adds when it sends one (none for HEAD)
PER_ANSWER = ("agent-", "date", "transfer-encoding")


def test_head_of_the_stream_answers_its_start_and_forgets_the_stream(scene: Scene, live: LiveHub) -> None:
    key = cast(SigningKey, scene.writer.browser.key)
    got = live.stream(key, org=scene.org)
    assert got.frames(1)[0].event == "hello"
    expected = {name: value for name, value in got.response.headers.items() if not name.startswith(PER_ANSWER)}
    got.close()
    wait_for(lambda: not live.service.by_org)
    target = "/v1/stream?" + urlencode({"org": scene.org})
    with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
        for _ in range(3):
            head = client.head(live.base + target, headers=signed_headers(key, live.clock, target, method="HEAD"))
            assert head.status_code == 200
            assert head.content == b""
            assert {k: v for k, v in head.headers.items() if not k.startswith(PER_ANSWER)} == expected
    assert wait_for(lambda: not live.service.by_org and not live.service.by_user)
    rows = scene.rows("SELECT action, result, org_id::text FROM hub.audit WHERE action LIKE 'read.stream%%'")
    assert rows[0] == ("read.stream", "ok", scene.org)
