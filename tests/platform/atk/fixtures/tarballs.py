"""Malicious repository archives of ATK-40 (07-testing.md section 2.3; 05-runner.md section 12.1; ATS-02).

A real GitHub never serves these (a git tree has no ``..``, absolute paths, hard links or devices), but a compromised
codeload host or a man in the middle could. Each archive has the GitHub shape (one top directory
``<owner>-<repo>-<sha7>/``, gzip) plus one kind of hostile member, and a harmless ``README.md``:

| Repository | Hostile members |
|---|---|
| ``atk/evil-dotdot`` | ``<top>/../../escape.txt`` (would land in the session directory) |
| ``atk/evil-absolute`` | ``/etc/atk40-absolute`` |
| ``atk/evil-hardlink`` | hard links ``<top>/shadow`` -> ``/etc/shadow`` and ``<top>/victim`` -> ``../../secret.txt`` |
| ``atk/evil-linkdir`` | symlink ``<top>/a`` -> ``/etc``, then the file ``<top>/a/atk40-linkdir`` (through the link) |
| ``atk/evil-device`` | ``<top>/null`` (char device 1:3), ``<top>/disk`` (block device 8:0), ``<top>/pipe`` (FIFO) |

``raw(repo)`` is the same content as a plain tar without the top directory: what a runner that skipped its own
checks would hand to the guest's ``files/import`` (the guest must refuse it alone).
"""

from __future__ import annotations

import gzip
import io
import tarfile

SHA = "a7f3c0de0123456789abcdef0123456789abcdef"
README = b"A malicious archive of the attack suite (ATK-40).\n"


def _member(
    name: str, kind: bytes, *, size: int = 0, link: str = "", major: int = 0, minor: int = 0
) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.size, info.linkname = kind, size, link
    info.mode = 0o644 if kind == tarfile.REGTYPE else 0o755
    info.devmajor, info.devminor = major, minor
    info.uname = info.gname = "root"
    return info


def _hostile(repo: str, top: str) -> list[tuple[tarfile.TarInfo, bytes]]:
    escape = b"escaped\n"
    prefix = f"{top}/" if top else ""
    members = {
        "atk/evil-dotdot": [(_member(f"{prefix}../../escape.txt", tarfile.REGTYPE, size=len(escape)), escape)],
        "atk/evil-absolute": [(_member("/etc/atk40-absolute", tarfile.REGTYPE, size=len(escape)), escape)],
        "atk/evil-hardlink": [
            (_member(f"{prefix}shadow", tarfile.LNKTYPE, link="/etc/shadow"), b""),
            (_member(f"{prefix}victim", tarfile.LNKTYPE, link="../../secret.txt"), b""),
        ],
        "atk/evil-linkdir": [
            (_member(f"{prefix}a", tarfile.SYMTYPE, link="/etc"), b""),
            (_member(f"{prefix}a/atk40-linkdir", tarfile.REGTYPE, size=len(escape)), escape),
        ],
        "atk/evil-device": [
            (_member(f"{prefix}null", tarfile.CHRTYPE, major=1, minor=3), b""),
            (_member(f"{prefix}disk", tarfile.BLKTYPE, major=8, minor=0), b""),
            (_member(f"{prefix}pipe", tarfile.FIFOTYPE), b""),
        ],
    }
    return members[repo]


def _archive(repo: str, top: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        if top:
            archive.addfile(_member(top, tarfile.DIRTYPE))
        readme = _member(f"{top}/README.md" if top else "README.md", tarfile.REGTYPE, size=len(README))
        archive.addfile(readme, io.BytesIO(README))
        for info, data in _hostile(repo, top):
            archive.addfile(info, io.BytesIO(data) if data else None)
    return buffer.getvalue()


def top_directory(repo: str) -> str:
    owner, name = repo.split("/", 1)
    return f"{owner}-{name}-{SHA[:7]}"


def tarball(repo: str) -> bytes:
    """The gzipped archive with the GitHub top directory."""
    return gzip.compress(_archive(repo, top_directory(repo)), mtime=0)


def raw(repo: str) -> bytes:
    """The plain tar without top directory (hostile members unchanged)."""
    return _archive(repo, "")
