"""Copied from tests/platform/hub/crypto/test_formats.py.

U-HUB-crypto: bundle, wrap, event, credential, device request/response, invitation, recovery, enrollment and dedupe
formats (04-crypto.md sections 3, 4.1-4.6, 4.10, 5.5, 6.4 E1/E2/E4/E8)."""

from __future__ import annotations

import os
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes, hmac
from cryptography.hazmat.primitives import hpke as pyca_hpke
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from agent_runner.crypto import (
    aead,
    bundle,
    credential,
    dedupe,
    encoding,
    enroll,
    event,
    fields,
    hpke,
    ids,
    invite,
    kdf,
    recovery,
    request,
    signed,
    wrap,
)
from agent_runner.crypto.encoding import P, b64u_decode, b64u_encode, canonical
from agent_runner.crypto.errors import CryptoError
from agent_runner.crypto.keys import Identity

SUITE = pyca_hpke.Suite(pyca_hpke.KEM.X25519, pyca_hpke.KDF.HKDF_SHA256, pyca_hpke.AEAD.AES_256_GCM)
TS = "2026-11-21T12:00:00.000Z"
ORG, WG, TASK, SESSION, USER, DEVICE = (ids.uuid7() for _ in range(6))


def refused(code: str) -> pytest.RaisesExc[CryptoError]:
    """`pytest.raises` for exactly this error code."""
    return pytest.raises(CryptoError, match=f"^{code}$")


# --- fields ---


def test_field_checks() -> None:
    assert fields.uuid_or_empty("") == ""
    assert fields.kind("session.output") == "session.output"
    assert fields.kind("a") == "a"
    for bad in ("Session", "a..b", "a.", "x" * 65, 3, "a-b"):
        with refused("header_invalid"):
            fields.kind(bad)
    fields.literal(1, 1)
    for value in (True, 1.0, 2):
        with refused("header_invalid"):
            fields.literal(value, 1)
    with refused("non_ascii"):
        fields.header_dict({"a": "ü"}, frozenset({"a"}))


# --- bundle (4.1) ---


def test_bundle_round_trip_in_both_slots() -> None:
    ident, cred_id = Identity.generate(), os.urandom(16)
    kek = kdf.kek_from_prf(os.urandom(32), cred_id)
    slot = bundle.passkey_slot(cred_id)
    assert slot == "passkey:" + b64u_encode(cred_id)
    sealed = bundle.seal_bundle(kek, USER, slot, ident)
    assert bundle.open_bundle(kek, USER, slot, sealed).public() == ident.public()
    raw = b64u_decode(sealed)
    nonce, aad = raw[:12], P + b"bundle\x00" + USER.encode() + b"\x00" + slot.encode()
    assert aead.open_(kek, nonce, raw[12:], aad) == bundle.bundle_plaintext(ident)
    rec = kdf.kek_from_recovery_code(os.urandom(16))
    sealed_rec = bundle.seal_bundle(rec, USER, "recovery", ident, bytes(12))
    assert bundle.open_bundle(rec, USER, "recovery", sealed_rec).public() == ident.public()
    with refused("decrypt_failed"):
        bundle.open_bundle(kek, USER, "recovery", sealed)
    with refused("decrypt_failed"):
        bundle.open_bundle(kek, DEVICE, slot, sealed)


def test_bundle_plaintext_is_canonical() -> None:
    ident = Identity.generate()
    text = bundle.bundle_plaintext(ident)
    assert text.startswith(b'{"kem":"')
    assert text.endswith(b'","v":1}')
    assert bundle.parse_bundle_plaintext(text).public() == ident.public()
    data = encoding.parse_json(text)
    assert isinstance(data, dict)
    for bad in (
        b'{"kem":"' + bytes(data["kem"], "ascii") + b'","sig":"' + bytes(data["sig"], "ascii") + b'","v":2}',
        b'{"kem":"' + bytes(data["kem"], "ascii") + b'","sig":"' + bytes(data["sig"], "ascii") + b'","v":true}',
        b'{"sig":"' + bytes(data["sig"], "ascii") + b'","kem":"' + bytes(data["kem"], "ascii") + b'","v":1}',
        b'{"kem":"' + bytes(data["kem"], "ascii") + b'","v":1}',
        canonical({"kem": b64u_encode(bytes(31)), "sig": data["sig"], "v": 1}),
    ):
        with refused("header_invalid"):
            bundle.parse_bundle_plaintext(bad)


def test_bundle_slot_and_aad_rules() -> None:
    assert bundle.check_slot("recovery") == "recovery"
    for bad in ("passkey:", "passkey:a=", "passkey:Zh", "pass", 5, "Recovery"):
        with refused("header_invalid"):
            bundle.check_slot(bad)
    with refused("header_invalid"):
        bundle.passkey_slot(b"")
    with refused("non_ascii"):
        bundle.bundle_aad("usér", "recovery")
    for user in ("", None):
        with refused("header_invalid"):
            bundle.bundle_aad(user, "recovery")  # type: ignore[arg-type]
    with refused("header_invalid"):
        bundle.seal_bundle(bytes(16), USER, "recovery", Identity.generate())


# --- wrap (4.2) ---


def _wrap_args(recipient: str = USER, epoch: int = 2) -> dict[str, Any]:
    return {"scope": "wg", "scope_id": WG, "epoch": epoch, "recipient": recipient}


def test_wrap_round_trip_and_binding() -> None:
    member, wrapper = Identity.generate(), Identity.generate()
    key = os.urandom(32)
    sealed = wrap.wrap_key(member.kem_public(), key=key, **_wrap_args())
    assert wrap.unwrap_key(member.kem, wrap=sealed, **_wrap_args()) == key
    info = P + b"wrap\x00" + canonical({"epoch": 2, "recipient": USER, "scope": "wg", "scope_id": WG})
    assert wrap.wrap_info("wg", WG, 2, USER) == info
    assert SUITE.decrypt(b64u_decode(sealed), member.kem, info=info) == key
    for changed in (_wrap_args(epoch=3), _wrap_args(recipient=DEVICE), {**_wrap_args(), "scope": "org"}):
        with refused("decrypt_failed"):
            wrap.unwrap_key(member.kem, wrap=sealed, **changed)
    with refused("hpke_short"):
        wrap.unwrap_key(member.kem, wrap=b64u_encode(b64u_decode(sealed)[:40]), **_wrap_args())
    sig = wrap.sign_wrap(wrapper.sig, wrap=sealed, **_wrap_args())
    wrap.verify_wrap(wrapper.sig_public(), wrap=sealed, signature=sig, **_wrap_args())
    text = P + b"wrap\x00" + canonical({"epoch": 2, "recipient": USER, "scope": "wg", "scope_id": WG, "wrap": sealed})
    assert wrap.wrap_signature_text("wg", WG, 2, USER, sealed) == text
    with refused("signature_invalid"):
        wrap.verify_wrap(wrapper.sig_public(), wrap=sealed, signature=sig, **_wrap_args(epoch=3))
    with refused("signature_invalid"):
        wrap.verify_wrap(member.sig_public(), wrap=sealed, signature=sig, **_wrap_args())


def test_wrap_field_rules() -> None:
    member = Identity.generate()
    for scope, scope_id, epoch, recipient in (
        ("team", WG, 1, USER),
        ("wg", "", 1, USER),
        ("wg", WG, 1, ""),
        ("wg", WG, 0, USER),
        ("wg", WG, True, USER),
        ("wg", 5, 1, USER),
        ("wg", WG, 1, None),
    ):
        with refused("header_invalid"):
            wrap.wrap_info(scope, scope_id, epoch, recipient)  # type: ignore[arg-type]
    assert wrap.wrap_info("cred", USER, 1, DEVICE).startswith(P + b"wrap\x00")
    with refused("header_invalid"):
        wrap.wrap_key(member.kem_public(), key=bytes(16), **_wrap_args())
    with refused("header_invalid"):
        wrap.wrap_signature_text("wg", WG, 1, USER, "a=")
    sealed = b64u_encode(hpke.seal(member.kem_public(), wrap.wrap_info("wg", WG, 2, USER), bytes(31)))
    with refused("decrypt_failed"):
        wrap.unwrap_key(member.kem, wrap=sealed, **_wrap_args())


# --- events (4.3) ---


def _header(**changes: object) -> dict[str, object]:
    base: dict[str, Any] = {
        "id": ids.uuid7(),
        "org": ORG,
        "scope": "wg",
        "scope_id": WG,
        "epoch": 3,
        "author": USER,
        "kind": "prompt",
        "ts": TS,
        "lh": 0,
        "wg": WG,
        "task": TASK,
        "session": "",
        "ref": "",
    }
    base.update(changes)
    return event.new_header(**base)


def test_event_round_trip() -> None:
    author, key = Identity.generate(), os.urandom(32)
    fields = _header()
    assert set(fields) == event.HEADER_KEYS - {"salt", "nonce"}
    sealed = event.seal_event(key, fields, "Grüsse".encode(), author.sig)
    assert set(sealed) == {"header", "ct", "sig"}
    header = sealed["header"]
    assert isinstance(header, dict)
    assert header == {**fields, "salt": header["salt"], "nonce": header["nonce"]}
    assert event.verify_event(sealed, author.sig_public()) == header
    assert event.open_event(key, sealed, author.sig_public()) == "Grüsse".encode()
    ct = b64u_decode(sealed["ct"])
    k_event = kdf.hkdf(key, P + b"event", salt=b64u_decode(header["salt"]))
    assert aead.open_(k_event, b64u_decode(header["nonce"]), ct, canonical(header)) == "Grüsse".encode()
    text = P + b"event-sig\x00" + canonical(header) + ct
    assert sealed["sig"] == b64u_encode(author.sig.sign(text))
    with refused("decrypt_failed"):
        event.open_event(os.urandom(32), sealed, author.sig_public())
    with refused("signature_invalid"):
        event.verify_event(sealed, Identity.generate().sig_public())


def test_event_org_scope_and_refs() -> None:
    header = _header(scope="org", scope_id=ORG, wg="", task="", kind="org.profile", ref=ids.uuid7())
    assert header["scope_id"] == ORG
    _header(scope="org", scope_id=ORG, wg=WG, kind="wg.profile")
    _header(ref="task:" + TASK, session=SESSION)
    for changes in (
        {"scope": "org", "scope_id": WG},
        {"scope": "wg", "scope_id": ORG},
        {"scope": "wg", "wg": ""},
        {"scope": "task"},
        {"ref": "with space"},
        {"ref": 5},
        {"ref": "x" * 201},
        {"session": "s-1"},
        {"task": TASK.upper()},
        {"author": ""},
        {"id": "x"},
        {"org": ""},
        {"epoch": 0},
        {"lh": -1},
        {"lh": True},
        {"ts": "2026-11-21T12:00:00Z"},
        {"kind": "Prompt"},
        {"scope": "org", "scope_id": ORG, "wg": WG, "kind": "org.profile"},
    ):
        with refused("header_invalid"):
            _header(**changes)


def test_event_header_key_set_and_values() -> None:
    header = event.seal_event(os.urandom(32), _header(), b"", Identity.generate().sig)["header"]
    assert isinstance(header, dict)
    for bad in (
        {**header, "salt": b64u_encode(bytes(31))},
        {**header, "nonce": b64u_encode(bytes(16))},
        {k: v for k, v in header.items() if k != "ref"},
        {**header, "seq": 1},
        {**header, "v": 2},
        {**header, "v": True},
        {**header, "alg": "A256GCM"},
        {**header, "epoch": 1.0},
        [header],
    ):
        with refused("header_invalid"):
            event.check_header(bad)
    with refused("non_ascii"):
        event.check_header({**header, "kind": "prömpt", "epoch": 0})


def test_event_structure_and_size() -> None:
    author, key = Identity.generate(), os.urandom(32)
    sealed = event.seal_event(key, _header(), b"x", author.sig)
    pk = author.sig_public()
    for bad in ({**sealed, "extra": 1}, {"header": sealed["header"], "ct": sealed["ct"]}, "x"):
        with refused("header_invalid"):
            event.verify_event(bad, pk)
    with refused("header_invalid"):
        event.verify_event({**sealed, "ct": "a="}, pk)
    with refused("signature_invalid"):
        event.verify_event({**sealed, "sig": 5}, pk)
    terminal = _header(kind="session.terminal", session=SESSION)
    assert event.max_ct_bytes(terminal) == 64 * 1024
    assert event.max_ct_bytes(_header()) == 256 * 1024
    event.seal_event(key, terminal, bytes(64 * 1024 - 16), author.sig)
    with refused("size_exceeded"):
        event.seal_event(key, terminal, bytes(64 * 1024 - 15), author.sig)
    big = event.seal_event(key, _header(), bytes(256 * 1024 - 16), author.sig)
    event.verify_event(big, pk)
    with refused("size_exceeded"):
        event.seal_event(key, _header(), bytes(256 * 1024 - 15), author.sig)
    big_header = big["header"]
    assert isinstance(big_header, dict)
    with refused("size_exceeded"):
        event.verify_event({**big, "header": {**big_header, "kind": "session.terminal", "session": SESSION}}, pk)


def test_every_seal_draws_a_fresh_salt_and_nonce() -> None:
    author, key, fields = Identity.generate(), os.urandom(32), _header()
    first = event.seal_event(key, fields, b"first", author.sig)
    second = event.seal_event(key, fields, b"edited", author.sig)
    one, two = first["header"], second["header"]
    assert isinstance(one, dict)
    assert isinstance(two, dict)
    assert one["salt"] != two["salt"]
    assert one["nonce"] != two["nonce"]
    assert len(b64u_decode(one["salt"])) == 32
    assert len(b64u_decode(one["nonce"])) == 12
    for header in (one, {**fields, "salt": one["salt"]}, {**fields, "nonce": one["nonce"]}, [fields]):
        with refused("header_invalid"):
            event.seal_event(key, header, b"x", author.sig)
    fixed = event.seal_event(key, fields, b"x", author.sig, fixed_salt_nonce=(bytes(32), bytes(12)))
    assert fixed == event.seal_event(key, fields, b"x", author.sig, fixed_salt_nonce=(bytes(32), bytes(12)))
    with refused("header_invalid"):
        signed.check_fields(event.FORMAT, {**fields, "v": 2})


# --- credential records (4.4) ---


def _cred_header(**changes: object) -> dict[str, object]:
    base: dict[str, Any] = {
        "id": ids.uuid7(),
        "owner": USER,
        "kind": "github",
        "version": 1,
        "epoch": 1,
        "author": USER,
        "ts": TS,
    }
    base.update(changes)
    return credential.new_header(**base)


def test_credential_round_trip_and_refusals() -> None:
    owner, ck = Identity.generate(), os.urandom(32)
    fields = _cred_header()
    assert set(fields) == credential.HEADER_KEYS - {"salt", "nonce"}
    body = encoding.body_json({"token": "t", "login": "ü"})
    record = credential.seal_credential(ck, fields, body, owner.sig)
    header = record["header"]
    assert isinstance(header, dict)
    assert credential.verify_credential(record, owner.sig_public()) == header
    assert credential.open_credential(ck, record, owner.sig_public()) == body
    ct = b64u_decode(record["ct"])
    k_cred = kdf.hkdf(ck, P + b"credential", salt=b64u_decode(header["salt"]))
    assert aead.open_(k_cred, b64u_decode(header["nonce"]), ct, canonical(header)) == body
    assert record["sig"] == b64u_encode(owner.sig.sign(P + b"credential-sig\x00" + canonical(header) + ct))
    with refused("decrypt_failed"):
        credential.open_credential(os.urandom(32), record, owner.sig_public())
    with refused("signature_invalid"):
        credential.verify_credential({**record, "header": {**header, "version": 2}}, owner.sig_public())
    for kind in sorted(credential.KINDS):
        _cred_header(kind=kind)
    for changes in ({"kind": "gitlab"}, {"version": 0}, {"epoch": 0}, {"owner": ""}, {"author": "x"}, {"ts": ""}):
        with refused("header_invalid"):
            _cred_header(**changes)
    with refused("header_invalid"):
        credential.check_header({**header, "v": 2})
    with refused("header_invalid"):
        credential.check_header({**header, "alg": "x"})
    with refused("size_exceeded"):
        credential.seal_credential(ck, fields, bytes(1024 * 1024), owner.sig)
    again = credential.seal_credential(ck, fields, body, owner.sig)
    assert again["header"] != header


# --- device requests (4.5) ---


def _req_header(**changes: object) -> dict[str, object]:
    header: dict[str, object] = {
        "v": 1,
        "id": ids.uuid7(),
        "user": USER,
        "device": DEVICE,
        "kind": "claude.login",
        "follows": "",
        "ts": TS,
        "enc_alg": "HPKE-X25519-A256GCM",
    }
    header.update(changes)
    return header


def _resp_header(**changes: object) -> dict[str, object]:
    header: dict[str, object] = {
        "v": 1,
        "id": ids.uuid7(),
        "user": USER,
        "device": DEVICE,
        "kind": "claude.login",
        "in_reply_to": ids.uuid7(),
        "seq": 1,
        "final": False,
        "ts": TS,
        "enc_alg": "HPKE-X25519-A256GCM",
    }
    header.update(changes)
    return header


def test_request_and_response_round_trip() -> None:
    user, device = Identity.generate(), Identity.generate()
    header = _req_header(follows=ids.uuid7())
    envelope = request.seal_request(header, device.kem_public(), b'{"code":"x"}', user.sig)
    assert request.verify_request(envelope, user.sig_public()) == header
    assert request.open_request(device.kem, envelope, user.sig_public()) == b'{"code":"x"}'
    ct = b64u_decode(envelope["ct"])
    info = P + b"request-wrap\x00" + canonical(header)
    assert SUITE.decrypt(ct, device.kem, info=info) == b'{"code":"x"}'
    assert envelope["sig"] == b64u_encode(user.sig.sign(P + b"request-sig\x00" + canonical(header) + ct))
    response = request.seal_response(_resp_header(final=True, seq=2), user.kem_public(), b"{}", device.sig)
    assert request.verify_response(response, device.sig_public())["final"] is True
    assert request.open_response(user.kem, response, device.sig_public()) == b"{}"
    with refused("decrypt_failed"):
        request.open_request(user.kem, envelope, user.sig_public())
    with refused("signature_invalid"):
        request.verify_request(envelope, device.sig_public())
    with refused("header_invalid"):
        request.verify_request(response, device.sig_public())
    with refused("header_invalid"):
        request.verify_response(envelope, user.sig_public())


def test_request_header_rules() -> None:
    for changes in (
        {"v": 2},
        {"enc_alg": "HPKE"},
        {"id": ""},
        {"user": "u"},
        {"device": ""},
        {"kind": "Claude"},
        {"ts": TS[:-1]},
        {"follows": "x"},
    ):
        with refused("header_invalid"):
            request.check_request_header(_req_header(**changes))
    for changes in ({"seq": 0}, {"final": 1}, {"in_reply_to": ""}):
        with refused("header_invalid"):
            request.check_response_header(_resp_header(**changes))


def test_request_structure_and_size() -> None:
    user, device = Identity.generate(), Identity.generate()
    envelope = request.seal_request(_req_header(), device.kem_public(), b"", user.sig)
    with refused("header_invalid"):
        request.verify_request({**envelope, "x": 1}, user.sig_public())
    with refused("header_invalid"):
        request.verify_request({**envelope, "ct": "Zh"}, user.sig_public())
    with refused("size_exceeded"):
        request.seal_request(_req_header(), device.kem_public(), bytes(1024 * 1024), user.sig)
    huge = {**envelope, "ct": b64u_encode(bytes(1024 * 1024 + 1))}
    with refused("size_exceeded"):
        request.verify_request(huge, user.sig_public())
    header = _req_header()
    short = b"\x01" * 47
    sig = b64u_encode(user.sig.sign(request.signature_text(header, short)))
    with refused("hpke_short"):
        request.open_request(device.kem, {"header": header, "ct": b64u_encode(short), "sig": sig}, user.sig_public())
    fixed = X25519PrivateKey.generate()
    again = request.seal_request(header, device.kem_public(), b"x", user.sig, fixed)
    assert again == request.seal_request(header, device.kem_public(), b"x", user.sig, fixed)


# --- invitations (3, 4.6) ---


def _package(**changes: object) -> dict[str, object]:
    package: dict[str, object] = {
        "org": ORG,
        "org_name": "Zürich",
        "wg_names": {WG: "Allgemein"},
        "founder": b64u_encode(os.urandom(32)),
        "head": {"seq": 0, "hash": b64u_encode(os.urandom(32))},
        "keys": [{"scope": "org", "scope_id": ORG, "epoch": 1, "key": b64u_encode(os.urandom(32))}],
        "hub_key": {"id": "hub-key-1", "public_key": b64u_encode(os.urandom(32))},
        "v": 1,
    }
    package.update(changes)
    return package


def test_code_text_round_trip() -> None:
    code = invite.generate_code()
    text = invite.format_code(code)
    assert len(text) == 30
    assert [len(group) for group in text.split("-")] == [5, 5, 5, 5, 6]
    assert invite.parse_code(text) == code
    assert invite.parse_code(" " + text.lower().replace("-", " ") + " ") == code
    with refused("invite_code_length"):
        invite.format_code(bytes(15))


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA", "invite_code_length"),
        ("AAAAA-AAAAA-AAAAA-AAAAA-AAAAAAA", "invite_code_length"),
        ("", "invite_code_length"),
        ("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA1", "invite_code_invalid"),
        ("AAAAA-AAAAA-AAAAA-AAAAA-AAAAAB", "invite_code_invalid"),
        ("AAAAA_AAAAA-AAAAA-AAAAA-AAAAAA", "invite_code_invalid"),
        ("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA=", "invite_code_invalid"),
        ("\u017fAAAA-AAAAA-AAAAA-AAAAA-AAAAAA", "invite_code_invalid"),
        ("AAAAA-AAAAA-AAAAA-AAAAA-AAAAA\u0131", "invite_code_invalid"),
        (None, "invite_code_invalid"),
    ],
)
def test_code_text_refusals(text: object, code: str) -> None:
    with refused(code):
        invite.parse_code(text)


def test_invite_secrets_and_package() -> None:
    code = invite.generate_code()
    secrets = invite.invite_secrets(code)
    assert secrets.invite_id == b64u_encode(kdf.invite_id_raw(code)) == invite.invite_id(code)
    assert len(b64u_decode(secrets.invite_id)) == 16
    assert secrets.package_key == kdf.package_key(code)
    assert b64u_decode(secrets.pk_inv) == secrets.signing.public_key().public_bytes_raw()
    package = _package()
    sealed = invite.seal_package(secrets, package)
    assert invite.open_package(secrets, sealed) == package
    raw = b64u_decode(sealed)
    aad = P + b"invite-package\x00" + secrets.invite_id.encode()
    assert aead.open_(secrets.package_key, raw[:12], raw[12:], aad) == encoding.body_json(package)
    other = invite.invite_secrets(invite.generate_code())
    with refused("invite_code_invalid"):
        invite.open_package(other, sealed)
    with refused("invite_code_invalid"):
        invite.open_package(secrets, b64u_encode(raw[:20]))
    with refused("invite_code_length"):
        invite.invite_secrets(bytes(15))
    with refused("header_invalid"):
        invite.package_aad("short")


def test_package_rules() -> None:
    wg_key = {"scope": "wg", "scope_id": WG, "epoch": 2, "key": b64u_encode(bytes(32))}
    invite.check_package(_package(keys=[wg_key], wg_names={}))
    package_changes: list[dict[str, object]] = [
        {"v": 2},
        {"org": "o"},
        {"org_name": 5},
        {"wg_names": []},
        {"wg_names": {WG: 5}},
        {"wg_names": {"wg": "x"}},
        {"founder": b64u_encode(bytes(31))},
        {"head": {"seq": -1, "hash": b64u_encode(bytes(32))}},
        {"head": {"seq": 0}},
        {"head": {"seq": 0, "hash": "x"}},
        {"keys": []},
        {"keys": "k"},
        {"keys": [{**wg_key, "scope": "cred"}]},
        {"keys": [{**wg_key, "scope_id": "w"}]},
        {"keys": [{**wg_key, "epoch": 0}]},
        {"keys": [{**wg_key, "key": b64u_encode(bytes(16))}]},
        {"keys": [{**wg_key, "extra": 1}]},
        {"hub_key": {"id": "Hub", "public_key": b64u_encode(bytes(32))}},
        {"hub_key": {"id": 1, "public_key": b64u_encode(bytes(32))}},
        {"hub_key": {"id": "hub", "public_key": "x"}},
        {"hub_key": {"id": "hub"}},
    ]
    for changes in package_changes:
        with refused("header_invalid"):
            invite.check_package(_package(**changes))
    with refused("header_invalid"):
        invite.check_package({k: v for k, v in _package().items() if k != "hub_key"})
    secrets = invite.invite_secrets(invite.generate_code())
    with refused("header_invalid"):
        invite.seal_package(secrets, _package(v=2))
    bad = aead.seal_blob(secrets.package_key, b"[]", invite.package_aad(secrets.invite_id))
    with refused("header_invalid"):
        invite.open_package(secrets, b64u_encode(bad))


# --- recovery (3, 5.5) ---


def test_recovery_code_and_proof() -> None:
    code = recovery.generate_recovery_code()
    text = recovery.format_recovery_code(code)
    assert recovery.parse_recovery_code(text) == code
    assert recovery.recovery_id(code) == b64u_encode(kdf.recovery_id_raw(code))
    assert recovery.recovery_id(code) != invite.invite_id(code)
    with refused("recovery_code_length"):
        recovery.format_recovery_code(bytes(17))
    with refused("recovery_code_length"):
        recovery.parse_recovery_code(text[:-1])
    with refused("invite_code_invalid"):
        recovery.parse_recovery_code("8" + text[1:])
    with refused("recovery_code_length"):
        recovery.recovery_id(bytes(15))
    ident, challenge = Identity.generate(), os.urandom(32)
    proof = recovery.sign_proof(ident.sig, USER, challenge)
    assert recovery.proof_text(USER, challenge) == P + b"recovery-proof\x00" + USER.encode() + b"\x00" + challenge
    recovery.verify_proof(ident.sig_public(), USER, challenge, proof)
    with refused("signature_invalid"):
        recovery.verify_proof(ident.sig_public(), USER, os.urandom(32), proof)
    with refused("header_invalid"):
        recovery.proof_text(USER, bytes(31))
    with refused("non_ascii"):
        recovery.proof_text("ü", challenge)
    with refused("header_invalid"):
        recovery.proof_text("", challenge)


# --- enrollment (3) ---


def test_enrollment_text_and_signatures() -> None:
    device = Identity.generate()
    secrets = invite.invite_secrets(invite.generate_code())
    body = {"device_id": DEVICE, "kind": "platform", "invite_id": secrets.invite_id, "pub": device.public()}
    text = enroll.enrollment_text(body)
    assert text == P + b"enroll\x00" + canonical(body)
    signed_body = enroll.sign_enrollment(body, device.sig, secrets.signing)
    assert enroll.enrollment_text(signed_body) == text
    assert signed_body["enroll_sig"] == b64u_encode(device.sig.sign(text))
    assert signed_body["inv_sig"] == b64u_encode(secrets.signing.sign(text))
    pk_inv = b64u_decode(secrets.pk_inv)
    enroll.verify_enrollment(signed_body, pk_inv)
    resigned = enroll.sign_enrollment(signed_body, device.sig, secrets.signing)
    assert resigned == signed_body
    with refused("signature_invalid"):
        enroll.verify_enrollment(signed_body, device.sig_public())
    with refused("signature_invalid"):
        enroll.verify_enrollment({**signed_body, "os": "linux"}, pk_inv)
    with refused("signature_invalid"):
        enroll.verify_enrollment({k: v for k, v in signed_body.items() if k != "inv_sig"}, pk_inv)
    with refused("header_invalid"):
        enroll.sign_enrollment(body, Identity.generate().sig, secrets.signing)
    bad_bodies: list[object] = [
        [],
        {**body, "pub": {"sig": device.public()["sig"]}},
        {**body, "pub": {**device.public(), "kem": "x"}},
    ]
    for bad in bad_bodies:
        with refused("header_invalid"):
            enroll.verify_enrollment(bad, pk_inv)
    with refused("non_ascii"):
        enroll.enrollment_text({**body, "os": "ünix"})


# --- dedupe (4.10) ---


def test_dedupe_tag_definition() -> None:
    wk = os.urandom(32)
    dk = kdf.dedupe_key(wk)
    fields_ = {"repo": "a/b", "branch": "x"}
    text = P + b"dedupe\x00" + canonical({"type": "github.create_branch", "fields": fields_})
    assert dedupe.dedupe_text("github.create_branch", fields_) == text
    mac = hmac.HMAC(dk, hashes.SHA256())
    mac.update(text)
    assert dedupe.dedupe_tag(dk, "github.create_branch", fields_) == b64u_encode(mac.finalize())
    with refused("header_invalid"):
        dedupe.dedupe_tag(bytes(16), "github.create_branch", fields_)


def test_dedupe_fields_normalisation() -> None:
    sha = "AbC" * 13 + "D"
    assert dedupe.dedupe_fields(
        "github.checkout", {"repo": "DFX/Agent", "ref": sha, "session": SESSION, "extra": 1}
    ) == {"repo": "dfx/agent", "ref": sha.lower(), "session": SESSION}
    assert dedupe.dedupe_fields("github.create_branch", {"repo": "a/b", "branch": "refs/heads/feature/x"}) == {
        "repo": "a/b",
        "branch": "feature/x",
    }
    assert dedupe.dedupe_fields("github.create_branch", {"repo": "a/b", "branch": "refs/heads/ä"})["branch"] == (
        "sha256:" + dedupe.sha256_hex("ä")
    )
    assert dedupe.dedupe_fields(
        "github.commit", {"repo": "a/b", "branch": "x", "parent_sha": "A", "tree_sha": "B"}
    ) == {"repo": "a/b", "branch": "x", "parent_sha": "a", "tree_sha": "b"}
    assert dedupe.dedupe_fields("github.push", {"repo": "a/b", "branch": "x", "commit_sha": "C"})["commit_sha"] == "c"
    assert dedupe.dedupe_fields(
        "github.create_pull_request", {"repo": "a/b", "head": "refs/heads/x", "base": "main"}
    ) == {"repo": "a/b", "head": "x", "base": "main"}
    assert dedupe.dedupe_fields("github.comment", {"repo": "a/b", "number": 3, "body": "ü"}) == {
        "repo": "a/b",
        "number": 3,
        "body": dedupe.sha256_hex("ü"),
    }
    assert dedupe.dedupe_fields("subagent.request", {"session": SESSION, "purpose": "ab", "prompt": "c"}) == {
        "session": SESSION,
        "prompt": dedupe.sha256_hex("ab\x00c"),
    }
    assert dedupe.dedupe_fields("subagent.request", {"session": SESSION, "purpose": "a", "prompt": "bc"}) != (
        dedupe.dedupe_fields("subagent.request", {"session": SESSION, "purpose": "ab", "prompt": "c"})
    )
    assert dedupe.dedupe_fields("github.create_issue", {"repo": "A/B", "title": "ü"}) == {
        "repo": "a/b",
        "title": dedupe.sha256_hex("ü"),
        "body": dedupe.sha256_hex(""),
    }
    assert dedupe.dedupe_fields("github.create_issue", {"repo": "a/b", "title": "t", "body": "x"})["body"] == (
        dedupe.sha256_hex("x")
    )
    assert {
        "github.checkout",
        "github.create_branch",
        "github.commit",
        "github.push",
        "github.create_pull_request",
        "github.comment",
        "github.create_issue",
        "subagent.request",
    } == dedupe.DEDUPE_TYPES


def test_dedupe_refusals() -> None:
    refusals: list[tuple[str, object]] = [
        ("github.create_issue", {"repo": "a/b", "title": "t", "body": 5}),
        ("github.create_issue", {"repo": "a/b", "body": "x"}),
        ("github.list_repositories", {}),
        ("github.push", {"repo": "", "branch": "x", "commit_sha": "c"}),
        ("github.push", {"repo": 5, "branch": "x", "commit_sha": "c"}),
        ("github.checkout", {"repo": "a/b", "ref": "c", "session": "s"}),
        ("github.comment", {"repo": "a/b", "number": True, "body": "x"}),
        ("github.comment", {"repo": "a/b", "number": 1, "body": "\ud800"}),
        ("github.push", None),
        ("github.push", ["repo"]),
    ]
    for op_type, params in refusals:
        with refused("header_invalid"):
            dedupe.dedupe_fields(op_type, params)
    assert dedupe.idempotency_fields("k-1", TASK) == {"idempotency_key": "k-1", "task": TASK}
    assert dedupe.idempotency_fields("schlüssel", TASK)["idempotency_key"] == "sha256:" + dedupe.sha256_hex("schlüssel")
    for key, task in (("", TASK), (5, TASK), ("k", "t")):
        with refused("header_invalid"):
            dedupe.idempotency_fields(key, task)  # type: ignore[arg-type]
