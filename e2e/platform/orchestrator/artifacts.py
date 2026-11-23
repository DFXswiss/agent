"""Artefact secret scan (07-testing.md sections 5.2 and 6, R-P.9): after every run, before archiving.

Every file below the artefact directory is scanned - also the members of zip files (Playwright traces) - for the
secret values the run generated (hub signing key, database passwords) and for well-known credential shapes. A
finding names the file and the kind of secret, never the value. Videos and images are skipped.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

SKIPPED_SUFFIXES = frozenset({".webm", ".mp4", ".png", ".jpg", ".jpeg", ".gif", ".woff2"})
MAX_BYTES = 64 * 1024 * 1024
PATTERNS: dict[str, re.Pattern[bytes]] = {
    "private key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "anthropic key": re.compile(rb"sk-ant-[A-Za-z0-9_-]{20,}"),
    "openrouter key": re.compile(rb"sk-or-[A-Za-z0-9_-]{20,}"),
    "github token": re.compile(rb"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})"),
    "aws key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "slack token": re.compile(rb"\bxox[abposr]-[A-Za-z0-9-]{10,}"),
}


def _contents(path: Path) -> Iterator[tuple[str, bytes]]:
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if not member.is_dir() and member.file_size <= MAX_BYTES:
                    yield f"{path}!{member.filename}", archive.read(member)
    elif path.stat().st_size <= MAX_BYTES:
        yield str(path), path.read_bytes()


def scan(root: Path, known: Iterable[str]) -> list[str]:
    """Findings `<file>: <kind>`; empty when the artefacts are clean."""
    values = [value.encode() for value in known if len(value) >= 8]
    findings: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() in SKIPPED_SUFFIXES:
            continue
        try:
            items = list(_contents(path))
        except (OSError, zipfile.BadZipFile):
            findings.append(f"{path}: unreadable")
            continue
        for name, data in items:
            if any(value in data for value in values):
                findings.append(f"{name}: generated run secret")
            findings.extend(f"{name}: {kind}" for kind, pattern in PATTERNS.items() if pattern.search(data))
    return findings
