"""Build of the runner package of an own device (ADR-074 section 1).

`python -m agent_runner.install.build (--checkout DIR | --source DIR --commit-time SECONDS) --commit REF --hub URL
--root-key KEY --release-key-file FILE --work DIR --out DIR [--sign-identity ID] [--installer-identity ID] [--notarized]
[--expires-days N]`

Everything comes from one commit (`git archive`) and its lock (`scripts/macos-package/lock.json`,
`scripts/macos-package/requirements.lock`):

1. the version is the commit time in UTC (`YYYY.MM.DD.HHMM`);
2. the pinned inputs are downloaded into `<work>/cache` and checked by SHA-256 (a cached file that matches is reused);
3. the payload is staged: the relocatable CPython (`python/`), the runner's dependencies installed into it with
   `pip --require-hashes` and the packages `agent_runner` and `agent_cli` copied into its `site-packages`, Lima
   (`lima/`), PostgreSQL (`postgresql/`), the golden's sources (`golden-src/`: `images/`, `src/agent_guest/`,
   `contracts/`) and the launchers in `libexec/`;
4. modes and times are normalised (0755 for executables and directories, 0644 for the rest, the commit time
   everywhere) and the interpreter's whole library is compiled with unchecked hashes (no time stamps; the launcher
   runs `python -I -B`, so nothing is ever written into the payload), so two builds of one commit stage the same tree;
5. with `--sign-identity` every Mach-O file is signed with the hardened runtime and a time stamp (entitlements from
   `scripts/macos-package/entitlements/`), **before** the manifest is written;
6. `release.json` (every file's SHA-256 and the pinned Claude Code download) is written and signed with the release
   key; `pkgbuild` makes the package (signed with `--installer-identity` when given), whose `postinstall` runs the
   root helper;
   the release key must already be listed in the payload's `release_keys.py`; `--trust-build-key` adds the build's
   own key instead (test builds, never a release);
7. `latest.json` names the package and its digest, is signed and expires after `--expires-days` (at most 120).

Outputs in `--out`: the package, `release.json`, `latest.json` and `latest.json.sig`. The private release key is read
from a file (base64url of the 32-byte Ed25519 seed) and never written anywhere.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

from agent_runner.crypto.encoding import b64u_decode
from agent_runner.crypto.errors import CryptoError
from agent_runner.errors import RunnerError
from agent_runner.install import release
from agent_runner.install.layout import PACKAGE_ID, Layout
from agent_runner.install.system import Fetcher, Runner, fetch_url, run_argv

LOCK = "scripts/macos-package/lock.json"
REQUIREMENTS = "scripts/macos-package/requirements.lock"
ENTITLEMENTS = "scripts/macos-package/entitlements"
VERSIONS = "images/versions.json"
GOLDEN_PARTS = ("images", "src/agent_guest", "contracts")
PACKAGES = ("agent_runner", "agent_cli")
LIBRARY = "python/lib/python3.12"
SITE = LIBRARY + "/site-packages"
MIN_MACOS = "14.0"
KEYS_LINE = re.compile(r"^KEYS: tuple\[str, \.\.\.\] = (\(.*\))$", re.MULTILINE)
SKIP = shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store")
MAX_INPUT = 1024**3
MACHO = (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xfe\xed\xfa\xcf", b"\xbe\xba\xfe\xca")
LAUNCHER = """#!/bin/sh
# Runs a module of the payload's interpreter, isolated from the caller's Python settings (ADR-074 section 3).
here=$(cd "$(dirname "$0")/.." && pwd -P)
exec "$here/python/bin/python3" -I -B -m "$@"
"""
ROOT_HELPER = """#!/bin/sh
# The privileged helper of the runner package (ADR-074 section 3); only root runs it.
exec "$(dirname "$0")/dfx-agent-runner" agent_runner.install.root "$@"
"""
POSTINSTALL = """#!/bin/sh
# postinstall of the runner package (ADR-074 section 3): everything that needs root happens in the root helper.
exec "{payload}/libexec/root-helper" postinstall --version "{version}"
"""


@dataclass(frozen=True)
class Options:
    """`checkout` is a git repository whose `commit` is exported; or `source` is the commit's tree already exported
    (a test device without the repository's history), with `commit` the full SHA-1 and `commit_time` its UNIX time."""

    checkout: Path
    commit: str
    hub: str
    root_key: str
    release_key: bytes
    work: Path
    out: Path
    sign_identity: str | None = None
    installer_identity: str | None = None
    notarized: bool = False
    expires_days: int = 90
    source: Path | None = None
    commit_time: int | None = None
    trust_build_key: bool = False


@dataclass
class Tools:
    run: Runner = run_argv
    fetch: Fetcher = fetch_url
    now: Callable[[], datetime] = lambda: datetime.now(UTC)
    log: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Result:
    version: str
    package: Path
    package_sha256: str
    manifest_sha256: str


def _must(tools: Tools, argv: Sequence[str], step: str) -> str:
    done = tools.run(argv)
    if done.returncode != 0:
        raise RunnerError("build_failed", step)
    return done.stdout


def version_of(tools: Tools, options: Options) -> tuple[str, int]:
    """The release version and the commit time (UNIX seconds) of the commit."""
    text = _must(tools, ["git", "-C", str(options.checkout), "show", "-s", "--format=%H %ct", options.commit], "git")
    parts = text.split()
    if len(parts) != 2 or not parts[1].isdigit() or release.COMMIT_RE.fullmatch(parts[0]) is None:
        raise RunnerError("build_failed", "git")
    seconds = int(parts[1])
    return datetime.fromtimestamp(seconds, UTC).strftime("%Y.%m.%d.%H%M"), seconds


def full_commit(tools: Tools, options: Options) -> str:
    return _must(tools, ["git", "-C", str(options.checkout), "rev-parse", options.commit + "^{commit}"], "git").strip()


def export(tools: Tools, options: Options, target: Path) -> None:
    """The commit's tree in `target` (`git archive`, extracted with the data filter)."""
    archive = options.work / "source.tar"
    _must(tools, ["git", "-C", str(options.checkout), "archive", "-o", str(archive), options.commit], "git")
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with tarfile.open(archive) as handle:
        handle.extractall(target, filter="data")
    archive.unlink()


def read_lock(source: Path) -> dict[str, Any]:
    try:
        lock = json.loads((source / LOCK).read_text(encoding="utf-8"))
        versions = json.loads((source / VERSIONS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise RunnerError("build_failed", "lock") from None
    names = ("python", "lima", "postgresql", "claude_code")
    if not isinstance(lock, dict) or lock.get("v") != 1 or not all(isinstance(lock.get(n), dict) for n in names):
        raise RunnerError("build_failed", "lock")
    for name in names:
        item = lock[name]
        if (
            not str(item.get("url", "")).startswith("https://")
            or release.DIGEST_RE.fullmatch(str(item.get("sha256", ""))) is None
        ):
            raise RunnerError("build_failed", "lock")
    if lock["claude_code"].get("version") != versions.get("claude_code"):
        raise RunnerError("build_failed", "claude_pin")
    return lock


def fetch_input(tools: Tools, cache: Path, name: str, item: dict[str, Any]) -> Path:
    """The pinned input `name` in the cache, downloaded when missing or different; `build_failed` on a digest error."""
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / f"{name}-{item['sha256'][:16]}"
    if path.is_file() and release.sha256_file(path) == item["sha256"]:
        return path
    data = tools.fetch(str(item["url"]), MAX_INPUT)
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise RunnerError("build_failed", f"digest_{name}")
    path.write_bytes(data)
    return path


def _extract_tar(archive: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive) as handle:
        handle.extractall(target, filter="data")


def stage_postgresql(jar: Path, target: Path) -> None:
    """The binaries inside the pinned jar (`postgres-darwin-arm_64.txz`)."""
    with zipfile.ZipFile(jar) as archive:
        inner = [name for name in archive.namelist() if name.endswith(".txz")]
        if len(inner) != 1:
            raise RunnerError("build_failed", "postgresql")
        data = archive.read(inner[0])
    target.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as handle:
        handle.extractall(target, filter="data")


def drop_scripts(folder: Path, stage_root: Path, site: Path) -> list[str]:
    """Remove the console scripts pip wrote (their first line names the build directory; nothing runs them) and
    their lines in the `RECORD` files of `site`."""
    marker = b"#!" + str(stage_root).encode() + b"/"
    removed: list[str] = []
    for path in sorted(folder.iterdir()):
        if path.is_file() and not path.is_symlink():
            with path.open("rb") as handle:
                head = handle.read(len(marker))
            if head == marker:
                path.unlink()
                removed.append(path.name)
    suffixes = tuple(f"/bin/{name}," for name in removed)
    for record in sorted(site.glob("*.dist-info/RECORD")):
        lines = record.read_text(encoding="utf-8").splitlines(keepends=True)
        kept = [line for line in lines if not line.startswith("../") or not any(s in line for s in suffixes)]
        record.write_text("".join(kept), encoding="utf-8")
    return removed


def stage(tools: Tools, options: Options, source: Path, lock: dict[str, Any], target: Path, *, version: str) -> None:
    cache = options.work / "cache"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    _extract_tar(fetch_input(tools, cache, "python", lock["python"]), target)
    python = target / "python/bin/python3"
    if not python.is_file():
        raise RunnerError("build_failed", "python")
    pip = [str(python), "-I", "-B", "-m", "pip", "install", "--disable-pip-version-check", "--no-compile"]
    _must(tools, [*pip, "--require-hashes", "--only-binary=:all:", "-r", str(source / REQUIREMENTS)], "pip")
    site = target / SITE
    for package in PACKAGES:
        shutil.copytree(source / "src" / package, site / package, ignore=SKIP)
    drop_scripts(target / "python/bin", target, site)
    _extract_tar(fetch_input(tools, cache, "lima", lock["lima"]), target / "lima")
    stage_postgresql(fetch_input(tools, cache, "postgresql", lock["postgresql"]), target / "postgresql")
    for part in GOLDEN_PARTS:
        shutil.copytree(source / part, target / "golden-src" / part, ignore=SKIP)
    libexec = target / "libexec"
    libexec.mkdir()
    (libexec / "dfx-agent-runner").write_text(LAUNCHER, encoding="utf-8")
    (libexec / "root-helper").write_text(ROOT_HELPER, encoding="utf-8")
    for name in ("dfx-agent-runner", "root-helper"):
        (libexec / name).chmod(0o755)


def compile_python(tools: Tools, tree: Path, version: str) -> None:
    """Byte code of the whole interpreter library with unchecked hashes (no time stamps, so it stays valid after
    `normalise` and is the same in every build) and the installed path as its source path; existing time-stamped
    files of the interpreter's own archive are replaced (`-f`)."""
    library = tree / LIBRARY
    final = Layout().version_dir(version) / LIBRARY
    argv = [str(tree / "python/bin/python3"), "-I", "-B", "-m", "compileall", "-q", "-f", "-j", "0"]
    argv += ["--invalidation-mode", "unchecked-hash", "-s", str(library), "-p", str(final), str(library)]
    _must(tools, argv, "pyc")


def normalise(tree: Path, seconds: int) -> None:
    """Modes 0755/0644 and the commit time on every entry (links keep their own mode)."""
    for directory, dirnames, filenames in os.walk(tree, topdown=False):
        base = Path(directory)
        for name in [*filenames, *dirnames]:
            path = base / name
            if path.is_symlink():
                os.utime(path, (seconds, seconds), follow_symlinks=False)
                continue
            executable = path.is_dir() or path.stat().st_mode & 0o111
            path.chmod(0o755 if executable else 0o644)
            os.utime(path, (seconds, seconds))
    tree.chmod(0o755)
    os.utime(tree, (seconds, seconds))


def macho_files(tree: Path) -> list[Path]:
    found: list[Path] = []
    for directory, _dirnames, filenames in os.walk(tree):
        for name in sorted(filenames):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            with path.open("rb") as handle:
                if handle.read(4) in MACHO:
                    found.append(path)
    return sorted(found)


def entitlements_for(source: Path, path: Path) -> Path | None:
    folder = source / ENTITLEMENTS
    if path.name == "limactl":
        return folder / "limactl.plist"
    if path.name.startswith("python3"):
        return folder / "python.plist"
    return None


def codesign(tools: Tools, options: Options, source: Path, tree: Path) -> int:
    """Sign every Mach-O file (hardened runtime, time stamp); returns their number. No identity: nothing."""
    if options.sign_identity is None:
        return 0
    files = macho_files(tree)
    for path in files:
        argv = ["codesign", "--force", "--options", "runtime", "--timestamp", "--sign", options.sign_identity]
        entitlements = entitlements_for(source, path)
        if entitlements is not None:
            argv += ["--entitlements", str(entitlements)]
        _must(tools, [*argv, str(path)], "codesign")
    return len(files)


def write_manifest(options: Options, tree: Path, version: str, commit: str, lock: dict[str, Any]) -> bytes:
    claude = lock["claude_code"]
    manifest = release.Manifest(
        version=version,
        commit=commit,
        hub=options.hub,
        platform_root_key=options.root_key,
        files=release.tree_entries(tree),
        fetch=(release.Fetch("claude-code", str(claude["url"]), str(claude["sha256"])),),
    )
    data = release.canonical(manifest.document())
    release.manifest_from(json.loads(data))
    (tree / release.MANIFEST).write_bytes(data)
    (tree / (release.MANIFEST + release.SIGNATURE_SUFFIX)).write_text(
        release.sign(data, options.release_key), encoding="ascii"
    )
    return data


def package(tools: Tools, options: Options, tree: Path, version: str, seconds: int) -> Path:
    payload = Layout().version_dir(version)
    scripts = options.work / "scripts"
    if scripts.exists():
        shutil.rmtree(scripts)
    scripts.mkdir(parents=True)
    script = scripts / "postinstall"
    script.write_text(POSTINSTALL.format(payload=payload, version=version), encoding="utf-8")
    script.chmod(0o755)
    normalise(scripts, seconds)
    options.out.mkdir(parents=True, exist_ok=True)
    output = options.out / f"DFX-Agent-Runner-{version}.pkg"
    argv = [
        "pkgbuild",
        "--root",
        str(tree),
        "--identifier",
        PACKAGE_ID,
        "--version",
        version,
        "--install-location",
        str(payload),
        "--scripts",
        str(scripts),
        "--ownership",
        "recommended",
    ]
    if options.installer_identity is not None:
        argv += ["--sign", options.installer_identity, "--timestamp"]
    _must(tools, [*argv, str(output)], "pkgbuild")
    return output


def write_feed(tools: Tools, options: Options, version: str, output: Path, manifest: bytes) -> release.Feed:
    if not 1 <= options.expires_days <= release.FEED_MAX_DAYS:
        raise RunnerError("build_failed", "expires")
    now = tools.now().replace(microsecond=0)
    feed = release.Feed(
        version=version,
        package=output.name,
        package_sha256=release.sha256_file(output),
        manifest_sha256=hashlib.sha256(manifest).hexdigest(),
        min_macos=MIN_MACOS,
        notarized=options.notarized,
        signed_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        expires=(now + timedelta(days=options.expires_days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    data = release.canonical(feed.document())
    release.feed_from(json.loads(data))
    (options.out / release.FEED).write_bytes(data)
    (options.out / (release.FEED + release.SIGNATURE_SUFFIX)).write_text(
        release.sign(data, options.release_key), encoding="ascii"
    )
    (options.out / release.MANIFEST).write_bytes(manifest)
    return feed


def origin(tools: Tools, options: Options) -> tuple[str, str, int, Path]:
    """Commit, version, commit time and the source tree of the build."""
    if options.source is None:
        commit = full_commit(tools, options)
        version, seconds = version_of(tools, options)
        source = options.work / "source"
        export(tools, options, source)
        return commit, version, seconds, source
    if release.COMMIT_RE.fullmatch(options.commit) is None or options.commit_time is None:
        raise RunnerError("build_failed", "source")
    seconds = options.commit_time
    return options.commit, datetime.fromtimestamp(seconds, UTC).strftime("%Y.%m.%d.%H%M"), seconds, options.source


def trust(options: Options, tree: Path) -> None:
    """The release key must be one the payload trusts; `--trust-build-key` adds it (test builds only)."""
    path = tree / SITE / "agent_runner/install" / release.KEYS_MODULE
    text = path.read_text(encoding="utf-8")
    match = KEYS_LINE.search(text)
    if match is None:
        raise RunnerError("build_failed", "release_keys")
    keys = release.keys_from({"keys": list(ast.literal_eval(match.group(1)))})
    own = release.public_key(options.release_key)
    if own in keys:
        return
    if not options.trust_build_key:
        raise RunnerError("build_failed", "release_key_untrusted")
    line = f"KEYS: tuple[str, ...] = {(*keys, own)!r}"
    path.write_text(text[: match.start()] + line + text[match.end() :], encoding="utf-8")


def build(options: Options, tools: Tools) -> Result:
    options.work.mkdir(parents=True, exist_ok=True)
    commit, version, seconds, source = origin(tools, options)
    lock = read_lock(source)
    tree = options.work / "stage"
    stage(tools, options, source, lock, tree, version=version)
    trust(options, tree)
    normalise(tree, seconds)
    compile_python(tools, tree, version)
    normalise(tree, seconds)
    signed = codesign(tools, options, source, tree)
    manifest = write_manifest(options, tree, version, commit, lock)
    normalise(tree, seconds)
    output = package(tools, options, tree, version, seconds)
    feed = write_feed(tools, options, version, output, manifest)
    tools.log.append(f"build version={version} signed_files={signed} package={output.name}")
    return Result(version, output, feed.package_sha256, feed.manifest_sha256)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        raise RunnerError("usage")


def parse(argv: Sequence[str]) -> Options:
    parser = _Parser(prog="agent-runner-build", add_help=False)
    for name in ("--commit", "--hub", "--root-key", "--release-key-file", "--work", "--out"):
        parser.add_argument(name, required=True)
    origin_group = parser.add_mutually_exclusive_group(required=True)
    origin_group.add_argument("--checkout")
    origin_group.add_argument("--source")
    parser.add_argument("--commit-time", type=int)
    parser.add_argument("--sign-identity")
    parser.add_argument("--installer-identity")
    parser.add_argument("--notarized", action="store_true")
    parser.add_argument("--trust-build-key", action="store_true")
    parser.add_argument("--expires-days", type=int, default=90)
    args = parser.parse_args(list(argv))
    try:
        key = b64u_decode(Path(args.release_key_file).read_text(encoding="ascii").strip(), 32)
    except (OSError, UnicodeDecodeError, CryptoError):
        raise RunnerError("build_failed", "release_key") from None
    return Options(
        checkout=Path(args.checkout or "."),
        commit=args.commit,
        hub=args.hub,
        root_key=args.root_key,
        release_key=key,
        work=Path(args.work),
        out=Path(args.out),
        sign_identity=args.sign_identity,
        installer_identity=args.installer_identity,
        notarized=args.notarized,
        expires_days=args.expires_days,
        source=None if args.source is None else Path(args.source),
        commit_time=args.commit_time,
        trust_build_key=args.trust_build_key,
    )


def main(argv: Sequence[str] | None = None, tools: Tools | None = None) -> int:
    used = tools or Tools()
    try:
        result = build(parse(sys.argv[1:] if argv is None else argv), used)
    except RunnerError as error:
        sys.stdout.write(json.dumps({"ok": False, "error": {"code": error.code, "name": error.name}}) + "\n")
        return 1
    sys.stdout.write(
        json.dumps(
            {
                "ok": True,
                "version": result.version,
                "package": str(result.package),
                "package_sha256": result.package_sha256,
                "manifest_sha256": result.manifest_sha256,
            },
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
