"""checks/plaintext_scan.py: encodings (ASCII and non-ASCII canaries), HTTP and WebSocket decoding of relay taps in
both directions, the scan and its command line."""

from __future__ import annotations

import base64
import gzip
import json
import unicodedata
import zlib
from pathlib import Path
from urllib.parse import quote

import pytest
from checks import plaintext_scan as scan_module
from checks.plaintext_scan import (
    Finding,
    check_canaries,
    encodings,
    http_parts,
    http_source,
    main,
    scan,
    sse_frames,
    ws_failures,
    ws_messages,
)

CANARY = "kz-canary-7f3a9c1e"
UNICODE = "Zκ-Ärger-日本語-😀-5b1c"


def test_canaries_are_checked() -> None:
    assert check_canaries([CANARY, UNICODE]) == [CANARY, UNICODE]
    for bad in ("short", "kürzer", " leading-space", "trailing-space ", "tab\tinside-x", "line\nbreak-x", 7):
        with pytest.raises(ValueError, match="printable text"):
            check_canaries([bad])


def test_encodings_cover_raw_hex_and_every_base64_alignment() -> None:
    found = dict(encodings(CANARY))
    assert found["raw"] == CANARY.encode()
    assert found["hex"] == CANARY.encode().hex().encode()
    assert found["HEX"] == CANARY.encode().hex().upper().encode()
    for prefix in (b"", b"a", b"ab", b"abc"):
        for encode in (base64.b64encode, base64.urlsafe_b64encode):
            encoded = encode(prefix + CANARY.encode() + b"tail")
            assert any(name.startswith("base64") and value in encoded for name, value in found.items()), prefix
    # base64 and base64url pieces that are equal are listed once; every piece is long enough to mean something
    assert len(set(found.values())) == len(found)
    assert min(len(value) for value in dict(encodings("kz-12345")).values()) == 8


def test_scan_reports_each_source_and_encoding() -> None:
    sources = {
        "dump": b"COPY hub.task FROM stdin;\n" + CANARY.encode().hex().encode() + b"\n",
        "log": b"nothing here",
        "b64": base64.b64encode(b"x" + CANARY.encode()),
    }
    findings = scan(sources, [CANARY])
    assert Finding("dump", CANARY, "hex") in findings
    assert {item.source for item in findings} == {"dump", "b64"}
    assert scan({"log": b"clean"}, [CANARY]) == []


def response(headers: str, body: bytes) -> bytes:
    return f"HTTP/1.1 200 OK\r\n{headers}\r\n\r\n".encode() + body


def test_http_parts_decode_lengths_chunks_and_compression() -> None:
    plain = response("Content-Length: 5", b"hello")
    chunked = response("Transfer-Encoding: chunked", b"6\r\nevent:\r\n4;x=1\r\n hi\n\r\n0\r\n\r\n")
    zipped = gzip.compress(b"gzipped " + CANARY.encode())
    gz = response(f"Content-Encoding: gzip\r\nContent-Length: {len(zipped)}", zipped)
    deflated = zlib.compress(b"deflated body")
    df = response(f"Content-Encoding: deflate\r\nContent-Length: {len(deflated)}", deflated)
    parts = http_parts(plain + chunked + gz + df)
    assert b"hello" in parts
    assert b"event: hi\n" in parts
    assert b"gzipped " + CANARY.encode() in parts
    assert b"deflated body" in parts
    # a canary split by the chunked coding is found in the decoded part
    split = response("Transfer-Encoding: chunked", b"6\r\nkz-can\r\nc\r\nary-7f3a9c1e\r\n0\r\n\r\n")
    assert CANARY.encode() not in split
    assert scan({"tap": http_source(split)}, [CANARY]) == [Finding("tap", CANARY, "raw")]


def test_http_parts_keep_what_cannot_be_decoded() -> None:
    assert http_parts(b"") == []
    assert http_parts(b"not http at all") == [b"not http at all"]
    assert http_parts(b"HTTP/1.1 200 OK\r\nno end of headers") == [b"HTTP/1.1 200 OK\r\nno end of headers"]
    # streaming without a length runs to the end; a cut stream keeps the partial chunks
    assert http_parts(response("Content-Type: text/event-stream", b"event: a\n"))[-1] == b"event: a\n"
    cut = http_parts(response("Transfer-Encoding: chunked", b"9\r\nevent: a\n\r\n20\r\nevent: b"))
    assert cut[-1] == b"event: a\nevent: b"
    assert http_parts(response("Transfer-Encoding: chunked", b"9\r\nevent: a\n"))[-1] == b"event: a\n"
    assert http_parts(response("Transfer-Encoding: chunked", b"5\r\nhello\r\n"))[-1] == b"hello"
    assert http_parts(response("Transfer-Encoding: chunked", b"zz\r\nhello"))[-1] == b"zz\r\nhello"
    broken = response("Content-Encoding: gzip\r\nContent-Length: 3", b"abc")
    assert http_parts(broken)[-1] == b"abc"
    assert http_parts(response("Transfer-Encoding: chunked", b"0\r\n"))[-1] == b""


def test_sse_frames_counts_event_lines() -> None:
    stream = response("Transfer-Encoding: chunked", b"1d\r\nevent: hello\ndata: {}\n\nevent: \r\n")
    assert sse_frames(stream) == 1
    assert sse_frames(b"") == 0


def test_main(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dump = tmp_path / "dump.sql"
    dump.write_text(f"x {CANARY} y")
    tap = tmp_path / "1.down"
    tap.write_bytes(response("Content-Length: 2", b"ok"))
    assert main(["--canary", CANARY, "--file", f"dump={dump}", "--http", f"tap={tap}"]) == 1
    assert json.loads(capsys.readouterr().out) == [{"source": "dump", "canary": CANARY, "encoding": "raw"}]
    assert main(["--canary", CANARY, "--http", f"tap={tap}"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert scan_module.MIN_CANARY == 8


# -- non-ASCII canaries (SCN-09) -------------------------------------------------------------------------------------


def test_unicode_canaries_are_found_in_the_encodings_of_stores_logs_and_urls() -> None:
    found = dict(encodings(UNICODE))
    raw = UNICODE.encode("utf-8")
    assert found["raw"] == raw
    assert found["hex"] == raw.hex().encode()
    escaped = json.dumps(UNICODE)[1:-1].encode()
    assert found["json"] == escaped
    assert b"\\ud83d\\ude00" in escaped  # astral characters as surrogate pairs
    upper = "".join(f"\\u{ord(c):04X}" if ord(c) > 0x7F and ord(c) < 0x10000 else c for c in UNICODE)
    assert found["JSON"] == upper.replace("😀", "\\uD83D\\uDE00").encode()
    assert found["PERCENT"] == quote(UNICODE, safe="").encode()
    assert found["PERCENT"] != found["percent"]
    assert found["percent"].lower() == found["PERCENT"].lower()
    # a decomposed form (NFD, as some file systems store names) is searched too
    decomposed = unicodedata.normalize("NFD", UNICODE)
    assert found["raw/nfd"] == decomposed.encode()
    assert "raw/nfc" not in found  # the canary is already composed
    sources = {
        "dump": b"x" + raw + b"y",
        "log": json.dumps({"text": UNICODE}).encode(),
        "url": b"GET /v1/search?q=" + quote(UNICODE).encode(),
        "b64": base64.urlsafe_b64encode(b"ab" + raw),
        "nfd": decomposed.encode(),
    }
    findings = {(item.source, item.encoding) for item in scan(sources, [UNICODE])}
    assert ("dump", "raw") in findings
    assert ("log", "json") in findings
    assert ("url", "PERCENT") in findings
    assert {name for source, name in findings if source == "b64"} & {"base64+2", "base64url+2"}
    assert ("nfd", "raw/nfd") in findings
    # an already decomposed canary is also searched in its composed form
    assert "raw/nfc" in dict(encodings(decomposed))


# -- requests and WebSocket frames (both directions of a relay tap) ---------------------------------------------------


def frame(payload: bytes, *, opcode: int = 1, fin: bool = True, rsv1: bool = False, mask: bytes | None = None) -> bytes:
    head = bytes([(0x80 if fin else 0) | (0x40 if rsv1 else 0) | opcode])
    length = len(payload)
    flag = 0x80 if mask is not None else 0
    if length < 126:
        head += bytes([flag | length])
    elif length < 65536:
        head += bytes([flag | 126]) + length.to_bytes(2, "big")
    else:
        head += bytes([flag | 127]) + length.to_bytes(8, "big")
    if mask is None:
        return head + payload
    return head + mask + bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))


def deflated(*messages: bytes) -> list[bytes]:
    """permessage-deflate with context takeover: one compressor, the 4-byte tail of each sync flush removed."""
    compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
    out = []
    for message in messages:
        data = compressor.compress(message) + compressor.flush(zlib.Z_SYNC_FLUSH)
        out.append(data[:-4])
    return out


UPGRADE = b"GET /v1/device/ws HTTP/1.1\r\nHost: hub\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n"
SWITCHED = (
    b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nSec-WebSocket-Extensions: permessage-deflate\r\n\r\n"
)


def test_requests_are_decoded_with_and_without_bodies() -> None:
    post = b"POST /v1/events HTTP/1.1\r\nContent-Length: 18\r\n\r\n" + CANARY.encode()
    get = b"GET /v1/stream HTTP/1.1\r\nAccept: text/event-stream\r\n\r\n"
    chunked = b"PUT /v1/x HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nkz-c\r\n0\r\n\r\n"
    parts = http_parts(get + post + chunked)
    assert parts[0].startswith(b"GET /v1/stream")
    assert parts[1] == b""
    assert CANARY.encode() in parts
    assert b"kz-c" in parts


def test_responses_without_a_body_do_not_swallow_the_next_one() -> None:
    for status in (b"204 No Content", b"304 Not Modified", b"100 Continue"):
        data = b"HTTP/1.1 " + status + b"\r\n\r\n" + response("Content-Length: 18", CANARY.encode())
        parts = http_parts(data)
        assert parts[1] == b""
        assert CANARY.encode() in parts


def test_websocket_frames_of_the_hub_are_decoded() -> None:
    hint = json.dumps({"hint": "events", "note": CANARY}).encode()
    long = UNICODE.encode() + b"." * 70_000
    first, second = deflated(hint, hint)
    down = SWITCHED + frame(first, rsv1=True) + frame(second, rsv1=True) + frame(b"\x03\xe8bye", opcode=8)
    down += frame(b"x" * 300, opcode=2) + frame(long, opcode=2)
    parts = http_parts(down)
    assert parts.count(hint) == 2  # the second message refers back to the first one's window
    assert b"x" * 300 in parts
    assert long in parts
    assert ws_messages(down) == 4  # the close frame is no data message
    assert scan({"tap": http_source(down)}, [CANARY, UNICODE]) == [
        Finding("tap", CANARY, "raw"),
        Finding("tap", UNICODE, "raw"),
    ]


def test_websocket_frames_of_the_client_are_unmasked_and_joined() -> None:
    mask = b"\x11\x22\x33\x44"
    up = UPGRADE + frame(b"kz-canary-", fin=False, mask=mask) + frame(b"7f3a9c1e", opcode=0, mask=mask)
    up += frame(b"ping", opcode=9, mask=mask)
    assert CANARY.encode() not in up
    assert CANARY.encode() in http_parts(up)
    assert ws_messages(up) == 1
    assert scan({"tap": http_source(up)}, [CANARY]) == [Finding("tap", CANARY, "raw")]


def test_websocket_rests_that_cannot_be_decoded_are_kept() -> None:
    assert http_parts(SWITCHED + b"\x81") == [SWITCHED[:-4], b"\x81"]
    cut = frame(b"0123456789")[:-3]
    assert http_parts(SWITCHED + cut)[-1] == cut
    for header in (b"\x81\x7e\x00", b"\x81\xfe\x00\x10\x01"):  # cut length, cut mask
        assert http_parts(SWITCHED + header)[-1] == header
    broken = frame(b"\x06 reserved block type", rsv1=True)
    assert http_parts(SWITCHED + broken)[-1] == b"\x06 reserved block type"
    assert ws_messages(SWITCHED + broken) == 1
    assert ws_failures(SWITCHED + broken) == 1  # counted, so a spec can require every message decoded
    assert ws_failures(SWITCHED + frame(deflated(b"fine")[0], rsv1=True)) == 0
    assert ws_messages(b"") == 0


def test_main_reports_directories_of_taps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    taps = tmp_path / "taps"
    taps.mkdir()
    (taps / "00001.up").write_bytes(UPGRADE + frame(UNICODE.encode(), mask=b"abcd"))
    (taps / "00001.down").write_bytes(response("Transfer-Encoding: chunked", b"a\r\nevent: x\n\r\n0\r\n\r\n"))
    (taps / "sub").mkdir()
    argv = ["--canary", UNICODE, "--http-dir", f"runner-hub={taps}", "--report"]
    assert main(argv) == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "findings": [{"source": "runner-hub/00001.up", "canary": UNICODE, "encoding": "raw"}],
        "sources": {"runner-hub/00001.down": report["sources"]["runner-hub/00001.down"], "runner-hub/00001.up": 228},
        "sse_frames": 1,
        "ws_messages": 1,
        "ws_failures": 0,
    }
    missing = tmp_path / "none"
    assert main(["--canary", CANARY, "--http-dir", f"none={missing}", "--report"]) == 0
    assert json.loads(capsys.readouterr().out)["sources"] == {}
