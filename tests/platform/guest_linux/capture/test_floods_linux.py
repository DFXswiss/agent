"""U-GST-scanner floods on Linux (HOT-06, ADR-057; marker ``linux``): as root, with a real session user and the
production layout (``/srv/sessions/<id>`` root-owned 0711, ``work`` the user's 0700).

* a checkout of 3 000 files through ``files.import_tar`` with the guard of the capture (``Capture.import_guard``)
  writes no ``session.file`` record; the agent's next single edit is reported with its patch
* a mass change of the session (2 000 files written by the session user) becomes one summary record
"""

from __future__ import annotations

import grp
import io
import os
import pwd
import shutil
import subprocess
import tarfile
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import agent_guest
from agent_guest import files
from agent_guest.clock import SystemClock
from agent_guest.journal import Journal
from agent_guest.sandbox import unit
from agent_guest.scanner import WorkspaceScanner
from agent_guest.schema import SchemaSet
from agent_guest.sessions import NO_SUBORDINATE_IDS, NOLOGIN, free_uid
from agent_guest.state import Session

pytestmark = pytest.mark.linux

REPO = Path(agent_guest.__file__).resolve().parents[2]


@pytest.fixture
def session() -> Iterator[Session]:
    sid = str(uuid.uuid4())
    name = unit.user_name(sid)
    used = {entry.pw_uid for entry in pwd.getpwall()} | {entry.gr_gid for entry in grp.getgrall()}
    uid = free_uid(used)
    base = Path(unit.session_dir(sid))
    subprocess.run(["groupadd", "--gid", str(uid), name], check=True, capture_output=True)
    useradd = ["useradd", "--uid", str(uid), "--gid", str(uid), "--no-create-home", "--home-dir", f"{base}/home"]
    subprocess.run([*useradd, *NO_SUBORDINATE_IDS, "--shell", NOLOGIN, name], check=True, capture_output=True)
    try:
        base.mkdir(mode=0o711)
        os.chmod(base, 0o711)
        (base / "work").mkdir(mode=0o700)
        os.chown(base / "work", uid, uid)
        yield Session(sid, uid, "0" * 64, "working", str(base / "work"), None, "2026-11-21T10:00:00.000Z")
    finally:
        shutil.rmtree(base, ignore_errors=True)
        subprocess.run(["userdel", name], check=False, capture_output=True)


@pytest.fixture
def journal(tmp_path: Path) -> Iterator[Journal]:
    opened = Journal.open(str(tmp_path / "journal.db"), SystemClock(), SchemaSet(REPO / "contracts"))
    try:
        yield opened
    finally:
        opened.close()


def records(journal: Journal) -> list[dict[str, Any]]:
    found, _ = journal.read(0, 100_000)
    return [record["body"] for record in found if record["kind"] == "session.file"]


def repository(count: int) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as out:
        for index in range(count):
            data = f"export const value{index} = {index};\n".encode()
            info = tarfile.TarInfo(f"src/part{index // 100:02}/m{index:04}.ts")
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    buffer.seek(0)
    return buffer


def as_session(session: Session, script: str) -> None:
    subprocess.run(
        ["sh", "-c", script],
        check=True,
        cwd=session.workspace,
        capture_output=True,
        user=session.uid,
        group=session.uid,
        extra_groups=[],
    )


def test_a_checkout_is_the_baseline_and_a_mass_change_one_summary(session: Session, journal: Journal) -> None:
    base = str(Path(session.workspace).parent)
    scanner = WorkspaceScanner(journal, session.id, base, session.uid)
    scanner.start()

    def guard(directory: str, tree: int) -> Any:
        return scanner.materializing(directory, scanner.survey(tree, directory))

    answer = files.import_tar(session, "repos/octo-org/big", repository(3000), replace=True, guard=guard)
    assert answer["files"] == 3000
    assert scanner.scan() == 0
    assert records(journal) == []
    assert [p.name for p in Path(base).iterdir() if p.name.startswith(".files-")] == []

    as_session(session, "printf 'export const value7 = 8;\\n' > repos/octo-org/big/src/part00/m0007.ts")
    assert scanner.check("repos/octo-org/big/src/part00/m0007.ts", "edit") is True
    (edit,) = records(journal)
    assert edit["op"] == "edit"
    assert "-export const value7 = 7;\n+export const value7 = 8;\n" in edit["patch"]

    as_session(session, "mkdir -p gen && i=0; while [ $i -lt 2000 ]; do echo $i > gen/f$i.txt; i=$((i+1)); done")
    as_session(session, "mkdir -p node_modules/pkg && echo x > node_modules/pkg/index.js")
    assert scanner.scan() == 1
    summary = records(journal)[-1]
    assert (summary["op"], summary["path"], summary["files"], summary["reason"]) == ("summary", "gen", 2000, "bulk")
    assert len(summary["paths"]) == 20
    assert "patch" not in summary
    assert scanner.scan() == 0
