"""U-HUB-head (stream, HOT-19): `HEAD /v1/stream` on a real uvicorn server answers the status and headers of the
browser live channel without a body and ends at once (the stream is not followed); its read is recorded as for the
`GET`."""

from __future__ import annotations

from typing import cast
from urllib.parse import urlencode

import httpx

from tests.platform.hub.conftest import SigningKey
from tests.platform.hub.events.support import Scene
from tests.platform.hub.stream.support import LiveHub, signed_headers


def test_head_of_the_stream_answers_its_start_and_ends(scene: Scene, live: LiveHub) -> None:
    target = "/v1/stream?" + urlencode({"org": scene.org})
    headers = signed_headers(cast(SigningKey, scene.writer.browser.key), live.clock, target, method="HEAD")
    with httpx.Client(timeout=httpx.Timeout(10.0)) as client:
        head = client.head(live.base + target, headers=headers)
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-type"].startswith("text/event-stream")
    rows = scene.rows("SELECT action, result, org_id::text FROM hub.audit WHERE action LIKE 'read.stream%%'")
    assert rows == [("read.stream", "ok", scene.org)]
