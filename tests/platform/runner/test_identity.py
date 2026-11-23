"""U-RUN-identity: device identity files (05-runner.md section 1.4, 02-data-model.md section 4)."""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agent_runner import identity as identity_module
from agent_runner.encoding import b64u_decode, b64u_encode
from agent_runner.errors import RunnerError
from agent_runner.identity import (
    Device,
    DeviceKeys,
    Identity,
    canonical,
    device_path,
    ensure_private_dir,
    generate_keys,
    key_path,
    load_device,
    load_identity,
    load_keys,
    parse_device,
    parse_keys,
    remove_identity,
    write_device,
    write_keys,
    write_new_file,
)

HUB_KEY = b64u_encode(b"\x11" * 32)


def device(**overrides: str) -> Device:
    fields = {
        "device_id": str(uuid.UUID(int=0x0190_0000_0000_7000_8000_0000_0000_0001)),
        "kind": "org",
        "hub_url": "https://hub.example.org",
        "hub_key": HUB_KEY,
        "enrolled_at": "2026-11-21T12:00:00.000Z",
    }
    fields.update(overrides)
    return Device(**fields)  # type: ignore[arg-type]


def mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def code_of(caught: pytest.ExceptionInfo[RunnerError]) -> tuple[str, str | None]:
    return caught.value.code, caught.value.name


def test_generate_and_round_trip(runner_home: Path) -> None:
    keys = generate_keys()
    write_keys(runner_home, keys)
    write_device(runner_home, device())
    loaded = load_identity(runner_home)
    assert loaded == Identity(device=device(), keys=keys)
    assert mode(runner_home) == 0o700
    assert mode(runner_home / "keys") == 0o700
    assert mode(key_path(runner_home)) == 0o600
    assert mode(device_path(runner_home)) == 0o600
    assert sorted(p.name for p in runner_home.iterdir()) == ["device.json", "keys"]
    assert sorted(p.name for p in (runner_home / "keys").iterdir()) == ["device.key"]


def test_key_file_format(runner_home: Path) -> None:
    keys = generate_keys()
    write_keys(runner_home, keys)
    text = key_path(runner_home).read_bytes()
    assert text == canonical({"kem": b64u_encode(keys.kem), "sig": b64u_encode(keys.sig), "v": 1})
    assert text.startswith(b'{"kem":"')
    parsed = json.loads(text)
    assert set(parsed) == {"kem", "sig", "v"}
    assert len(b64u_decode(parsed["kem"])) == len(b64u_decode(parsed["sig"])) == 32


def test_device_file_format(runner_home: Path) -> None:
    write_device(runner_home, device())
    parsed = json.loads(device_path(runner_home).read_bytes())
    assert parsed == {
        "device_id": device().device_id,
        "enrolled_at": "2026-11-21T12:00:00.000Z",
        "hub_key": HUB_KEY,
        "hub_url": "https://hub.example.org",
        "kind": "org",
    }


def test_public_keys_and_signing() -> None:
    keys = generate_keys()
    signature = keys.signing_key().sign(b"message")
    public = Ed25519PublicKey.from_public_bytes(b64u_decode(keys.sig_public))
    public.verify(signature, b"message")
    kem_public = keys.kem_key().public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert b64u_decode(keys.kem_public) == kem_public
    assert "private" in repr(keys)
    assert b64u_encode(keys.sig) not in repr(keys)
    assert generate_keys() != keys


def test_key_id() -> None:
    assert device().key_id == "device.01900000000070008000000000000001"


def test_existing_file_is_never_overwritten(runner_home: Path) -> None:
    keys = generate_keys()
    write_keys(runner_home, keys)
    before = key_path(runner_home).read_bytes()
    with pytest.raises(RunnerError) as caught:
        write_keys(runner_home, generate_keys())
    assert code_of(caught) == ("identity_exists", "device.key")
    assert key_path(runner_home).read_bytes() == before
    write_device(runner_home, device())
    with pytest.raises(RunnerError) as caught:
        write_device(runner_home, device(kind="platform"))
    assert code_of(caught) == ("identity_exists", "device.json")
    assert sorted(p.name for p in (runner_home / "keys").iterdir()) == ["device.key"]
    assert sorted(p.name for p in runner_home.iterdir()) == ["device.json", "keys"]


def test_symlink_target_is_refused_and_not_followed(runner_home: Path, tmp_path: Path) -> None:
    ensure_private_dir(runner_home / "keys")
    victim = tmp_path / "victim"
    victim.write_text("untouched", encoding="utf-8")
    key_path(runner_home).symlink_to(victim)
    with pytest.raises(RunnerError) as caught:
        write_keys(runner_home, generate_keys())
    assert code_of(caught) == ("identity_exists", "device.key")
    assert victim.read_text(encoding="utf-8") == "untouched"
    dangling = tmp_path / "dangling-target"
    device_path(runner_home).symlink_to(dangling)
    with pytest.raises(RunnerError):
        write_device(runner_home, device())
    assert not dangling.exists()


def test_symlinked_keys_directory_is_refused(runner_home: Path, tmp_path: Path) -> None:
    ensure_private_dir(runner_home)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (runner_home / "keys").symlink_to(elsewhere)
    with pytest.raises(RunnerError) as caught:
        write_keys(runner_home, generate_keys())
    assert code_of(caught) == ("identity_symlink", "keys")
    assert list(elsewhere.iterdir()) == []


def test_keys_path_that_is_a_file_is_refused(runner_home: Path) -> None:
    ensure_private_dir(runner_home)
    (runner_home / "keys").write_text("", encoding="utf-8")
    with pytest.raises(RunnerError) as caught:
        write_keys(runner_home, generate_keys())
    assert code_of(caught) == ("identity_not_directory", "keys")


def test_private_dir_tightens_mode(runner_home: Path) -> None:
    runner_home.mkdir(mode=0o755)
    os.chmod(runner_home, 0o755)
    ensure_private_dir(runner_home)
    assert mode(runner_home) == 0o700


def test_write_is_atomic_temporary_is_removed(runner_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ensure_private_dir(runner_home)
    seen: list[tuple[str, int]] = []
    real_link = os.link

    def watching_link(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        source = Path(src)
        seen.append((source.name, stat.S_IMODE(source.stat().st_mode)))
        assert source.read_bytes() == b"payload"
        assert not Path(dst).exists()
        real_link(src, dst)

    monkeypatch.setattr(os, "link", watching_link)
    write_new_file(runner_home / "file", b"payload")
    assert len(seen) == 1
    assert seen[0][0].startswith(".file.")
    assert seen[0][0].endswith(".tmp")
    assert seen[0][1] == 0o600
    assert sorted(p.name for p in runner_home.iterdir()) == ["file"]
    assert (runner_home / "file").read_bytes() == b"payload"


def test_load_missing_identity(runner_home: Path) -> None:
    assert load_identity(runner_home) is None
    assert load_keys(runner_home) is None
    assert load_device(runner_home) is None


def test_partial_identity_is_refused(runner_home: Path) -> None:
    write_keys(runner_home, generate_keys())
    with pytest.raises(RunnerError) as caught:
        load_identity(runner_home)
    assert code_of(caught) == ("identity_incomplete", None)
    remove_identity(runner_home)
    write_device(runner_home, device())
    with pytest.raises(RunnerError) as caught:
        load_identity(runner_home)
    assert code_of(caught) == ("identity_incomplete", None)


def test_load_refuses_wide_mode(runner_home: Path) -> None:
    write_keys(runner_home, generate_keys())
    os.chmod(key_path(runner_home), 0o644)
    with pytest.raises(RunnerError) as caught:
        load_keys(runner_home)
    assert code_of(caught) == ("identity_mode", "device.key")


def test_load_refuses_symlink(runner_home: Path, tmp_path: Path) -> None:
    real = tmp_path / "real.key"
    write_keys(tmp_path / "other", generate_keys())
    real.write_bytes(key_path(tmp_path / "other").read_bytes())
    os.chmod(real, 0o600)
    ensure_private_dir(runner_home / "keys")
    key_path(runner_home).symlink_to(real)
    with pytest.raises(RunnerError) as caught:
        load_keys(runner_home)
    assert code_of(caught) == ("identity_unreadable", "device.key")


def test_load_refuses_directory(runner_home: Path) -> None:
    ensure_private_dir(runner_home)
    device_path(runner_home).mkdir(mode=0o700)
    with pytest.raises(RunnerError) as caught:
        load_device(runner_home)
    assert code_of(caught) == ("identity_unreadable", "device.json")


@pytest.mark.parametrize(
    "text",
    [
        b"not json",
        b"[]",
        b'{"kem":"x","sig":"y"}',
        b'{"kem":"x","sig":"y","v":1,"extra":1}',
        b'{"kem":"AAAA","sig":"AAAA","v":2}',
        b'{"kem":"AAAA","sig":"AAAA","v":true}',
        b'{"kem":1,"sig":"AAAA","v":1}',
        b'{"kem":"AA=A","sig":"AAAA","v":1}',
        b'{"kem":"AAAA","sig":"AAAA","v":1}',
    ],
)
def test_parse_keys_invalid(text: bytes) -> None:
    with pytest.raises(RunnerError) as caught:
        parse_keys(text)
    assert code_of(caught) == ("identity_invalid", "device.key")


def test_parse_keys_valid() -> None:
    keys = DeviceKeys(kem=b"\x01" * 32, sig=b"\x02" * 32)
    assert parse_keys(identity_module.keys_text(keys)) == keys


def valid_device_dict() -> dict[str, object]:
    return json.loads(identity_module.device_text(device()))  # type: ignore[no-any-return]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("device_id", "not-a-uuid"),
        ("device_id", "0190000000007000800000000000000a"),
        ("device_id", "0190000A-0000-7000-8000-000000000001"),
        ("kind", "laptop"),
        ("hub_url", ""),
        ("hub_key", "short"),
        ("hub_key", b64u_encode(b"\x01" * 31)),
        ("enrolled_at", "2026-11-21 12:00:00"),
        ("enrolled_at", 5),
    ],
)
def test_parse_device_invalid(field: str, value: object) -> None:
    data = valid_device_dict()
    data[field] = value
    with pytest.raises(RunnerError) as caught:
        parse_device(json.dumps(data).encode())
    assert code_of(caught) == ("identity_invalid", "device.json")


@pytest.mark.parametrize("text", [b"{", b'"string"', b"{}"])
def test_parse_device_not_an_object(text: bytes) -> None:
    with pytest.raises(RunnerError) as caught:
        parse_device(text)
    assert code_of(caught) == ("identity_invalid", "device.json")


def test_parse_device_extra_field() -> None:
    data = valid_device_dict()
    data["extra"] = "x"
    with pytest.raises(RunnerError):
        parse_device(json.dumps(data).encode())


def test_write_device_validates_first(runner_home: Path) -> None:
    with pytest.raises(RunnerError) as caught:
        write_device(runner_home, device(kind="laptop"))
    assert code_of(caught) == ("identity_invalid", "device.json")
    assert not runner_home.exists()


def test_platform_device(runner_home: Path) -> None:
    write_keys(runner_home, generate_keys())
    write_device(runner_home, device(kind="platform"))
    loaded = load_identity(runner_home)
    assert loaded is not None
    assert loaded.device.kind == "platform"


def test_remove_identity(runner_home: Path, tmp_path: Path) -> None:
    write_keys(runner_home, generate_keys())
    write_device(runner_home, device())
    (runner_home / "other").write_text("kept", encoding="utf-8")
    remove_identity(runner_home)
    assert sorted(p.name for p in runner_home.iterdir()) == ["other"]
    remove_identity(runner_home)
    ensure_private_dir(runner_home / "keys")
    (runner_home / "keys" / "foreign").write_text("", encoding="utf-8")
    remove_identity(runner_home)
    assert (runner_home / "keys" / "foreign").exists()
    target = tmp_path / "t"
    target.write_text("t", encoding="utf-8")
    device_path(runner_home).symlink_to(target)
    remove_identity(runner_home)
    assert target.exists()
    assert not device_path(runner_home).is_symlink()


def test_remove_identity_never_reaches_through_a_symlinked_keys_dir(runner_home: Path, tmp_path: Path) -> None:
    ensure_private_dir(runner_home)
    elsewhere = tmp_path / "elsewhere"
    write_keys(elsewhere, generate_keys())
    (runner_home / "keys").symlink_to(elsewhere / "keys")
    remove_identity(runner_home)
    assert key_path(elsewhere).is_file()
    assert not (runner_home / "keys").exists()
    assert not (runner_home / "keys").is_symlink()


def test_load_refuses_a_symlinked_keys_directory(runner_home: Path, tmp_path: Path) -> None:
    """O_NOFOLLOW covers only the file; a linked keys directory must not hand out foreign keys."""
    foreign = tmp_path / "foreign"
    write_keys(foreign, generate_keys())
    ensure_private_dir(runner_home)
    (runner_home / "keys").symlink_to(foreign / "keys")
    with pytest.raises(RunnerError) as caught:
        load_keys(runner_home)
    assert code_of(caught) == ("identity_unreadable", "keys")
