"""U-HUB-audit (checkpoint part): `agent-audit-head-v1` checkpoints signed with the hub key (02-data-model.md
section 2.9, signature format of agent_core.record.checkpoint)."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from agent_core.signing import HubSigner, b64url_encode
from agent_hub.audit.chain import AuditRow, append_audit
from agent_hub.audit.checkpoint import (
    AuditCheckpoint,
    checkpoint_signature_valid,
    checkpoint_text,
    checkpoint_valid,
    newest_checkpoint,
    sign_checkpoint,
    write_checkpoint,
)
from tests.platform.contracts.harness import Contracts
from tests.platform.hub.conftest import FixedClock, HubDatabase

SEED = "11" * 32
SIGNER = HubSigner.from_seed_hex("hub-test", SEED)
OTHER = HubSigner.from_seed_hex("hub-test", "22" * 32)
HEAD = "ab" * 32
PUBLISHED = datetime(2026, 11, 21, 13, 0, 0, 456789, tzinfo=UTC)
ROW = AuditRow(at=datetime(2026, 11, 21, 12, 0, tzinfo=UTC), actor_kind="hub", action="read.me", result="ok")


def test_checkpoint_text_is_exact() -> None:
    text = checkpoint_text("hub-test", 42, HEAD, PUBLISHED)
    assert text == f"agent-audit-head-v1\nhub-test\n42\n{HEAD}\n2026-11-21T13:00:00.456Z".encode()


@pytest.mark.parametrize(
    ("key_id", "seq", "head"),
    [
        ("Hub", 1, HEAD),
        ("", 1, HEAD),
        ("h" * 65, 1, HEAD),
        ("hub", 0, HEAD),
        ("hub", True, HEAD),
        ("hub", "1", HEAD),
        ("hub", 1, HEAD.upper()),
        ("hub", 1, HEAD[:-1]),
    ],
)
def test_checkpoint_text_refuses_malformed_parts(key_id: str, seq: Any, head: str) -> None:
    with pytest.raises(ValueError, match=r"hub key id|seq|hash"):
        checkpoint_text(key_id, seq, head, PUBLISHED)


def test_signature_verifies_with_the_hub_public_key() -> None:
    checkpoint = sign_checkpoint(SIGNER, 42, HEAD, PUBLISHED)
    assert checkpoint.published_at == datetime(2026, 11, 21, 13, 0, 0, 456000, tzinfo=UTC)
    assert len(checkpoint.signature) == 64
    Ed25519PublicKey.from_public_bytes(SIGNER.public_key).verify(
        checkpoint.signature, checkpoint_text("hub-test", 42, HEAD, PUBLISHED)
    )
    assert checkpoint_signature_valid(SIGNER.public_key, checkpoint)
    assert "signature" not in repr(checkpoint)


def test_tampered_or_malformed_checkpoints_do_not_verify() -> None:
    checkpoint = sign_checkpoint(SIGNER, 42, HEAD, PUBLISHED)
    assert not checkpoint_signature_valid(OTHER.public_key, checkpoint)
    assert not checkpoint_signature_valid(SIGNER.public_key[:31], checkpoint)
    for changed in (
        replace(checkpoint, seq=43),
        replace(checkpoint, hash="cd" * 32),
        replace(checkpoint, hub_key_id="hub-other"),
        replace(checkpoint, published_at=datetime(2026, 11, 21, 14, 0, tzinfo=UTC)),
        replace(checkpoint, signature=bytes(64)),
        replace(checkpoint, signature=checkpoint.signature[:63]),
        replace(checkpoint, hub_key_id="Bad Key"),
    ):
        assert not checkpoint_signature_valid(SIGNER.public_key, changed)


def test_a_small_order_hub_key_does_not_verify_a_forged_checkpoint() -> None:
    """Under the identity key, `R = key, S = 0` verifies any text in OpenSSL; the hardened verify refuses it."""
    identity = bytes([1]) + bytes(31)
    forged = replace(sign_checkpoint(SIGNER, 42, HEAD, PUBLISHED), signature=identity + bytes(32))
    assert not checkpoint_signature_valid(identity, forged)


def test_write_checkpoint_of_the_head(
    migrated_db: HubDatabase, writer: psycopg.Connection[Any], clock: FixedClock
) -> None:
    assert write_checkpoint(writer, SIGNER, clock()) is None
    assert newest_checkpoint(writer, SIGNER.key_id) is None
    append_audit(writer, ROW)
    head = append_audit(writer, ROW)
    first = write_checkpoint(writer, SIGNER, clock())
    assert first is not None
    assert (first.hub_key_id, first.seq, first.hash) == ("hub-test", 2, head.hash)
    assert first.published_at == datetime(2026, 11, 21, 12, 0, 0, 123000, tzinfo=UTC)
    assert newest_checkpoint(writer, SIGNER.key_id) == first
    assert checkpoint_valid(writer, first, SIGNER.public_key)
    # Unchanged head: nothing new.
    assert write_checkpoint(writer, SIGNER, clock.advance(3600)) is None
    third = append_audit(writer, ROW)
    second = write_checkpoint(writer, SIGNER, clock())
    assert second is not None
    assert (second.seq, second.hash) == (3, third.hash)
    assert newest_checkpoint(writer, SIGNER.key_id) == second
    assert newest_checkpoint(writer, "hub-other") is None
    # The API form of a checkpoint matches the audit verify contract.
    Contracts.load().validate(
        "api/org-audit-verify.response.json",
        {
            "ok": True,
            "head": {"seq": 3, "hash": third.hash},
            "checkpoint": {
                "hub_key_id": second.hub_key_id,
                "seq": second.seq,
                "hash": second.hash,
                "published_at": "2026-11-21T13:00:00.123Z",
                "signature": b64url_encode(second.signature),
            },
        },
    )


def test_checkpoint_valid_requires_the_stored_hash(writer: psycopg.Connection[Any]) -> None:
    head = append_audit(writer, ROW)
    good = sign_checkpoint(SIGNER, 1, head.hash, PUBLISHED)
    assert checkpoint_valid(writer, good, SIGNER.public_key)
    assert not checkpoint_valid(writer, sign_checkpoint(SIGNER, 1, "cd" * 32, PUBLISHED), SIGNER.public_key)
    assert not checkpoint_valid(writer, sign_checkpoint(SIGNER, 2, head.hash, PUBLISHED), SIGNER.public_key)
    assert not checkpoint_valid(writer, good, OTHER.public_key)


def test_concurrent_writers_insert_one_checkpoint(migrated_db: HubDatabase, writer: psycopg.Connection[Any]) -> None:
    append_audit(writer, ROW)
    results: list[AuditCheckpoint | None] = []
    with migrated_db.connect(migrated_db.writer_role) as first:
        with first.transaction():
            results.append(write_checkpoint(first, SIGNER, PUBLISHED))

            def second_writer() -> None:
                with migrated_db.connect(migrated_db.writer_role) as second:
                    results.append(write_checkpoint(second, SIGNER, PUBLISHED))

            thread = threading.Thread(target=second_writer)
            thread.start()
            # The second insert waits on the primary key of the uncommitted first one.
            thread.join(timeout=0.5)
            assert thread.is_alive()
        thread.join()
    assert results[0] is not None
    assert results[1] is None
    assert writer.execute("SELECT count(*) FROM hub.audit_checkpoint").fetchone() == (1,)
