"""U-RUN-hygiene (R-P.9): no coverage exclusions, internal names, private addresses, local paths or secrets in
any new platform path (docs/platform/spec/07-testing.md section 3, CONTRIBUTING.md).

Literals that would match the checks are assembled at run time, so this file passes its own scan. Internal host
and account names are listed only as SHA-256 digests: this repository is public and must not spell them.
"""

from __future__ import annotations

import hashlib
import ipaddress
import itertools
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

# Every path the platform packages create (ADR-001); legacy paths are covered by the legacy tests.
SCANNED = (
    "src/agent_runner",
    "src/agent_guest",
    "src/agent_doubles",
    "src/agent_platform",
    "tests/platform",
    "docs/platform",
    "contracts",
    "scripts",
    "images",
    "deploy/monitoring",
    "Makefile",
    ".coveragerc-platform",
    ".github/workflows",
)
WORKFLOW_PREFIX = "platform-"
SKIPPED_DIRS = frozenset(
    {"node_modules", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "test-results"}
)
SKIPPED_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".woff", ".woff2", ".cast", ".gz", ".tar"})
CODE_SUFFIXES = frozenset(
    {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".sh", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".json"}
)
CODE_NAMES = frozenset({"Makefile"})

# SHA-256 (first 32 hex digits) of lower-cased internal host and account names.
FORBIDDEN_NAME_DIGESTS = frozenset(
    {
        "8f602d722ef3f523bd5aaf207c8222c9",
        "430629fb228e83bd1b88bb4aabc11c5d",
        "aa1885fa44353225a7640b99902be01b",
        "86a93c2a921e49509147ccb56985ecf5",
        "6057ead53f778af9843f2f2f58e0494f",
        "293f57ad344129ea8e70d6c9609c53f7",
        "c6e00751a13c85cb5e7f4fc8ed70c187",
        "0763b652c49e388875387249986c7ee0",
        "d864ad2dbc304f7e0a73555bb196dd4b",
        "4d194df9104edefb4d862f9b71a98e69",
    }
)

_COVER = "pra" + "gma"
PRAGMA_RE = re.compile(
    "|".join(
        (
            # coverage.py's own default exclusion pattern (also without colon or blanks)
            r"#\s*(" + _COVER + r")[:\s]?\s*(no)\s*(cover|branch)",
            "istanbul" + r"\s+ignore",
            r"\b(v8|c8)" + r"\s+ignore",
            r"#\s*type:\s*ignore(?!\[)",
            r"#\s*noqa(?!:)",
        )
    ),
    re.IGNORECASE,
)
LOCAL_HOST_RE = re.compile(r"\b[A-Za-z0-9][A-Za-z0-9-]*" + r"\.local\b(?![-.]?[A-Za-z0-9])")
IPV4_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.]*\d)")
HOME_RE = re.compile("/" + "Users" + r"/[A-Za-z0-9_.-]")
PROFILE_RE = re.compile("claude" + r"-abo\d")
TOKEN_RE = re.compile(
    "|".join(
        (
            r"\bgh[pousr]" + r"_[A-Za-z0-9]{20,}",
            "github" + r"_pat_[A-Za-z0-9_]{20,}",
            "sk-" + r"ant-[A-Za-z0-9_-]{20,}",
            "sk-" + r"or-v1-[A-Za-z0-9]{20,}",
            r"\bAK" + r"IA[0-9A-Z]{16}\b",
            "-----BEGIN " + r"[A-Z ]*PRIVATE KEY-----",
            r"\bxox" + r"[abposr]-[A-Za-z0-9-]{10,}",
        )
    )
)


def _net(first: int, second: int, third: int, prefix: int) -> ipaddress.IPv4Network:
    """Network from octets, so that no address literal appears in this file."""
    return ipaddress.IPv4Network((ipaddress.IPv4Address(bytes((first, second, third, 0))), prefix))


# PROJECT rule: no IP addresses except loopback, the unspecified address and the documentation networks.
ALLOWED_IPV4_NETS = (
    _net(127, 0, 0, 8),  # loopback
    _net(0, 0, 0, 32),  # unspecified (bind all)
    _net(192, 0, 2, 24),  # RFC 5737 TEST-NET-1
    _net(198, 51, 100, 24),  # RFC 5737 TEST-NET-2
    _net(203, 0, 113, 24),  # RFC 5737 TEST-NET-3
)
# Public addresses the specification names as test targets (07-testing.md section 2.3, ATK egress probes).
ALLOWED_IPV4 = frozenset({ipaddress.IPv4Address(bytes((1, 1, 1, 1)))})
# IPv6: unique-local and link-local addresses name internal networks.
FORBIDDEN_IPV6_NETS = (ipaddress.IPv6Network("fc" + "00::/7"), ipaddress.IPv6Network("fe" + "80::/10"))
IPV6_RE = re.compile(r"(?<![\w:.])([0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7})(?![\w:])")


def name_findings(text: str) -> list[str]:
    """Internal names by digest: each word, and each two neighbouring words written together (a name spelled with a
    blank, as in a concept quotation, KOR-38)."""
    found = []
    words = re.findall(r"[a-z0-9]+", text.lower())
    for token in set(words) | {first + second for first, second in itertools.pairwise(words)}:
        for candidate in {token, token.rstrip("0123456789")}:
            if hashlib.sha256(candidate.encode()).hexdigest()[:32] in FORBIDDEN_NAME_DIGESTS:
                found.append(f"internal name (digest of {candidate[:2]}…)")
    return found


def address_findings(text: str) -> list[str]:
    found = []
    for literal in IPV4_RE.findall(text):
        try:
            v4 = ipaddress.IPv4Address(literal)
        except ValueError:
            continue
        if v4 not in ALLOWED_IPV4 and not any(v4 in net for net in ALLOWED_IPV4_NETS):
            found.append(f"IP address {literal}")
    for literal in IPV6_RE.findall(text):
        try:
            v6 = ipaddress.IPv6Address(literal)
        except ValueError:
            continue
        if any(v6 in net for net in FORBIDDEN_IPV6_NETS):
            found.append(f"IP address {literal}")
    return found


def text_findings(text: str, *, code: bool) -> list[str]:
    """Every violation in one file's text; `code` enables the coverage-pragma rule."""
    found = name_findings(text) + address_findings(text)
    checks = [(LOCAL_HOST_RE, "local host name"), (HOME_RE, "home path"), (PROFILE_RE, "profile name")]
    checks.append((TOKEN_RE, "token pattern"))
    if code:
        checks.append((PRAGMA_RE, "coverage or lint exclusion"))
    for pattern, label in checks:
        found += [f"{label}: {match.group(0)[:24]}" for match in pattern.finditer(text)]
    return found


def is_code(path: Path) -> bool:
    return path.suffix in CODE_SUFFIXES or path.name in CODE_NAMES


def scanned_files(root: Path) -> Iterator[Path]:
    for entry in SCANNED:
        top = root / entry
        candidates = [top] if top.is_file() else sorted(top.rglob("*"))
        for path in candidates:
            rel = path.relative_to(root)
            if not path.is_file() or SKIPPED_DIRS.intersection(rel.parts) or path.suffix in SKIPPED_SUFFIXES:
                continue
            if rel.parts[:2] == (".github", "workflows") and not path.name.startswith(WORKFLOW_PREFIX):
                continue
            yield path


def scan(root: Path) -> list[str]:
    problems = []
    for path in scanned_files(root):
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        problems += [f"{path.relative_to(root)}: {item}" for item in text_findings(text, code=is_code(path))]
    return problems


def test_new_paths_are_clean() -> None:
    assert scan(REPO_ROOT) == []


def test_scan_sees_the_platform_files() -> None:
    names = {path.relative_to(REPO_ROOT).as_posix() for path in scanned_files(REPO_ROOT)}
    assert "tests/platform/test_hygiene.py" in names
    assert "Makefile" in names
    assert not any(name.startswith(".github/workflows/test") for name in names)


# Positive samples are built at run time so that they never appear literally in a scanned file.
DOT = "."


@pytest.mark.parametrize(
    "sample",
    [
        "# " + _COVER + ": no cover",
        "# " + _COVER + " no cover",
        "#" + _COVER + ":nocover",
        "# " + _COVER.upper() + ": NO BRANCH",
        "/* istanbul" + " ignore next */",
        "/* v8" + " ignore next */",
        "x = 1  # type" + ": ignore",
        "x = 1  # no" + "qa",
    ],
)
def test_pragmas_are_found_in_code_only(sample: str) -> None:
    assert text_findings(sample, code=True)
    assert text_findings(sample, code=False) == []


@pytest.mark.parametrize("sample", ["x = 1  # type" + ": ignore[attr-defined]", "x = 1  # no" + "qa: E501"])
def test_specific_ignores_are_allowed(sample: str) -> None:
    assert text_findings(sample, code=True) == []


@pytest.mark.parametrize(
    "sample",
    [
        "ssh runner" + DOT + "local",
        "10" + DOT + "1.2.3",
        "192" + DOT + "168.1.20",
        "172" + DOT + "20.0.1",
        "169" + DOT + "254.1.1",
        "100" + DOT + "64.0.9",
        "8" + DOT + "8.4.4",
        "fd" + "12:3456::1",
        "fe" + "80::1%en0",
        "/" + "Users/someone/x",
        "claude" + "-abo2",
        "gh" + "p_" + "a" * 24,
        "github" + "_pat_" + "b" * 24,
        "sk-" + "ant-" + "c" * 24,
        "sk-" + "or-v1-" + "d" * 24,
        "AK" + "IA" + "ABCDEFGHIJKLMNOP",
        "-----BEGIN " + "OPENSSH PRIVATE KEY-----",
        "xox" + "b-" + "1" * 12,
    ],
)
def test_violations_are_found(sample: str) -> None:
    assert text_findings(sample, code=False)


def test_names_are_matched_by_digest() -> None:
    digest_of = {"8f602d722ef3f523bd5aaf207c8222c9"}
    assert digest_of <= FORBIDDEN_NAME_DIGESTS
    assert name_findings("nothing internal here") == []
    assert name_findings("".join(chr(c) for c in (100, 102, 120, 53, 48, 48)))  # spelled by code point
    assert name_findings("".join(chr(c) for c in (100, 102, 120, 112, 114, 100, 49)))  # trailing digits stripped
    assert name_findings("".join(chr(c) for c in (68, 70, 88, 32, 53, 48, 48)))  # two words, upper case
    assert name_findings("".join(chr(c) for c in (100, 102, 120, 32, 115, 116, 117, 102, 102)))  # two words


@pytest.mark.parametrize(
    "sample",
    [
        "127.0.0.1:8788",
        "0.0.0.0",
        "192.0.2.10 and 198.51.100.7 and 203.0.113.9 (RFC 5737)",
        "2001:db8::1 (RFC 3849)",
        "::1 and 127.0.0.53",
        "1.1.1.1:443 (attack-suite egress probe)",
        "12:30:45.123 and std::vector and ::7700",
        "localhost and example.localhost",
        "~/.local/share/agent-runner",
        "version 1.2.3",
        "999.1.1.1",
        "runner device, hub device",
    ],
)
def test_allowed_texts(sample: str) -> None:
    assert text_findings(sample, code=True) == []


def test_scan_reports_and_skips(tmp_path: Path) -> None:
    (tmp_path / "docs" / "platform").mkdir(parents=True)
    (tmp_path / "docs" / "platform" / "a.md").write_text("see " + "10" + DOT + "0.0.1\n")
    (tmp_path / "docs" / "platform" / "b.bin").write_bytes(b"\xff\xfe\x00")
    (tmp_path / "docs" / "platform" / "c.png").write_text("10" + DOT + "0.0.1")
    (tmp_path / "docs" / "platform" / "node_modules").mkdir()
    (tmp_path / "docs" / "platform" / "node_modules" / "d.md").write_text("10" + DOT + "0.0.1")
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "test.yml").write_text("# " + _COVER + ": no cover\n")
    (tmp_path / ".github" / "workflows" / "platform-x.yml").write_text("# " + _COVER + ": no cover\n")
    (tmp_path / "Makefile").write_text("all:\n")
    assert scan(tmp_path) == [
        "docs/platform/a.md: IP address 10" + DOT + "0.0.1",
        ".github/workflows/platform-x.yml: coverage or lint exclusion: # " + _COVER + ": no cover",
    ]
