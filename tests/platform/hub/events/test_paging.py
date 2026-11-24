"""The paging helpers of the bounded list endpoints (`agent_hub.events.paging`, ADR-063 section 2, KOR-35)."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

import pytest

from agent_hub.crypto.encoding import b64u_encode
from agent_hub.errors import HubError
from agent_hub.events import paging

AT = datetime(2026, 11, 24, 7, 0, 0, 123456, tzinfo=UTC)


def test_a_cursor_round_trips() -> None:
    cursor = paging.encode_cursor(AT, "0193000a-0000-7000-8000-00000000000a")
    assert paging.decode_cursor(cursor) == (AT, "0193000a-0000-7000-8000-00000000000a")
    assert paging.decode_cursor(cursor, UUID) == (AT, UUID("0193000a-0000-7000-8000-00000000000a"))


@pytest.mark.parametrize(
    "text",
    [
        "x",
        b64u_encode(b"not json"),
        b64u_encode(b'{"at":1}'),
        b64u_encode(b'{"at":1,"id":2}'),
        b64u_encode(b'{"at":"1","id":"a"}'),
        b64u_encode(b'{"at":1000000000000000000000000000000,"id":"a"}'),
        b64u_encode(b'{"id":"a","at":1}'),
    ],
)
def test_malformed_cursors_are_request_invalid(text: str) -> None:
    with pytest.raises(HubError) as caught:
        paging.decode_cursor(text)
    assert (caught.value.code, caught.value.detail) == ("request_invalid", {"pointers": ["/before"]})


def test_a_key_the_list_cannot_use_is_request_invalid() -> None:
    with pytest.raises(HubError):
        paging.decode_cursor(paging.encode_cursor(AT, "not-a-uuid"), UUID)


def test_windows_and_their_extra_fields() -> None:
    capped = paging.window(None, None, 7)
    assert capped == paging.Window(paged=False, limit=7)
    assert (capped.extra(None), capped.extra((AT, "a"))) == ({}, {"truncated": True})
    first = paging.window(None, "3", 7)
    assert first == paging.Window(paged=True, limit=3)
    assert first.extra(None) == {"next_before": None}
    assert first.extra((AT, "a")) == {"next_before": paging.encode_cursor(AT, "a")}
    later = paging.window(paging.encode_cursor(AT, "a"), None, 7)
    assert later == paging.Window(paged=True, limit=paging.LIST_DEFAULT, before=(AT, "a"))
    with pytest.raises(HubError):
        paging.window(None, "201", 7)
    assert paging.cut([1, 2, 3], 2) == ([1, 2], True)
    assert paging.cut([1, 2], 2) == ([1, 2], False)
