"""LIVE-03 helpers: profile snapshots, the read-only access token, the usage answer shape and the secret scan.

Every token in this file is a made-up placeholder; the prefixes that the hygiene scan knows are assembled at run time.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from probelib.live03 import (
    KEYCHAIN_SERVICE,
    TokenError,
    access_token,
    compare_snapshots,
    is_excluded,
    keychain_metadata,
    keychain_service,
    profile_files,
    redact,
    scan_files,
    snapshot,
    token_findings,
    turn_problems,
    usage_shape,
)

FAKE = "tok-" + "x" * 40
OAT = "sk-" + "ant-oat01-" + "A" * 30

KEYCHAIN_TEXT = """keychain: "/home/someone/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    0x00000007 <blob>="Claude Code-credentials-0123abcd"
    0x00000008 <blob>=<NULL>
    "acct"<blob>="someone"
    "cdat"<timedate>=0x32303236313132313038303030305A00  "20261121080000Z\\000"
    "mdat"<timedate>=0x32303236313132313038303030305A00  "20261121080000Z\\000"
    "svce"<blob>="Claude Code-credentials-0123abcd"
"""


def test_keychain_service_follows_the_legacy_suffix_rule() -> None:
    # agent_cli.usage.keychain_service_name: first 8 hex of the SHA-256 of the NFC form of the directory.
    assert keychain_service("/profiles/one") == KEYCHAIN_SERVICE + "-d8abf930"
    assert keychain_service("/profiles/café") == keychain_service("/profiles/café")


def test_keychain_metadata_keeps_attributes_only() -> None:
    found = keychain_metadata(KEYCHAIN_TEXT)
    assert found["class"] == '"genp"'
    assert found['"mdat"<timedate>'].startswith("0x3230")
    assert found['"acct"<blob>'] == '"someone"'
    assert "0x00000008 <blob>" in found
    with pytest.raises(ValueError, match="no keychain attributes"):
        keychain_metadata("nothing here\n")


def test_profile_files_and_snapshot(tmp_path: Path) -> None:
    (tmp_path / "projects" / "p").mkdir(parents=True)
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "projects" / "p" / "t.jsonl").write_text("x\n", encoding="utf-8")
    os.symlink(tmp_path / "a.json", tmp_path / "link")
    files = profile_files(tmp_path)
    assert sorted(files) == ["a.json", "link", "projects/p/t.jsonl"]
    assert files["link"].startswith("link:")
    assert files["a.json"].startswith("sha256:")
    taken = snapshot(tmp_path, KEYCHAIN_TEXT)
    assert taken["files"] == files
    assert len(str(taken["keychain_sha256"])) == 64
    keychain = taken["keychain"]
    assert isinstance(keychain, dict)
    assert keychain['"svce"<blob>'] == '"Claude Code-credentials-0123abcd"'


def test_compare_snapshots(tmp_path: Path) -> None:
    (tmp_path / "a.json").write_text("{}", encoding="utf-8")
    (tmp_path / "b.json").write_text("{}", encoding="utf-8")
    before = snapshot(tmp_path, KEYCHAIN_TEXT)
    assert compare_snapshots(before, snapshot(tmp_path, KEYCHAIN_TEXT)) == []
    (tmp_path / "a.json").write_text('{"x": 1}', encoding="utf-8")
    (tmp_path / "b.json").unlink()
    (tmp_path / "c.json").write_text("{}", encoding="utf-8")
    mdat = '"mdat"<timedate>=0x32303236313132313038303030305A00  "20261121080000Z\\000"'
    changed_keychain = KEYCHAIN_TEXT.replace(mdat, mdat.replace("080000Z", "090000Z"))
    assert changed_keychain != KEYCHAIN_TEXT
    after = snapshot(tmp_path, changed_keychain)
    assert compare_snapshots(before, after) == [
        "changed: a.json",
        "removed: b.json",
        "added: c.json",
        'keychain attribute changed: "mdat"<timedate>',
    ]


def credential_json(token: str = FAKE, expires_ms: int = 2_000_000, **extra: object) -> str:
    oauth = {"accessToken": token, "refreshToken": "r-" + "y" * 30, "expiresAt": expires_ms, **extra}
    return json.dumps({"claudeAiOauth": oauth})


def test_access_token_reads_only_the_access_token() -> None:
    found = access_token(credential_json(subscriptionType="max"), now_ms=1_000_000, min_valid_s=600)
    assert found.token == FAKE
    assert found.expires_at_ms == 2_000_000
    assert found.subscription == "max"
    assert FAKE not in repr(found)
    assert "r-yyy" not in repr(found)
    assert access_token(credential_json(), now_ms=1_000_000, min_valid_s=0).subscription is None


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("not json " + FAKE, "not JSON"),
        (json.dumps([FAKE]), "claudeAiOauth"),
        (json.dumps({"claudeAiOauth": FAKE}), "claudeAiOauth"),
        (credential_json(token=""), "accessToken"),
        (credential_json(token="has space " + FAKE), "accessToken"),
        (json.dumps({"claudeAiOauth": {"accessToken": FAKE, "expiresAt": "soon"}}), "expiresAt"),
        (json.dumps({"claudeAiOauth": {"accessToken": FAKE, "expiresAt": True}}), "expiresAt"),
        (credential_json(expires_ms=1_500_000), "expires in 500 s"),
    ],
)
def test_access_token_refusals_never_show_the_token(raw: str, message: str) -> None:
    with pytest.raises(TokenError, match=message) as caught:
        access_token(raw, now_ms=1_000_000, min_valid_s=600)
    assert FAKE not in str(caught.value)
    assert caught.value.__context__ is None or FAKE not in str(caught.value.__context__)
    assert caught.value.__suppress_context__ or caught.value.__context__ is None


def test_usage_shape_keeps_names_not_values() -> None:
    body = json.dumps(
        {"five_hour": {"utilization": 12.0, "resets_at": "x"}, "seven_day": {"utilization": 3.0}, "extra": None}
    ).encode()
    assert usage_shape(200, body) == {
        "status": 200,
        "json": True,
        "keys": ["extra", "five_hour", "seven_day"],
        "windows": ["five_hour", "seven_day"],
        "window_fields": {"five_hour": ["resets_at", "utilization"], "seven_day": ["utilization"]},
    }
    assert usage_shape(401, b'{"type":"error","error":{"type":"authentication_error","message":"m"}}') == {
        "status": 401,
        "json": True,
        "keys": ["error", "type"],
        "windows": [],
        "window_fields": {},
        "error_type": "authentication_error",
    }
    assert usage_shape(403, b"<html>") == {"status": 403, "json": False}
    assert usage_shape(500, b"[1]") == {"status": 500, "json": True, "keys": [], "windows": [], "window_fields": {}}
    assert usage_shape(400, b'{"error": "plain"}')["window_fields"] == {}


def test_token_findings_and_scan(tmp_path: Path) -> None:
    assert token_findings("nothing", FAKE) == []
    assert token_findings("x " + FAKE + " y", FAKE) == ["the access token"]
    assert token_findings("x " + OAT, FAKE) == ["token pattern"]
    assert token_findings("Bearer " + "abcdefghijklmnopqrstuvwxyz0123456789ABCD", FAKE) == ["bearer value"]
    assert token_findings("Bearer <redacted>", FAKE) == []
    (tmp_path / "ok.txt").write_text("clean", encoding="utf-8")
    (tmp_path / "bad.json").write_text('{"v": "' + FAKE + '"}', encoding="utf-8")
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe" + FAKE.encode())
    (tmp_path / "sub").mkdir()
    assert scan_files([tmp_path / "ok.txt", tmp_path / "bad.json", tmp_path / "bin.dat", tmp_path / "sub"], FAKE) == [
        f"{tmp_path / 'bad.json'}: the access token",
        f"{tmp_path / 'bin.dat'}: the access token",
    ]


def test_redact() -> None:
    assert redact("a " + FAKE + " b", FAKE) == "a <redacted> b"
    assert redact("a b", "") == "a b"


def test_is_excluded_resolves_the_directory(tmp_path: Path) -> None:
    (tmp_path / "prof-x").mkdir()
    os.symlink(tmp_path / "prof-x", tmp_path / "alias")
    assert is_excluded(tmp_path / "prof-x", ["prof-x"])
    assert is_excluded(Path(str(tmp_path / "prof-x") + "/"), [" prof-x "])
    assert is_excluded(tmp_path / "sub" / ".." / "prof-x", ["prof-x"])
    assert is_excluded(tmp_path / "alias", ["prof-x"])
    assert not is_excluded(tmp_path / "prof-x", ["", "other"])


def test_turn_problems() -> None:
    good = {"failure": None, "prompt_accepted": True, "gateway_requests": [{"model": "m", "status": 200}]}
    assert turn_problems(good, "m") == []
    assert turn_problems({"failure": "timeout", "gateway_requests": "x"}, "m") == [
        "session: timeout",
        "prompt not accepted",
        "no gateway request of m with status 200",
    ]
    assert turn_problems({**good, "gateway_requests": [{"model": "m", "status": 401}, "x"]}, "m") == [
        "no gateway request of m with status 200"
    ]
