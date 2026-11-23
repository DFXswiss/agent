"""Linux-only part of U-GST-control files (03-api.md section 7.1; ATK-34 and ATK-40 at integration level): as root,
with a real session user and the production layout (``/srv/sessions/<id>`` root-owned 0711, ``work`` the user's
0700), import creates everything owned by the session user and its own group; links a session planted are never
followed, neither by import nor by export; export leaves out files of other owners and special files."""

from __future__ import annotations

import grp
import io
import json
import os
import pwd
import shutil
import stat
import subprocess
import tarfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent_guest import files
from agent_guest.sandbox import unit
from agent_guest.sessions import NO_SUBORDINATE_IDS, NOLOGIN, free_uid
from agent_guest.state import Session

pytestmark = pytest.mark.linux


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
        assert [p.name for p in base.iterdir()] == ["work"]  # no staging directory left
    finally:
        shutil.rmtree(base, ignore_errors=True)
        subprocess.run(["userdel", name], check=False, capture_output=True)
        subprocess.run(["groupdel", name], check=False, capture_output=True)


def tar_of(*members: tuple[str, bytes | None]) -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as out:
        for name, data in members:
            info = tarfile.TarInfo(name)
            info.type = tarfile.REGTYPE if data is not None else tarfile.DIRTYPE
            info.size = len(data or b"")
            info.mode = 0o4755  # set-uid bits in the archive are never applied
            info.uid = 0
            out.addfile(info, io.BytesIO(data) if data is not None else None)
    buffer.seek(0)
    return buffer


def test_import_belongs_to_the_session_user_and_group(session: Session) -> None:
    done = files.import_tar(session, "repos/o/r", tar_of(("src", None), ("src/a.py", b"a"), ("b", b"bb")), replace=True)
    assert done == {"files": 2, "bytes": 3}
    work = Path(session.workspace)
    for path, mode in (
        (work / "repos", 0o700),
        (work / "repos" / "o", 0o700),
        (work / "repos" / "o" / "r", 0o700),
        (work / "repos" / "o" / "r" / "src", 0o700),
        (work / "repos" / "o" / "r" / "src" / "a.py", 0o600),
        (work / "repos" / "o" / "r" / "b", 0o600),
    ):
        info = path.lstat()
        assert (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) == (session.uid, session.uid, mode), path


def test_links_planted_by_the_session_are_never_followed(session: Session, tmp_path: Path) -> None:
    work = Path(session.workspace)
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "keep").write_text("root data")
    os.symlink(victim, work / "repos")
    os.lchown(work / "repos", session.uid, session.uid)
    with pytest.raises(files.FilesError) as caught:
        files.import_tar(session, "repos/o/r", tar_of(("x", b"evil")), replace=True)
    assert caught.value.code == "path_invalid"
    os.unlink(work / "repos")
    os.symlink(victim, work / "target")
    files.import_tar(session, "target", tar_of(("keep", b"evil")), replace=True)
    assert (victim / "keep").read_text() == "root data"
    assert not (work / "target").is_symlink()
    with pytest.raises(files.FilesError) as caught:
        files.export_tar(session, "target/..", None)
    assert caught.value.code == "path_invalid"


def test_export_leaves_out_foreign_and_special_files(session: Session) -> None:
    work = Path(session.workspace)
    root = work / "r"
    root.mkdir(mode=0o700)
    os.chown(root, session.uid, session.uid)
    (root / "mine").write_text("mine")
    os.chown(root / "mine", session.uid, session.uid)
    (root / "roots").write_text("root owned")  # e.g. planted through another way: never exported
    os.symlink("/etc/shadow", root / "shadow")
    os.mkfifo(root / "fifo")
    os.mknod(root / "null", 0o600 | stat.S_IFCHR, os.makedev(1, 3))
    exported = files.export_tar(session, "r", None)
    data = b"".join(exported.stream)
    assert exported.manifest == {
        "files": [{"path": "mine", "size": 4, "sha256": exported.manifest["files"][0]["sha256"]}],
        "skipped": [
            {"path": "fifo", "reason": "special"},
            {"path": "null", "reason": "special"},
            {"path": "roots", "reason": "unreadable"},
            {"path": "shadow", "reason": "symlink"},
        ],
    }
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        assert archive.getnames() == [files.MANIFEST, "mine"]
        manifest = archive.extractfile(files.MANIFEST)
        assert manifest is not None
        assert json.loads(manifest.read()) == exported.manifest
