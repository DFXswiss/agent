"""U-HUB-crypto (browser frames): the live frames of a platform browser session of `agent_hub.crypto.browserframe`
(04-crypto.md section 4.13, ADR-085 section 6) and their vectors `contracts/vectors/browserframe.json`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from agent_hub.crypto import browserframe, event, runlog, vectors
from agent_hub.crypto.encoding import P, b64u_decode, b64u_encode
from agent_hub.crypto.errors import CryptoError

CONTRACTS = Path(__file__).resolve().parents[4] / "contracts"
VECTORS = json.loads((CONTRACTS / "vectors" / "browserframe.json").read_text(encoding="utf-8"))
WK = bytes(range(32))
SALT = bytes(32)
BROWSER = "br-00000000000a"
SESSION = "01930044-0000-7000-8000-000000000001"
WG = "01930044-0000-7000-8000-000000000002"
TS = "2026-11-25T12:00:00.000Z"


def code(call: Any) -> str:
    with pytest.raises(CryptoError) as refused:
        call()
    return refused.value.code


def seal(jpeg: bytes = b"\xff\xd8picture\xff\xd9", **changes: Any) -> dict[str, object]:
    args: dict[str, Any] = {"browser": BROWSER, "session": SESSION, "wg": WG, "epoch": 1, "seq": 0, "ts": TS}
    return browserframe.seal_frame(WK, **{**args, **changes}, jpeg=jpeg)


def test_the_vector_file_is_current_and_its_negatives_fail_as_stated() -> None:
    section = next(item for item in vectors.SECTIONS if item.name == "browserframe")
    assert vectors.render(section) == (CONTRACTS / "vectors" / "browserframe.json").read_text(encoding="utf-8")
    for vector in VECTORS["invalid"]:
        assert code(lambda v=vector: vectors.run_op(v["op"], v["args"])) == vector["error"], vector["name"]


def test_the_positive_vector_reproduces_and_opens() -> None:
    wk, salt = b64u_decode(VECTORS["workgroup_key"]), b64u_decode(VECTORS["salt"])
    header = {name: VECTORS[name] for name in ("browser", "session", "wg", "epoch", "seq", "ts")}
    assert b64u_encode(browserframe.frame_key(wk, salt, VECTORS["browser"])) == VECTORS["k_frame"]
    assert browserframe.frame_aad(**header).decode("ascii") == VECTORS["aad"]
    jpeg = b64u_decode(VECTORS["jpeg"])
    nonce = b64u_decode(VECTORS["nonce"])
    assert browserframe.seal_frame(wk, **header, jpeg=jpeg, salt=salt, nonce=nonce) == VECTORS["frame"]
    assert browserframe.check_frame(VECTORS["frame"]) == VECTORS["frame"]
    assert browserframe.open_frame(wk, VECTORS["frame"], session=VECTORS["session"], wg=VECTORS["wg"]) == jpeg
    limits = VECTORS["limits"]
    assert (limits["frame_bytes"], limits["frame_ct_min"], limits["frame_ct_max"]) == (
        browserframe.FRAME_BYTES,
        browserframe.FRAME_CT_MIN,
        browserframe.FRAME_CT_MAX,
    )


def test_a_frame_is_sealed_with_a_fresh_salt_and_opened_again() -> None:
    first, second = seal(seq=5), seal(seq=5)
    assert first["salt"] != second["salt"]
    assert first["ct"] != second["ct"]
    assert set(first) == browserframe.FRAME_KEYS
    assert browserframe.open_frame(WK, first, session=SESSION, wg=WG) == b"\xff\xd8picture\xff\xd9"
    largest = seal(bytes(browserframe.FRAME_BYTES), seq=browserframe.MAX_SEQ, epoch=browserframe.MAX_EPOCH)
    assert len(b64u_decode(largest["ct"])) == browserframe.FRAME_CT_MAX
    assert browserframe.open_frame(WK, largest, session=SESSION, wg=WG) == bytes(browserframe.FRAME_BYTES)


def test_sealing_refusals() -> None:
    assert code(lambda: seal(b"")) == "size_exceeded"
    assert code(lambda: seal(bytes(browserframe.FRAME_BYTES + 1))) == "size_exceeded"
    assert code(lambda: seal(browser="br-1")) == "header_invalid"
    assert code(lambda: seal(session="s")) == "header_invalid"
    assert code(lambda: seal(ts="2026-11-25")) == "header_invalid"
    assert code(lambda: seal(epoch=0)) == "header_invalid"
    assert code(lambda: browserframe.frame_key(WK[:31], SALT, BROWSER)) == "header_invalid"
    assert code(lambda: browserframe.frame_key(WK, SALT[:31], BROWSER)) == "header_invalid"
    assert code(lambda: browserframe.browser_id(7)) == "header_invalid"


def test_the_check_order() -> None:
    frame = seal()
    big = {**frame, "ct": b64u_encode(bytes(browserframe.FRAME_CT_MAX + 1))}
    # the size comes after the formats and after the expected session and workgroup
    assert code(lambda: browserframe.check_frame(big)) == "size_exceeded"
    assert code(lambda: browserframe.check_frame({**big, "seq": True})) == "header_invalid"
    assert code(lambda: browserframe.check_frame(big, session=WG)) == "header_invalid"
    assert code(lambda: browserframe.check_frame(big, wg=SESSION)) == "header_invalid"
    assert browserframe.check_frame(frame, session=SESSION, wg=WG) == frame
    assert code(lambda: browserframe.check_frame([frame])) == "header_invalid"
    assert code(lambda: browserframe.check_frame({**frame, "ct": "é"})) == "non_ascii"
    assert code(lambda: browserframe.open_frame(WK, big, session=SESSION, wg=WG)) == "size_exceeded"
    assert code(lambda: browserframe.open_frame(WK, frame, session=SESSION, wg=SESSION)) == "header_invalid"


def test_a_frame_never_passes_for_another_format() -> None:
    frame = seal(salt=SALT)
    sealed = b64u_decode(frame["ct"])
    key = browserframe.frame_key(WK, SALT, BROWSER)
    assert code(lambda: runlog.open_chunk(key, SESSION, 0, sealed)) == "decrypt_failed"
    assert browserframe.frame_aad(browser=BROWSER, session=SESSION, wg=WG, epoch=1, seq=0, ts=TS).startswith(
        P + b"browser-frame\x00{"
    )
    assert code(lambda: event.verify_event(frame, bytes(32))) == "header_invalid"
