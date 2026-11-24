"""U-HUB-log: entry formats, signing texts, hashes, heads and L9 (agent_hub.log.entries)."""

from __future__ import annotations

import copy
import hashlib
from collections.abc import Callable
from typing import Any

import pytest

from agent_hub.crypto.encoding import P, b64u_encode, canonical
from agent_hub.crypto.errors import CryptoError
from agent_hub.log import entries
from agent_hub.log.vectors import LogBuilder, PlatformBuilder, identity, orgwide_builder

Json = dict[str, Any]


def test_every_entry_of_the_vector_logs_is_well_formed(main: LogBuilder, plat: PlatformBuilder) -> None:
    logs = [*main.entries, *orgwide_builder().entries]
    assert {entries.check_log_entry(e)["type"] for e in logs} == entries.LOG_TYPES
    assert {entries.check_platform_entry(e)["type"] for e in plat.entries} == entries.PLATFORM_TYPES


def _set(path: tuple[str, ...], value: object) -> Callable[[Json], None]:
    def change(entry: Json) -> None:
        target = entry
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return change


def _drop(path: tuple[str, ...]) -> Callable[[Json], None]:
    def change(entry: Json) -> None:
        target = entry
        for key in path[:-1]:
            target = target[key]
        del target[path[-1]]

    return change


# (seq of the main log, change, code)
LOG_FORMAT_CASES = [
    (1, lambda e: e.clear(), "header_invalid"),
    (1, _set(("body",), []), "header_invalid"),
    (1, _drop(("body", "type")), "header_invalid"),
    (1, _set(("body", "type"), 7), "header_invalid"),
    (1, _set(("body", "type"), "wg_delete"), "log_type"),
    (1, _set(("extra",), "x"), "header_invalid"),
    (3, _drop(("inv_sig",)), "header_invalid"),
    (1, _set(("body", "org"), "not-a-uuid"), "header_invalid"),
    (1, _set(("body", "seq"), -1), "header_invalid"),
    (1, _set(("body", "seq"), True), "header_invalid"),
    (1, _set(("body", "prev"), "short"), "header_invalid"),
    (1, _set(("body", "ts"), "2026-11-21T12:00:00Z"), "header_invalid"),
    (1, _set(("body", "ts"), "2026-11-21T12:00:00.000é"), "non_ascii"),
    (1, _set(("body", "v"), True), "header_invalid"),
    (1, _set(("body", "epoch"), 0), "header_invalid"),
    (1, _set(("body", "epoch"), 2**31), "header_invalid"),
    (1, _set(("body", "epoch"), 1.0), "header_invalid"),
    (0, _set(("body", "private"), 1), "header_invalid"),
    (0, _set(("body", "pub"), {"kem": "x"}), "header_invalid"),
    (0, _set(("body", "pub", "kem"), "AAAA"), "header_invalid"),
    (2, _set(("body", "role"), "owner"), "header_invalid"),
    (2, _set(("body", "wgs"), []), "header_invalid"),
    (2, _set(("body", "wgs"), {"not-a-uuid": "read"}), "header_invalid"),
    (2, _set(("body", "kind"), "robot"), "header_invalid"),
    (2, _set(("body", "invite_id"), "AAAA"), "header_invalid"),
    (4, _set(("body", "wgs"), {"01a1ee56-5e00-7342-8ef3-e59bb5fa9e61": "read"}), "header_invalid"),
    (4, _set(("body", "role"), "member"), "header_invalid"),
    (3, _set(("body", "kind"), "robot"), "header_invalid"),
    (12, _set(("body", "access"), "admin"), "header_invalid"),
    (13, _set(("body", "platform_seq"), -1), "header_invalid"),
    (16, _set(("body", "scope"), "cred"), "header_invalid"),
]


@pytest.mark.parametrize(("seq", "change", "code"), LOG_FORMAT_CASES)
def test_log_entry_format_refusals(main: LogBuilder, seq: int, change: Callable[[Json], None], code: str) -> None:
    entry = copy.deepcopy(main.entries[seq])
    change(entry)
    with pytest.raises(CryptoError) as refused:
        entries.check_log_entry(entry)
    assert refused.value.code == code


def test_log_entry_must_be_an_object() -> None:
    with pytest.raises(CryptoError, match="header_invalid"):
        entries.check_log_entry("entry")


def test_device_assign_without_platform_seq_has_no_extra_field(main: LogBuilder) -> None:
    assert "platform_seq" not in entries.check_log_entry(main.entries[14])
    assert entries.check_log_entry(main.entries[13])["platform_seq"] == 2


# (seq of the platform log, change, code)
PLATFORM_FORMAT_CASES = [
    (0, _set(("body", "pub"), "short"), "header_invalid"),
    (0, _set(("inv_sig",), "x"), "header_invalid"),
    (1, _set(("body", "by"), "root"), "header_invalid"),
    (1, _set(("body", "type"), "accept"), "log_type"),
    (2, _drop(("body", "capacity", "pool_ready")), "header_invalid"),
    (2, _set(("body", "capacity", "pool_ready"), -1), "header_invalid"),
    (2, _set(("body", "agent_version"), "-1"), "header_invalid"),
    (2, _set(("body", "os"), ""), "header_invalid"),
    (2, _set(("body", "arch"), 64), "header_invalid"),
    (2, _set(("body", "enroll_sig"), "AAAA"), "header_invalid"),
    (4, _set(("body", "role"), "admin"), "header_invalid"),
    (5, _set(("body", "granted"), "yes"), "header_invalid"),
]


@pytest.mark.parametrize(("seq", "change", "code"), PLATFORM_FORMAT_CASES)
def test_platform_entry_format_refusals(
    plat: PlatformBuilder, seq: int, change: Callable[[Json], None], code: str
) -> None:
    entry = copy.deepcopy(plat.entries[seq])
    change(entry)
    with pytest.raises(CryptoError) as refused:
        entries.check_platform_entry(entry)
    assert refused.value.code == code


def test_signing_texts_hashes_and_signatures(main: LogBuilder) -> None:
    entry = main.entries[3]
    body = entry["body"]
    assert entries.log_text(body) == P + b"log\x00" + canonical(body)
    assert entries.platform_text(body) == P + b"platform-log\x00" + canonical(body)
    assert entries.entry_hash(entry) == b64u_encode(hashlib.sha256(canonical(entry)).digest())
    assert entries.entry_hash({**entry, "sig": entry["inv_sig"]}) != entries.entry_hash(entry)
    signed = entries.sign_log_entry(body, identity("bob").sig)
    assert set(signed) == {"body", "sig"}
    assert signed["sig"] == entry["sig"]
    key = identity("bob").sig_public()
    assert entries.signature_valid(key, entry["sig"], entries.log_text(body))
    assert not entries.signature_valid(key, entry["inv_sig"], entries.log_text(body))
    assert not entries.signature_valid(key, 42, entries.log_text(body))


def test_head_and_l9(main: LogBuilder) -> None:
    head = entries.Head(3, entries.entry_hash(main.entries[3]))
    assert head.to_json() == {"seq": 3, "hash": head.hash}
    entries.check_extends(None, [])
    entries.check_extends(head, main.entries[:4])
    entries.check_extends(head, main.entries)
    with pytest.raises(CryptoError, match="log_rollback"):
        entries.check_extends(head, main.entries[:3])
    with pytest.raises(CryptoError, match="log_rollback"):
        entries.check_extends(entries.Head(3, entries.entry_hash(main.entries[2])), main.entries)
