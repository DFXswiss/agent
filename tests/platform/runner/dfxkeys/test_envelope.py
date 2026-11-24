"""U-RUN-dfxkey: the envelope of the per-device DFX key (ADR-080 section 7, contract sheet of KOR-50) - sealed by the
issuing platform device, opened only by the org device it names, and every forgery a hub could make is refused:
unsigned, signed by a device that is not a certified platform device, sealed for another device, another KEM key or
another organization."""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from agent_runner.crypto import hpke
from agent_runner.crypto.encoding import b64u_decode, b64u_encode, canonical
from agent_runner.crypto.keys import ed25519_sign
from agent_runner.crypto.log import vectors as v
from agent_runner.crypto.log.platform import verify_platform_log
from agent_runner.model.dfxkey import (
    MAX_ENVELOPE,
    REASONS,
    EnvelopeRefused,
    Holder,
    open_envelope,
    seal_envelope,
    verify_envelope,
)

from ..reconcile.rig import ROOT, device_keys, pid
from .kit import ISSUER, ODEV, ORG, OTHER_ORG, REQUEST, odev_keys, org_book, platform_entries

SUB = "sk-or-v1-SUBKEYMARKER-0123456789abcdef0123456789abcdef"
KEY_REF = "a" * 64
ISSUED_AT = "2026-11-24T12:00:00.000Z"


def header(**changes: str) -> dict[str, str]:
    return {
        "request_id": REQUEST,
        "org_id": ORG,
        "device_id": pid(ODEV),
        "device_kem": odev_keys().kem_public,
        "key_ref": KEY_REF,
        "issued_at": ISSUED_AT,
        "issuer_device_id": ISSUER,
        **changes,
    }


def envelope(key: str = SUB, signer: str = "rdev", **changes: str) -> str:
    return seal_envelope(header(**changes), key, device_keys(signer).signing_key())


def holder() -> Holder:
    return Holder(book=org_book(), device_id=pid(ODEV), keys=odev_keys())


def opened(text: object, *, org_id: str = ORG, key_ref: str = KEY_REF, issuer: str = ISSUER) -> str:
    return open_envelope(holder(), text, org_id=org_id, key_ref=key_ref, issuer=issuer)


def refused(text: object, reason: str, **listed: str) -> None:
    assert reason in REASONS
    with pytest.raises(EnvelopeRefused) as caught:
        opened(text, **listed)
    assert caught.value.reason == reason
    assert str(caught.value) == reason


def document(text: str) -> dict[str, Any]:
    found: dict[str, Any] = json.loads(b64u_decode(text))
    return found


def forge(doc: object) -> str:
    return b64u_encode(canonical(doc))


def signed(head: dict[str, str], ct: bytes, signer: str = "rdev") -> str:
    """A document with a valid signature of `signer` over a ciphertext of the forger's choice."""
    sig = ed25519_sign(device_keys(signer).signing_key(), canonical(head) + ct)
    return forge({"v": 1, "header": head, "ct": b64u_encode(ct), "sig": b64u_encode(sig)})


def test_the_envelope_opens_on_the_device_it_names() -> None:
    text = envelope()
    assert opened(text) == SUB
    parsed = verify_envelope(holder(), text, org_id=ORG, key_ref=KEY_REF, issuer=ISSUER)
    assert parsed.header == header()
    found = document(text)
    assert set(found) == {"v", "header", "ct", "sig"}
    assert found["v"] == 1
    # the key is sealed: it appears neither in the envelope nor in its decoded document
    assert "SUBKEYMARKER" not in text
    assert "SUBKEYMARKER" not in str(found)
    assert "SUBKEYMARKER" not in base64.urlsafe_b64decode(found["ct"] + "==").decode("latin-1")


def test_another_certified_platform_device_may_issue() -> None:
    assert opened(envelope(signer="pdev2", issuer_device_id=pid("pdev2")), issuer=pid("pdev2")) == SUB


def test_the_largest_key_fits_the_envelope_limit() -> None:
    assert len(envelope("k" * 4096)) <= MAX_ENVELOPE
    with pytest.raises(EnvelopeRefused, match="key_invalid"):
        envelope("k" * 4097)
    with pytest.raises(EnvelopeRefused, match="key_invalid"):
        envelope("with space")
    with pytest.raises(EnvelopeRefused, match="malformed"):
        envelope(device_id="not-a-uuid")


@pytest.mark.parametrize(
    ("make", "listed", "reason"),
    [
        # unsigned or signed by anyone but a certified platform device
        (lambda: _resigned(bytes(64)), {}, "signature"),
        (lambda: envelope(signer=ODEV), {}, "signature"),  # the org device's own key
        (lambda: envelope(signer="hub"), {}, "signature"),  # a key the hub holds
        (lambda: envelope(signer="pdev2"), {}, "signature"),  # a certified device's key under another's name
        (
            lambda: envelope(signer="mallory", issuer_device_id=pid("mallory")),
            {"issuer": pid("mallory")},
            "issuer_not_platform",
        ),
        (lambda: envelope(signer="qdev", issuer_device_id=pid("qdev")), {"issuer": pid("qdev")}, "issuer_not_platform"),
        (lambda: envelope(signer=ODEV, issuer_device_id=pid(ODEV)), {"issuer": pid(ODEV)}, "issuer_not_platform"),
        # sealed for someone else
        (lambda: envelope(device_id=pid("odev2")), {}, "other_device"),
        (lambda: envelope(device_kem=device_keys("odev2").kem_public), {}, "other_kem"),
        (lambda: envelope(org_id=OTHER_ORG), {}, "other_org"),
        (lambda: envelope(org_id=OTHER_ORG), {"org_id": OTHER_ORG}, "org_not_served"),
        (lambda: envelope(org_id=pid("unknown/org")), {"org_id": pid("unknown/org")}, "org_not_served"),
        # the hub's listing must match the signed header
        (envelope, {"key_ref": "b" * 64}, "key_ref_mismatch"),
        (envelope, {"issuer": pid("pdev2")}, "issuer_mismatch"),
        # signed correctly, but not openable or not a key
        (
            lambda: signed(
                header(), hpke.seal(b64u_decode(device_keys("odev2").kem_public), canonical(header()), b"k")
            ),
            {},
            "decrypt",
        ),
        (lambda: signed(header(), _sealed(b"bad key")), {}, "key_invalid"),
        (lambda: signed(header(), _sealed(b"k" * 4097)), {}, "key_invalid"),
        (lambda: signed(header(), _sealed("schlüssel".encode())), {}, "key_invalid"),
    ],
)
def test_forged_envelopes_are_refused(make: Any, listed: dict[str, str], reason: str) -> None:
    refused(make(), reason, **listed)


def _sealed(plain: bytes) -> bytes:
    return hpke.seal(b64u_decode(odev_keys().kem_public), canonical(header()), plain)


def _resigned(sig: bytes) -> str:
    found = document(envelope())
    return forge({**found, "sig": b64u_encode(sig)})


def test_without_a_verified_platform_log_nothing_is_accepted() -> None:
    book = org_book()
    book.platform = None
    with pytest.raises(EnvelopeRefused, match="issuer_not_platform"):
        open_envelope(Holder(book, pid(ODEV), odev_keys()), envelope(), org_id=ORG, key_ref=KEY_REF, issuer=ISSUER)


def test_an_issuer_decertified_later_is_refused() -> None:
    entries = platform_entries()
    log = v.PlatformBuilder(entries)
    log.add("device_decertify", device=ISSUER)
    book = org_book(platform=log.entries)
    assert verify_platform_log(log.entries, ROOT).state.status(ISSUER) == "decertified"
    with pytest.raises(EnvelopeRefused, match="issuer_not_platform"):
        open_envelope(Holder(book, pid(ODEV), odev_keys()), envelope(), org_id=ORG, key_ref=KEY_REF, issuer=ISSUER)


def _with(**changes: object) -> str:
    found = document(envelope())
    return forge({**found, **changes})


def _header_with(**changes: object) -> str:
    found = document(envelope())
    return forge({**found, "header": {**found["header"], **changes}})


@pytest.mark.parametrize(
    "make",
    [
        lambda: 42,
        lambda: "",
        lambda: "A" * (MAX_ENVELOPE + 1),
        lambda: "not base64!",
        lambda: b64u_encode(b"{"),
        lambda: b64u_encode(b'{"a":1,"a":2}'),
        lambda: forge([1]),
        lambda: _with(extra="x"),
        lambda: _with(v=2),
        lambda: _with(v=True),
        lambda: _with(v="1"),
        lambda: _with(header="h"),
        lambda: _with(ct="not base64!"),
        lambda: _with(ct=7),
        lambda: _with(sig=b64u_encode(bytes(63))),
        lambda: _header_with(extra="x"),
        lambda: _header_with(key_ref=7),
        lambda: _header_with(key_ref="short"),
        lambda: _header_with(request_id="01930050-0000-7000-8000-00000000000G"),
        lambda: _header_with(device_kem=b64u_encode(bytes(31))),
        lambda: _header_with(issued_at="2026-11-24 12:00:00"),
    ],
)
def test_malformed_envelopes_are_refused(make: Any) -> None:
    refused(make(), "malformed")
