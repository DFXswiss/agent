"""Plaintext scan of the hub side (09-milestones.md M1, 07-testing.md section 2.1 E2E-09): canary values must not
appear in the hub database dump, the hub logs or what crossed the hub's links (the relay taps,
`orchestrator/relay.py`). SCN-09 extends it to the full check of E2E-09: non-ASCII canaries, both directions of a
link (requests as well as responses) and WebSocket frames (the device channel).

A canary is printable text (any script) of at least `MIN_CANARY` characters without surrounding white space. It is
searched in every source as raw UTF-8 text and in the encodings a careless store, log or client would use: the other
Unicode normal form (NFC/NFD), JSON string escapes (`\\uXXXX`, lower and upper case), URL percent-encoding (lower and
upper case), hexadecimal (lower and upper case, e.g. a `bytea` column in the dump) and base64/base64url at the three
byte alignments (the encoded part that does not depend on the neighbouring bytes).

HTTP sources (one file per relay connection and direction) are decoded first: each request's and response's header
and body, with `chunked` transfer coding and `gzip`/`deflate` content coding undone. After a WebSocket upgrade (a
`101` response, or a request with `Upgrade: websocket`) the rest is read as WebSocket frames (RFC 6455): unmasked,
fragments joined, `permessage-deflate` undone with one decompressor per direction (context takeover). An undecodable
rest is scanned as it is; the raw file is always scanned as well.

    python -m checks.plaintext_scan --canary <value> … [--file <label>=<path> …] [--http <label>=<path> …]
                                    [--http-dir <label>=<directory> …] [--report]

prints the findings as JSON (with `--report`: `{findings, sources: {label: bytes}, sse_frames, ws_messages,
ws_failures}`) and
exits 1 when there is one.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import unicodedata
import zlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

MIN_CANARY = 8
_SSE_EVENT_RE = re.compile(rb"^event: [a-z_]+$", re.MULTILINE)
_START_RE = re.compile(rb"HTTP/1\.[01] \d{3}|[A-Z]{3,10} [^ \r\n]+ HTTP/1\.[01]\r\n")
_ESCAPE_RE = re.compile(rb"\\u([0-9a-f]{4})")
_PERCENT_RE = re.compile(rb"%[0-9A-F]{2}")
_WS_TAIL = b"\x00\x00\xff\xff"
_WS_CONTINUATION = 0
_WS_CONTROL = 8
# Responses that never have a body (RFC 9110 section 6.4.1): 1xx (101 is the WebSocket upgrade), 204, 304.
_NO_BODY = frozenset({b"100", b"102", b"103", b"204", b"304"})


@dataclass(frozen=True)
class Finding:
    source: str
    canary: str
    encoding: str


def check_canaries(values: Iterable[object]) -> list[str]:
    out = []
    for value in values:
        if not isinstance(value, str) or len(value) < MIN_CANARY or not value.isprintable() or value != value.strip():
            raise ValueError(
                f"a canary must be printable text of at least {MIN_CANARY} characters without surrounding white "
                f"space: {value!r}"
            )
        out.append(value)
    return out


def _b64_pieces(data: bytes) -> list[tuple[str, bytes]]:
    """The base64 and base64url text of `data` at the offsets 0, 1, 2 that no neighbouring byte influences."""
    out = []
    for offset in range(3):
        padded = b"\0" * offset + data
        start = 4 if offset else 0
        end = (len(padded) // 3) * 4
        for name, encode in (("base64", base64.b64encode), ("base64url", base64.urlsafe_b64encode)):
            # at least MIN_CANARY characters for a canary of at least MIN_CANARY bytes
            out.append((f"{name}+{offset}", encode(padded)[start:end]))
    return out


def _forms(text: str) -> list[tuple[str, bytes]]:
    raw = text.encode("utf-8")
    escaped = json.dumps(text)[1:-1].encode("ascii")
    percent = quote(text, safe="").encode("ascii")
    out = [
        ("raw", raw),
        ("json", escaped),
        ("JSON", _ESCAPE_RE.sub(lambda m: b"\\u" + m.group(1).upper(), escaped)),
        ("PERCENT", percent),
        ("percent", _PERCENT_RE.sub(lambda m: m.group().lower(), percent)),
        ("hex", raw.hex().encode()),
        ("HEX", raw.hex().upper().encode()),
    ]
    return out + _b64_pieces(raw)


def encodings(canary: str) -> list[tuple[str, bytes]]:
    """Every distinct encoding of the canary and of its other normal form (suffix `/nfc` or `/nfd`)."""
    variants = [("", canary)]
    for form in ("NFC", "NFD"):
        other = unicodedata.normalize(form, canary)
        if other != canary:
            variants.append((f"/{form.lower()}", other))
    out: list[tuple[str, bytes]] = []
    seen: set[bytes] = set()
    for suffix, text in variants:
        for name, value in _forms(text):
            if value not in seen:
                seen.add(value)
                out.append((name + suffix, value))
    return out


def _headers(block: bytes) -> dict[str, str]:
    out = {}
    for line in block.split(b"\r\n")[1:]:
        name, _, value = line.partition(b":")
        out[name.strip().lower().decode("latin-1")] = value.strip().decode("latin-1").lower()
    return out


def _dechunk(data: bytes, at: int) -> tuple[bytes, int]:
    """The body of a `chunked` message starting at `at` and the position after it. A message cut off (the relay
    closed the connection during a stream) yields what arrived, including a partial last chunk."""
    body = bytearray()
    while True:
        end = data.find(b"\r\n", at)
        if end < 0:
            return bytes(body), len(data)
        size = int(data[at:end].split(b";", 1)[0], 16)
        at = end + 2
        if size == 0:
            trailer = data.find(b"\r\n\r\n", at - 2)
            return bytes(body), len(data) if trailer < 0 else trailer + 4
        body += data[at : at + size]
        if at + size >= len(data):
            return bytes(body), len(data)
        at += size + 2


def _decode(body: bytes, coding: str) -> bytes:
    if coding == "gzip":
        return zlib.decompress(body, 16 + zlib.MAX_WBITS)
    if coding == "deflate":
        return zlib.decompress(body)
    return body


def _unmask(payload: bytes, key: bytes) -> bytes:
    mask = (key * (len(payload) // 4 + 1))[: len(payload)]
    return (int.from_bytes(payload, "big") ^ int.from_bytes(mask, "big")).to_bytes(len(payload), "big")


def _ws_frame(data: bytes, at: int) -> tuple[int, bool, bool, bytes, int] | None:
    """(opcode, fin, rsv1, unmasked payload, position after the frame) of the frame at `at`, None when cut."""
    if len(data) - at < 2:
        return None
    first, second = data[at], data[at + 1]
    length, pos = second & 0x7F, at + 2
    if length >= 126:
        size = 2 if length == 126 else 8
        if len(data) < pos + size:
            return None
        length, pos = int.from_bytes(data[pos : pos + size], "big"), pos + size
    key = b""
    if second & 0x80:
        key, pos = data[pos : pos + 4], pos + 4
    end = pos + length
    if end > len(data) or len(key) not in (0, 4):
        return None
    payload = data[pos:end]
    return first & 0x0F, bool(first & 0x80), bool(first & 0x40), _unmask(payload, key) if key else payload, end


def _ws_messages(data: bytes) -> tuple[list[bytes], int, int]:
    """The payloads of the WebSocket frames in `data` (data messages joined and inflated, control frames as they are),
    the number of data messages and the number of compressed messages that could not be inflated (after the first
    failure the shared window is lost, so every later compressed message of the connection fails too); a cut rest is
    kept as it is."""
    parts: list[bytes] = []
    count = failures = 0
    inflate = zlib.decompressobj(-zlib.MAX_WBITS)
    message = bytearray()
    compressed = False
    at = 0
    while at < len(data):
        frame = _ws_frame(data, at)
        if frame is None:
            parts.append(data[at:])
            break
        opcode, fin, rsv1, payload, at = frame
        if opcode >= _WS_CONTROL:
            parts.append(payload)
            continue
        if opcode != _WS_CONTINUATION:
            message, compressed = bytearray(), rsv1
        message += payload
        if fin:
            count += 1
            whole = bytes(message)
            if compressed:
                try:
                    whole = inflate.decompress(whole + _WS_TAIL)
                except zlib.error:
                    failures += 1
            parts.append(whole)
    return parts, count, failures


def _messages(data: bytes) -> tuple[list[bytes], int, int]:
    """Header blocks and decoded bodies of the HTTP/1.x requests and responses in `data`, then the WebSocket payloads
    after an upgrade; an undecodable rest stays as it is. Also the number of WebSocket data messages and of those
    that could not be inflated."""
    parts: list[bytes] = []
    at = 0
    while at < len(data):
        head_end = data.find(b"\r\n\r\n", at)
        if not _START_RE.match(data, at) or head_end < 0:
            parts.append(data[at:])
            break
        block = data[at:head_end]
        parts.append(block)
        headers = _headers(block)
        start = head_end + 4
        response = data.startswith(b"HTTP/", at)
        if (response and block[9:12] == b"101") or headers.get("upgrade") == "websocket":
            frames, count, failures = _ws_messages(data[start:])
            return parts + frames, count, failures
        try:
            if headers.get("transfer-encoding") == "chunked":
                body, at = _dechunk(data, start)
            elif "content-length" in headers:
                at = start + int(headers["content-length"])
                body = data[start:at]
            elif not response or block[9:12] in _NO_BODY:
                body, at = b"", start
            else:
                body, at = data[start:], len(data)
            parts.append(_decode(body, headers.get("content-encoding", "")))
        except (ValueError, zlib.error):
            parts.append(data[start:])
            break
    return parts, 0, 0


def http_parts(data: bytes) -> list[bytes]:
    """Header blocks, decoded bodies and WebSocket payloads of the HTTP/1.x traffic in `data`."""
    return _messages(data)[0]


def ws_messages(data: bytes) -> int:
    """Number of WebSocket data messages in the HTTP traffic `data`."""
    return _messages(data)[1]


def ws_failures(data: bytes) -> int:
    """Number of compressed WebSocket messages in `data` that could not be inflated (scanned only as sent)."""
    return _messages(data)[2]


def scan(sources: Mapping[str, bytes], canaries: Sequence[str]) -> list[Finding]:
    """Every (source, canary, encoding) found; each source label once per canary and encoding."""
    found: list[Finding] = []
    table = {canary: encodings(canary) for canary in check_canaries(canaries)}
    for label, data in sources.items():
        for canary, variants in table.items():
            found += [Finding(label, canary, name) for name, value in variants if value in data]
    return found


def http_source(data: bytes) -> bytes:
    """The raw bytes followed by the decoded parts (both scanned)."""
    return b"\n".join([data, *http_parts(data)])


def sse_frames(data: bytes) -> int:
    """Number of server-sent event frames (`event: <type>` lines) in decoded HTTP data."""
    return len(_SSE_EVENT_RE.findall(b"\n".join(http_parts(data))))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m checks.plaintext_scan")
    parser.add_argument("--canary", action="append", default=[], required=True)
    parser.add_argument("--file", action="append", default=[], metavar="LABEL=PATH")
    parser.add_argument("--http", action="append", default=[], metavar="LABEL=PATH")
    parser.add_argument("--http-dir", action="append", default=[], metavar="LABEL=DIRECTORY")
    parser.add_argument("--report", action="store_true", help="print sources and frame counts as well")
    args = parser.parse_args(argv)
    sources: dict[str, bytes] = {}
    frames = {"sse_frames": 0, "ws_messages": 0, "ws_failures": 0}
    https = [item.partition("=")[::2] for item in args.http]
    for spec in args.http_dir:
        label, _, directory = spec.partition("=")
        root = Path(directory).expanduser()
        files = sorted(path for path in root.iterdir() if path.is_file()) if root.is_dir() else []
        https += [(f"{label}/{path.name}", str(path)) for path in files]
    for label, path in (item.partition("=")[::2] for item in args.file):
        sources[label] = Path(path).expanduser().read_bytes()
    for label, path in https:
        data = Path(path).expanduser().read_bytes()
        frames["sse_frames"] += sse_frames(data)
        frames["ws_messages"] += ws_messages(data)
        frames["ws_failures"] += ws_failures(data)
        sources[label] = http_source(data)
    findings = [asdict(finding) for finding in scan(sources, args.canary)]
    if args.report:
        print(json.dumps({"findings": findings, "sources": {k: len(v) for k, v in sources.items()}, **frames}))
    else:
        print(json.dumps(findings))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
