"""Sealing of outbox events (04-crypto.md section 5.12, 05-runner.md section 2.3).

An `event` item is stored as a draft (`outbox.event_draft`) and sealed at its first push attempt: the writer chooses
the **current** epoch of the scope (the highest epoch whose key the device holds), sets `author` to the device ID and
`lh` to its verified log head of the org, encrypts the body JSON and signs with the device key. The sealed event is
stored and reused for every retry. After `status:"reseal"` the item is sealed again with an epoch **newer** than the
refused one (`min_epoch`); while the device holds no such key yet, sealing returns None and the push waits for the
reconcile loop (RUN-03) to fetch the new wrap.

`check_draft` runs the header and size checks at `enqueue` time with placeholder values, so a draft that could
never be sealed is refused before it takes an `origin_seq` (a gapless stream cannot skip an item locally).

`split_terminal` turns a well-formed `session.terminal` body whose sealed size would exceed E8 (64 KiB) into several
bodies of consecutive frames (an event's data cut at character boundaries), so a chunk of a guest that measured its
limit on the raw bytes is delivered instead of dropped (decisions/KOR-02.md).

`StoreSealer` reads the scope keys (`runner.scope_key`, sealed with the storage key) and the verified log head
(`runner.log_head`) that the reconcile steps of RUN-03 maintain; another implementation of `Sealer` may be injected.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from psycopg import AsyncConnection

from agent_runner.crypto.encoding import body_json
from agent_runner.crypto.errors import fail
from agent_runner.crypto.event import MAX_TERMINAL_CT_BYTES, max_ct_bytes, new_header, seal_event
from agent_runner.crypto.storage import open_local, scope_key_context

AEAD_TAG_BYTES = 16
MAX_TERMINAL_BODY = MAX_TERMINAL_CT_BYTES - AEAD_TAG_BYTES
MAX_TERMINAL_DATA = 16 * 1024
"""contracts `body_session_terminal`: an event's data has at most 16384 characters."""
MAX_TERMINAL_FRAMES = 4096
MAX_COUNT = 2**53 - 1
_TERMINAL_KEYS = frozenset({"offset_ms_start", "frames", "cols", "rows"})
_PLACEHOLDER_AUTHOR = "00000000-0000-7000-8000-000000000000"


class Sealer(Protocol):
    async def seal(
        self, conn: AsyncConnection[Any], draft: Mapping[str, Any], *, min_epoch: int = 1
    ) -> dict[str, object] | None: ...


def _fields(header: Mapping[str, Any], *, epoch: int, author: str, lh: int) -> dict[str, object]:
    return new_header(
        id=header["id"],
        org=header["org"],
        scope=header["scope"],
        scope_id=header["scope_id"],
        epoch=epoch,
        author=author,
        kind=header["kind"],
        ts=header["ts"],
        lh=lh,
        wg=header["wg"],
        task=header["task"],
        session=header["session"],
        ref=header["ref"],
    )


def check_draft(draft: Mapping[str, Any]) -> None:
    """E1 header formats and the E8 size limit of a draft (`CryptoError` `header_invalid`/`non_ascii`/
    `size_exceeded`)."""
    fields = _fields(draft["header"], epoch=1, author=_PLACEHOLDER_AUTHOR, lh=0)
    if len(body_json(draft["body"])) + AEAD_TAG_BYTES > max_ct_bytes(fields):
        fail("size_exceeded")


def _terminal_frames(body: Mapping[str, Any]) -> list[tuple[int, str, str]] | None:
    """The frames of a body that satisfies `body_session_terminal` apart from its sealed size, else None (left to
    the seal checks): at most 4096 frames of `[t_ms, "o"|"m", data]`, every data at most 16384 characters and 16 KiB
    of UTF-8 data in all, `cols`/`rows` 1-1000. Only such a body is split, so an untrusted guest cannot make the
    runner emit more or larger chunks than the contract allows a guest to write."""
    frames, offset, cols, rows = (body.get(key) for key in ("frames", "offset_ms_start", "cols", "rows"))
    if set(body) != _TERMINAL_KEYS or not isinstance(frames, list) or not 0 < len(frames) <= MAX_TERMINAL_FRAMES:
        return None
    bounds = ((offset, 0, MAX_COUNT), (cols, 1, 1000), (rows, 1, 1000))
    if not all(type(value) is int and low <= value <= high for value, low, high in bounds):
        return None
    found = []
    for frame in frames:
        if not (isinstance(frame, list) and len(frame) == 3):
            return None
        at, code, data = frame
        if type(at) is not int or not 0 <= at <= MAX_COUNT or code not in ("o", "m") or not isinstance(data, str):
            return None
        found.append((at, code, data))
    if sum(len(data.encode("utf-8", "surrogatepass")) for _, _, data in found) > MAX_TERMINAL_DATA:
        return None
    return found


def _pieces(data: str) -> list[str]:
    """`data` in pieces of at most `MAX_TERMINAL_DATA` characters and as many bytes of body JSON."""
    pieces = []
    while data:
        count = min(len(data), MAX_TERMINAL_DATA)
        low, high = 1, count
        while low < high:
            middle = (low + high + 1) // 2
            if len(body_json(data[:middle])) - 2 <= MAX_TERMINAL_DATA:
                low = middle
            else:
                high = middle - 1
        pieces.append(data[:low])
        data = data[low:]
    return pieces


def split_terminal(body: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """`body` itself when it fits E8 or is no well-formed terminal body; else consecutive bodies that each fit."""
    frames = _terminal_frames(body)
    if frames is None or len(body_json(body)) <= MAX_TERMINAL_BODY:
        return [body]
    parts: list[list[list[object]]] = []
    group: list[list[object]] = []
    size = 0
    for at, code, data in frames:
        for piece in _pieces(data) or [data]:
            frame: list[object] = [at, code, piece]
            frame_size = len(body_json(frame))
            if group and (size + 1 + frame_size > MAX_TERMINAL_BODY or len(group) == MAX_TERMINAL_FRAMES):
                parts.append(group)
                group = []
            if not group:
                size = len(body_json({**body, "offset_ms_start": at, "frames": [frame]}))
            else:
                size += 1 + frame_size
            group.append(frame)
    parts.append(group)
    return [{**body, "offset_ms_start": part[0][0], "frames": part} for part in parts]


class StoreSealer:
    """Seals with the scope keys and log heads of the runner store."""

    def __init__(self, *, storage_key: bytes, device_id: str, signing_key: Ed25519PrivateKey) -> None:
        self._storage_key = storage_key
        self._device_id = device_id
        self._signing_key = signing_key

    async def seal(
        self, conn: AsyncConnection[Any], draft: Mapping[str, Any], *, min_epoch: int = 1
    ) -> dict[str, object] | None:
        header = draft["header"]
        cur = await conn.execute(
            "SELECT epoch, key_ct FROM runner.scope_key WHERE scope = %s AND scope_id = %s AND epoch >= %s "
            "ORDER BY epoch DESC LIMIT 1",
            (header["scope"], header["scope_id"], min_epoch),
        )
        key_row = await cur.fetchone()
        cur = await conn.execute("SELECT seq FROM runner.log_head WHERE org_id = %s", (header["org"],))
        head_row = await cur.fetchone()
        if key_row is None or head_row is None:
            return None
        epoch = int(key_row[0])
        context = scope_key_context(header["scope"], header["scope_id"], epoch)
        scope_key = open_local(self._storage_key, "scope_key", context, bytes(key_row[1]))
        fields = _fields(header, epoch=epoch, author=self._device_id, lh=int(head_row[0]))
        return seal_event(scope_key, fields, body_json(draft["body"]), self._signing_key)
