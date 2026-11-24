"""Copied from agent-core tests/platform/hub/crypto/test_vectors.py at 56db2db59b0e.

CON-02 (Python part): the vector files are current, every negative vector fails with its code, the positive vectors
open with independent code, and the formats unchanged since Spike D reproduce the spike's vectors."""

from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import hpke as pyca_hpke

from agent_runner.crypto import (
    aead,
    bundle,
    credential,
    dedupe,
    encoding,
    enroll,
    event,
    ids,
    invite,
    kdf,
    keys,
    recovery,
    request,
    vectors,
    wrap,
)
from agent_runner.crypto.encoding import P, b64u_decode, b64u_encode, canonical
from agent_runner.crypto.errors import CRYPTO_CODES, CryptoError

REPO_ROOT = Path(__file__).resolve().parents[4]
CONTRACTS = REPO_ROOT / "contracts"
VECTORS = CONTRACTS / "vectors"
SPIKE = json.loads((Path(__file__).with_name("data") / "spike_d_e2ee.json").read_text(encoding="utf-8"))
SUITE = pyca_hpke.Suite(pyca_hpke.KEM.X25519, pyca_hpke.KDF.HKDF_SHA256, pyca_hpke.AEAD.AES_256_GCM)
# The codes this package raises (CRY-01); the others of 04-crypto.md section 8 belong to agent_hub.log and clients.
OWN_CODES = {
    "non_ascii",
    "header_invalid",
    "signature_invalid",
    "decrypt_failed",
    "prf_length",
    "recovery_code_length",
    "hpke_short",
    "invite_code_length",
    "invite_code_invalid",
    "size_exceeded",
}


def load(name: str) -> Any:
    return json.loads((VECTORS / f"{name}.json").read_text(encoding="utf-8"))


def all_invalid() -> list[dict[str, Any]]:
    found = []
    for section in vectors.SECTIONS:
        found += load(section.name)["invalid"]
    return found


def test_vectors_file_is_current() -> None:
    assert vectors.stale(VECTORS, vectors.discover(CONTRACTS / "tools")) == [], (
        "run `python contracts/tools/make_vectors.py`"
    )
    assert vectors.main(["--check"], CONTRACTS) == 0


def test_generator_is_deterministic() -> None:
    for section in vectors.SECTIONS:
        assert vectors.render(section) == vectors.render(section)


@pytest.mark.parametrize("case", all_invalid(), ids=lambda case: f"{case['op']}:{case['name']}")
def test_negative_vector(case: dict[str, Any]) -> None:
    with pytest.raises(CryptoError) as refused:
        vectors.run_op(case["op"], case["args"])
    assert refused.value.code == case["error"]


def test_every_own_error_code_has_a_negative_vector() -> None:
    assert OWN_CODES <= CRYPTO_CODES
    assert {case["error"] for case in all_invalid()} == OWN_CODES
    assert {case["op"] for case in all_invalid()} == set(vectors.OPS)
    names = [f"{case['op']}:{case['name']}" for case in all_invalid()]
    assert len(names) == len(set(names))


def test_negative_refuses_a_wrong_expectation() -> None:
    with pytest.raises(RuntimeError, match="decrypt_failed instead of hpke_short"):
        vectors.negative(
            "x",
            "open_bundle",
            {"kek": b64u_encode(bytes(32)), "user_id": "u", "slot": "recovery", "sealed": b64u_encode(bytes(40))},
            "hpke_short",
        )
    with pytest.raises(RuntimeError, match="no error instead of header_invalid"):
        vectors.negative("x", "b64u_decode", {"text": "Zm9v"}, "header_invalid")


# --- positive vectors, opened with independent code ---


def test_encoding_vectors() -> None:
    data = load("encoding")
    for case in data["b64u"]:
        assert b64u_decode(case["b64u"]) == bytes.fromhex(case["hex"])
    for case in data["canonical"]:
        assert canonical(case["value"]).decode() == case["canonical"]
        assert json.dumps(case["value"], sort_keys=True, separators=(",", ":")) == case["canonical"]
    for case in data["body_json"]:
        assert encoding.body_json(case["value"]).decode() == case["body_json"]
        assert json.loads(case["body_json"]) == case["value"]
    for case in data["uuid7"]:
        assert ids.uuid7(case["unix_ms"], bytes.fromhex(case["random_hex"])) == case["uuid"]
    for case in data["time"]:
        moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=case["unix_ms"])
        assert encoding.format_time(moment) == case["text"]
        assert encoding.parse_time(case["text"]) == moment


def test_kdf_and_keys_vectors() -> None:
    data = load("kdf")
    assert data["prefix"] == P.decode()
    assert b64u_decode(data["prf_input"]) == kdf.prf_input()
    k = data["kek_from_prf"]
    assert kdf.kek_from_prf(b64u_decode(k["prf_output"]), b64u_decode(k["credential_id"])) == b64u_decode(k["kek"])
    code = b64u_decode(data["invite"]["code"])
    assert invite.invite_secrets(code).pk_inv == data["invite"]["pk_inv"]
    seed = b64u_decode(data["invite"]["sk_inv_seed"])
    assert b64u_encode(keys.ed25519_public_raw(keys.ed25519_private(seed))) == data["invite"]["pk_inv"]
    assert recovery.recovery_id(b64u_decode(data["recovery"]["code"])) == data["recovery"]["recovery_id"]
    key_data = load("keys")
    for actor in key_data["identities"].values():
        ident = keys.Identity.from_raw(b64u_decode(actor["kem_sk"]), b64u_decode(actor["sig_sk"]))
        assert ident.public() == {"kem": actor["kem"], "sig": actor["sig"]}
    ed = key_data["ed25519"]
    keys.ed25519_verify(b64u_decode(ed["public_key"]), b64u_decode(ed["signature"]), b64u_decode(ed["message"]))
    p256 = key_data["p256"]
    session = keys.p256_private(b64u_decode(p256["private_scalar"]))
    assert b64u_encode(keys.p256_public_raw(session)) == p256["public_key"]
    keys.p256_verify_raw(b64u_decode(p256["public_key"]), b64u_decode(p256["signature"]), b64u_decode(p256["message"]))


def test_bundle_wrap_event_vectors() -> None:
    data = load("bundle")
    for slot in ("passkey", "recovery"):
        case = data[slot]
        ident = bundle.open_bundle(b64u_decode(case["kek"]), data["user_id"], case["slot"], case["sealed"])
        assert bundle.bundle_plaintext(ident).decode() == data["plaintext"]
    wraps = load("wrap")
    for case in wraps["wraps"]:
        sk = keys.x25519_private(b64u_decode(case["recipient_kem_sk"]))
        opened = SUITE.decrypt(b64u_decode(case["wrap"]), sk, info=b64u_decode(case["info"]))
        assert b64u_encode(opened) == wraps["scope_key"]
        wrap.verify_wrap(
            b64u_decode(case["wrapper_sig_pk"]),
            scope=case["scope"],
            scope_id=case["scope_id"],
            epoch=case["epoch"],
            recipient=case["recipient"],
            wrap=case["wrap"],
            signature=case["sig"],
        )
    for case in load("event")["events"]:
        plain = event.open_event(b64u_decode(case["scope_key"]), case["event"], b64u_decode(case["author_sig_pk"]))
        assert plain.decode() == case["body_json"]
        assert not plain.isascii()
        assert canonical(case["event"]["header"]).decode() == case["aad"]


def test_credential_request_invite_vectors() -> None:
    data = load("credential")
    plain = credential.open_credential(
        b64u_decode(data["cred_key"]), data["record"], b64u_decode(data["author_sig_pk"])
    )
    assert plain.decode() == data["body_json"]
    req = load("request")
    device_kem = keys.x25519_private(b64u_decode(req["device"]["kem_sk"]))
    user_kem = keys.x25519_private(b64u_decode(req["user"]["kem_sk"]))
    for case in req["requests"]:
        body = request.open_request(device_kem, case["envelope"], b64u_decode(req["user"]["sig_pk"]))
        assert body.decode() == case["body_json"]
    for case in req["responses"]:
        body = request.open_response(user_kem, case["envelope"], b64u_decode(req["device"]["sig_pk"]))
        assert body.decode() == case["body_json"]
    inv = load("invite")
    for case in inv["parse"]:
        assert b64u_encode(invite.parse_code(case["text"])) == case["code"]
    secrets = invite.invite_secrets(b64u_decode(inv["code"]))
    assert invite.open_package(secrets, inv["sealed"]) == inv["package"]
    raw = b64u_decode(inv["sealed"])
    opened = aead.open_(secrets.package_key, raw[:12], raw[12:], b64u_decode(inv["package_aad"]))
    assert opened.decode() == inv["package_body_json"]
    rec = load("recovery")
    assert recovery.parse_recovery_code(rec["text"]) == b64u_decode(rec["code"])
    proof = rec["proof"]
    recovery.verify_proof(
        b64u_decode(proof["identity_sig_pk"]), proof["user_id"], b64u_decode(proof["challenge"]), proof["proof"]
    )
    enr = load("enroll")
    enroll.verify_enrollment(enr["body"], b64u_decode(enr["pk_inv"]))
    assert enroll.enrollment_text(enr["body"]).decode() == enr["text"]
    ded = load("dedupe")
    dk = kdf.dedupe_key(b64u_decode(ded["workgroup_key"]))
    assert b64u_encode(dk) == ded["dedupe_key"]
    for case in ded["tags"]:
        assert dedupe.dedupe_tag(dk, case["type"], case["fields"]) == case["tag"]
        if "params" in case:
            assert dedupe.dedupe_fields(case["type"], case["params"]) == case["fields"]


# --- Spike D interop: formats unchanged since the spike reproduce its vectors byte for byte ---


def test_spike_d_vectors_reproduce_where_the_format_is_unchanged() -> None:
    assert b64u_decode(SPIKE["prf_salt"]) == kdf.prf_input()
    alice = SPIKE["identities"]["alice"]
    ident = keys.Identity.from_raw(b64u_decode(alice["kem_sk"]), b64u_decode(alice["sig_sk"]))
    assert ident.public() == {"kem": alice["kem"], "sig": alice["sig"]}
    b = SPIKE["bundle"]
    kek = kdf.kek_from_prf(b64u_decode(b["prf_output"]), b64u_decode(b["credential_id"]))
    assert b64u_encode(kek) == b["kek"]
    assert bundle.bundle_plaintext(ident) == b64u_decode(b["plaintext"])
    assert bundle.seal_bundle(kek, b["user_id"], b["slot"], ident, b64u_decode(b["nonce"])) == b["wrapped"]
    r = SPIKE["recovery"]
    kek_rec = kdf.kek_from_recovery_code(b64u_decode(r["code"]))
    assert b64u_encode(kek_rec) == r["kek"]
    assert bundle.seal_bundle(kek_rec, "alice", "recovery", ident, b64u_decode(r["nonce"])) == r["wrapped"]
    for w in SPIKE["wraps"]:
        sealed = wrap.wrap_key(
            b64u_decode(w["recipient_kem_pk"]),
            scope=w["scope"],
            scope_id=w["scope_id"],
            epoch=w["epoch"],
            recipient=w["recipient"],
            key=b64u_decode(SPIKE["scope_key"]),
            ephemeral=keys.x25519_private(b64u_decode(w["ephemeral_sk"])),
        )
        assert sealed == w["wrapped"]
    i = SPIKE["invite"]
    secrets = invite.invite_secrets(b64u_decode(i["code"]))
    assert invite.format_code(b64u_decode(i["code"])) == i["formatted"]
    assert (secrets.invite_id, b64u_encode(secrets.package_key), secrets.pk_inv) == (
        i["invite_id"],
        i["package_key"],
        i["pk_inv"],
    )
    # the package structure changed (section 4.6); its sealing (body JSON = canonical JSON for ASCII) did not
    package_blob = aead.seal_blob(
        secrets.package_key,
        encoding.body_json(i["package"]),
        invite.package_aad(secrets.invite_id),
        b64u_decode(i["nonce"]),
    )
    assert b64u_encode(package_blob) == i["sealed"]


def test_spike_d_event_derivation_is_unchanged_but_its_header_is_refused() -> None:
    e = SPIKE["event"]
    header, scope_key = e["sealed"]["header"], b64u_decode(SPIKE["scope_key"])
    k_event = kdf.event_key(scope_key, b64u_decode(header["salt"]))
    ct = aead.seal(k_event, b64u_decode(header["nonce"]), e["plaintext"].encode(), canonical(header))
    assert b64u_encode(ct) == e["sealed"]["ct"]
    signature_text = P + kdf.LABEL_EVENT_SIG + canonical(header) + ct
    keys.ed25519_verify(b64u_decode(e["author_sig_pk"]), b64u_decode(e["sealed"]["sig"]), signature_text)
    with pytest.raises(CryptoError, match="header_invalid"):  # (changed): seq/device removed, id/scope_id/ref/lh added
        event.verify_event(e["sealed"], b64u_decode(e["author_sig_pk"]))


# --- registry and command line ---


def _section(name: str) -> vectors.Section:
    return vectors.Section(name, lambda: {"name": name, "text": "ü"})


def test_discover_tool_files_and_areas(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "vectors_extra.py").write_text(
        "from agent_runner.crypto.vectors import Section\nSECTIONS = [Section('extra', lambda: {'a': 1})]\n"
    )
    (tools / "make_vectors.py").write_text("raise SystemExit('not a section file')\n")
    root = tmp_path / "pkg"
    for area in ("crypto", "zarea"):
        (root / area).mkdir(parents=True)
        (root / area / "vectors.py").write_text("")
    fake = types.ModuleType("agent_runner.crypto.zarea.vectors")
    fake.SECTIONS = (_section("log"),)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "agent_runner.crypto.zarea.vectors", fake)
    names = [section.name for section in vectors.discover(tools, root)]
    assert names == [section.name for section in vectors.SECTIONS] + ["log", "extra"]
    assert [s.name for s in vectors.discover(None, root)][-1] == "log"
    discovered = [s.name for s in vectors.discover()]  # the real areas (e.g. agent_hub.log) follow the own sections
    assert discovered[: len(vectors.SECTIONS)] == [s.name for s in vectors.SECTIONS]


def test_discover_refuses_bad_registrations(tmp_path: Path) -> None:
    (tmp_path / "vectors_bad.py").write_text("SECTIONS = [1]\n")
    with pytest.raises(TypeError, match=r"vectors_bad\.py: SECTIONS must be a sequence of Section"):
        vectors.discover(tmp_path)
    (tmp_path / "vectors_bad.py").write_text("NOTHING = 1\n")
    with pytest.raises(TypeError):
        vectors.discover(tmp_path)
    (tmp_path / "vectors_bad.py").write_text(
        "from agent_runner.crypto.vectors import Section\nSECTIONS = (Section('event', dict),)\n"
    )
    with pytest.raises(ValueError, match="duplicate vector sections: event"):
        vectors.discover(tmp_path)


def test_main_writes_and_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(vectors, "SECTIONS", (_section("one"), _section("two")))
    monkeypatch.setattr(vectors, "discover", lambda _tools=None: list(vectors.SECTIONS))  # without the real areas
    assert vectors.main(["--check"], tmp_path) == 1
    assert capsys.readouterr().err == "stale: one.json\nstale: two.json\n"
    assert vectors.main([], tmp_path) == 0
    assert capsys.readouterr().out == "wrote one.json\nwrote two.json\n"
    written = (tmp_path / "vectors" / "one.json").read_text(encoding="utf-8")
    assert written == '{\n  "name": "one",\n  "text": "ü"\n}\n'
    (tmp_path / "vectors" / "two.json").write_text("{}\n")
    (tmp_path / "vectors" / "foreign.json").write_text("{}\n")
    assert vectors.main(["--check"], tmp_path) == 1
    assert capsys.readouterr().err == "stale: two.json\n"
    assert vectors.main([], tmp_path) == 0
    assert capsys.readouterr().out == "wrote two.json\n"
    assert vectors.main(["--check"], tmp_path) == 0
    assert (tmp_path / "vectors" / "foreign.json").read_text() == "{}\n"
