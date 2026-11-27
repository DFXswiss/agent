"""HOT-29: a page of `GET /v1/scopes/{scope_id}/events` is bounded in stored bytes as well as in count
(`agent_hub.events.read.PAGE_BYTES`), so a page of large events stays below what a signed client accepts; the first
event of a page is always served, and the cursors (`next_after`, `next_before`) go on behind the last event served.
The device pull's bound: `tests/platform/hub/device/test_pull.py`."""

from __future__ import annotations

import secrets
from typing import Any
from uuid import UUID

import pytest

from agent_hub.crypto import event as event_format
from agent_hub.crypto import signed
from agent_hub.crypto.encoding import b64u_encode
from agent_hub.events import read
from tests.platform.hub.events.support import Scene

Json = dict[str, Any]
CT_BYTES = 20_000


def sized(scene: Scene) -> Json:
    """A workgroup event of the writer with a random `ct` of `CT_BYTES` (the hub never decrypts)."""
    event = scene.wg_event(scene.writer)
    ct = secrets.token_bytes(CT_BYTES)
    sig = scene.writer.identity.sig.sign(signed.signature_text(event_format.FORMAT, event["header"], ct))
    return {"header": event["header"], "ct": b64u_encode(ct), "sig": b64u_encode(sig)}


def _fill(scene: Scene, count: int = 5) -> dict[int, int]:
    """`count` large events (seq 1 to count) and the stored bytes of each (`size` of `ct`, header text, `sig`)."""
    for _ in range(count):
        assert scene.post(scene.writer, sized(scene)).status_code == 200
    rows = scene.rows(
        "SELECT scope_seq, size + octet_length(header_text) + octet_length(sig) FROM hub.event WHERE scope_id = %s",
        scene.wg,
    )
    return {int(seq): int(size) for seq, size in rows}


def _page(scene: Scene, **query: str) -> tuple[list[int], Json]:
    response = scene.read(scene.reader, **query)
    assert response.status_code == 200, response.text
    body = response.json()
    return [item["scope_seq"] for item in body["events"]], body


def test_pages_are_bounded_in_bytes_in_both_directions(scene: Scene, monkeypatch: pytest.MonkeyPatch) -> None:
    sizes = _fill(scene)
    assert sizes[1] > CT_BYTES
    monkeypatch.setattr(read, "PAGE_BYTES", sizes[1] + sizes[2])  # two events fit, a third does not
    seqs, body = _page(scene)
    assert (seqs, body["next_after"], body["head"]) == ([1, 2], 2, 5)
    assert _page(scene, after="2")[0] == [3, 4]
    seqs, body = _page(scene, after="4")
    assert (seqs, body["next_after"]) == ([5], None)
    seqs, body = _page(scene, before="9999999999999999")
    assert (seqs, body["next_before"]) == ([5, 4], 4)
    seqs, body = _page(scene, before="4")
    assert (seqs, body["next_before"]) == ([3, 2], 2)
    seqs, body = _page(scene, before="2")
    assert (seqs, body["next_before"]) == ([1], None)
    # the count still applies within the bytes
    seqs, body = _page(scene, limit="1")
    assert (seqs, body["next_after"]) == ([1], 1)


def test_an_event_larger_than_the_bound_is_served_alone(scene: Scene, monkeypatch: pytest.MonkeyPatch) -> None:
    _fill(scene, 3)
    monkeypatch.setattr(read, "PAGE_BYTES", 1)
    seqs, body = _page(scene)
    assert (seqs, body["next_after"]) == ([1], 1)
    seqs, body = _page(scene, after="1")
    assert (seqs, body["next_after"]) == ([2], 2)
    seqs, body = _page(scene, after="2")
    assert (seqs, body["next_after"]) == ([3], None)
    seqs, body = _page(scene, before="9999999999999999")
    assert (seqs, body["next_before"]) == ([3], 3)
    seqs, body = _page(scene, before="2")
    assert (seqs, body["next_before"]) == ([1], None)


def test_the_bound_is_counted_in_the_query(scene: Scene) -> None:
    """`read_events` with an explicit bound: the stored bytes of the page, and the bound reached exactly."""
    sizes = _fill(scene, 3)
    wg = UUID(scene.wg)
    with scene.world.hub.pool.connection() as connection:
        exact = read.read_events(connection, wg, max_bytes=sizes[1] + sizes[2])
        assert ([item["scope_seq"] for item in exact.events], exact.next_after, exact.size) == (
            [1, 2],
            2,
            sizes[1] + sizes[2],
        )
        short = read.read_events(connection, wg, max_bytes=sizes[1] + sizes[2] - 1)
        assert ([item["scope_seq"] for item in short.events], short.next_after) == ([1], 1)
        whole = read.read_events(connection, wg)
        assert ([item["scope_seq"] for item in whole.events], whole.next_after, whole.size) == (
            [1, 2, 3],
            None,
            sum(sizes.values()),
        )
