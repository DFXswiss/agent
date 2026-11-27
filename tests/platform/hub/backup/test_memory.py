"""The memory of `agent-hub export` and `agent-hub audit verify` is bounded (HOT-27).

Both commands run as their own process, as in operation, on a database with large data: 2000 events of 1 MiB
ciphertext each (2 GiB, more than any fixed block of rows can hold within the limit), and an audit chain of 100 000
rows with large details. The peak resident memory of the process (what the OOM killer of a container counts) must
stay below `PEAK_LIMIT`, which is far below the data each command reads. The child reports `VmHWM` of
`/proc/self/status`: the peak of its own program only. `ru_maxrss` is no measure here, because on Linux it keeps the
peak of the parent across `exec` (a pytest worker after thousands of tests). The child gets only the hub's
environment, so it runs the command alone (without the test's coverage tracer).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from datetime import timedelta
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from psycopg.types.json import Jsonb

from agent_core.signing import b64url_decode
from agent_hub.audit.chain import AuditRow, audit_hash
from agent_hub.backup.export import signed_text
from agent_hub.config import load_config
from tests.platform.hub.conftest import FIXED_NOW, HubDatabase, hub_environ

MIB = 1024 * 1024
PEAK_LIMIT = 256 * MIB
EVENTS = 2000
EVENT_CT = MIB
AUDIT_ROWS = 100_000
DETAIL_KEYS = 16
CHUNK = 4 * MIB
TRAILER_ROOM = MIB
TIMEOUT_SECONDS = 900
CHILD = """
import resource, sys
from agent_hub.cli import main
code = main(sys.argv[1:])
sys.stdout.flush()
try:
    with open("/proc/self/status") as status:
        peak = next(int(line.split()[1]) * 1024 for line in status if line.startswith("VmHWM:"))
except OSError:  # no procfs (macOS): ru_maxrss counts bytes there
    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(f"peak {peak}", file=sys.stderr)
sys.exit(code)
"""


def child(database: HubDatabase, *args: str) -> subprocess.Popen[bytes]:
    """`agent-hub <args>` as its own process with the hub's environment only; it reports its peak memory."""
    environ = {"PATH": os.environ.get("PATH", ""), **hub_environ(database)}
    return subprocess.Popen(
        [sys.executable, "-c", CHILD, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environ
    )


def peak_bytes(stderr: bytes) -> int:
    """The peak resident memory the child reported, in bytes."""
    found = [line for line in stderr.decode().splitlines() if line.startswith("peak ")]
    assert len(found) == 1, stderr
    return int(found[0].split()[1])


def fill_events(database: HubDatabase) -> str:
    """An org with `EVENTS` events of `EVENT_CT` bytes of ciphertext each (distinct per row, built by the server)."""
    org, author = uuid.uuid4(), uuid.uuid4()
    with database.admin() as admin:
        admin.execute(
            "INSERT INTO hub.org (id, private, default_wg, created_by, created_at, log_head_seq, log_head_hash)"
            " VALUES (%s, false, %s, %s, %s, 0, %s)",
            (org, uuid.uuid4(), author, FIXED_NOW, "0" * 64),
        )
        admin.execute(
            "INSERT INTO hub.event (scope_id, scope_seq, id, scope, org_id, author_id, author_kind, kind, ref, epoch,"
            " ts, lh, header_text, ct, sig, size, received_at)"
            " SELECT %(org)s, g, gen_random_uuid(), 'org', %(org)s, %(author)s, 'user', 'test', 'r', 1, %(now)s, 0,"
            " '{}', decode(repeat(md5(g::text), %(repeat)s), 'hex'), decode(repeat('00', 64), 'hex'), %(size)s,"
            " %(now)s FROM generate_series(1, %(events)s) AS g",
            {
                "org": org,
                "author": author,
                "now": FIXED_NOW,
                "repeat": EVENT_CT // 16,
                "size": EVENT_CT,
                "events": EVENTS,
            },
        )
        assert admin.execute("SELECT sum(octet_length(ct)) FROM hub.event").fetchone() == (EVENTS * EVENT_CT,)
    return str(org)


def read_export(stream: Any) -> tuple[int, bytes, str]:
    """Read the export from a pipe while it is written: its length, its trailer line and the SHA-256 of the bytes
    before the trailer (only the last chunks are held)."""
    digest, held, total = hashlib.sha256(), b"", 0
    while chunk := stream.read(CHUNK):
        total += len(chunk)
        held += chunk
        if len(held) > 2 * TRAILER_ROOM:
            digest.update(held[:-TRAILER_ROOM])
            held = held[-TRAILER_ROOM:]
    cut = held.rindex(b"\n", 0, len(held) - 1) + 1
    digest.update(held[:cut])
    return total, held[cut:], digest.hexdigest()


def test_export_of_large_rows_stays_within_a_fixed_memory_limit(migrated_db: HubDatabase) -> None:
    org = fill_events(migrated_db)
    process = child(migrated_db, "export", "--org", org)
    assert process.stdout is not None
    assert process.stderr is not None
    total, trailer_line, digest = read_export(process.stdout)
    stderr = process.stderr.read()
    assert process.wait(timeout=TIMEOUT_SECONDS) == 0, stderr
    trailer = json.loads(trailer_line)
    assert trailer["sha256"] == digest
    assert trailer["rows"] >= EVENTS
    signer = load_config(hub_environ(migrated_db)).signer()
    signature = b64url_decode(trailer["signature"])
    assert signature is not None
    text = signed_text(signer.key_id, org, trailer["rows"], digest)
    Ed25519PublicKey.from_public_bytes(signer.public_key).verify(signature, text)
    assert total > EVENTS * EVENT_CT * 4 // 3  # every ciphertext went through, as b64u
    peak = peak_bytes(stderr)
    assert peak < PEAK_LIMIT, f"peak {peak // MIB} MiB"


def fill_audit(database: HubDatabase) -> str:
    """A valid audit chain of `AUDIT_ROWS` rows with `DETAIL_KEYS` details of 250 characters each; its head hash."""
    prev: str | None = None
    columns = "seq, at, actor_kind, action, result, detail, prev_hash, hash"
    with (
        database.admin() as admin,
        admin.cursor() as cursor,
        cursor.copy(f"COPY hub.audit ({columns}) FROM STDIN") as copy,
    ):
        for seq in range(1, AUDIT_ROWS + 1):
            detail = {f"k{key:02d}": f"{seq:010d}{key:02d}".ljust(250, "x") for key in range(DETAIL_KEYS)}
            at = FIXED_NOW + timedelta(milliseconds=seq)
            row = AuditRow(at=at, actor_kind="hub", action="read.me", result="ok", detail=detail)
            digest = audit_hash(seq, row, prev)
            copy.write_row((seq, row.at, row.actor_kind, row.action, row.result, Jsonb(detail), prev, digest))
            prev = digest
    assert prev is not None
    return prev


def test_audit_verify_of_a_large_chain_stays_within_a_fixed_memory_limit(migrated_db: HubDatabase) -> None:
    head = fill_audit(migrated_db)
    process = child(migrated_db, "audit", "verify")
    stdout, stderr = process.communicate(timeout=TIMEOUT_SECONDS)
    assert process.returncode == 0, stderr
    assert json.loads(stdout) == {"ok": True, "head": {"seq": AUDIT_ROWS, "hash": head}, "checkpoint": None}
    peak = peak_bytes(stderr)
    assert peak < PEAK_LIMIT, f"peak {peak // MIB} MiB"
