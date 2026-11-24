"""U-RUN-install-build: the build of the runner package (ADR-074 section 1): version from the commit time, the commit
exported with `git archive`, the lock checked and its inputs downloaded by digest into a cache, the staged payload
(interpreter, dependencies, packages, Lima, PostgreSQL, golden sources, launchers), normalised modes and times, code
signing before the manifest, the signed `release.json`, `pkgbuild` and the signed, expiring `latest.json`; two builds
of one commit produce the same `release.json`."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import marshal
import os
import stat
import sys
import tarfile
import zipfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agent_runner.crypto.encoding import b64u_encode
from agent_runner.install import build, release

from .conftest import (
    COMMIT,
    HUB,
    NOW,
    ROOT_KEY,
    VERSION,
    FakeFetch,
    FakeRun,
    Keys,
    new_keys,
    refused,
    write_golden_source,
)

SECONDS = int(datetime(2026, 11, 24, 8, 0, tzinfo=UTC).timestamp())
MACHO = b"\xcf\xfa\xed\xfe" + b"\0" * 12
FAT = b"\xca\xfe\xba\xbe" + b"\0" * 12
SITE = "python/lib/python3.12/site-packages"
LIBRARY = "python/lib/python3.12"
FINAL = "/Library/Application Support/DFXAgentRunner/versions/" + VERSION
CLAUDE_URL = "https://registry.example/claude-code-darwin-arm64-2.1.330.tgz"
CLAUDE_SHA = "cd" * 32
IDENTITY = "Developer ID Application: Example (TEAM)"
INSTALLER_IDENTITY = "Developer ID Installer: Example (TEAM)"

Entry = tuple[str, bytes | None, int]


KEYS_MODULE = '"""Release keys."""\n\nKEYS: tuple[str, ...] = {keys}\n'


def tar_gz(entries: list[Entry], links: dict[str, str] | None = None, mode: str = "w:gz") -> bytes:
    """A tarball of `(name, data or None for a directory, mode)` entries and `name -> target` links."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as archive:  # type: ignore[call-overload]
        for name, data, permissions in entries:
            info = tarfile.TarInfo(name)
            info.mode = permissions
            if data is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            archive.addfile(info)
    return buffer.getvalue()


def jar(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in members.items():
            archive.writestr(name, data)
    return buffer.getvalue()


PYTHON = tar_gz(
    [("python/bin/python3", MACHO, 0o755), (SITE, None, 0o755), (LIBRARY + "/os.py", b"# os\n", 0o644)],
    {"python/bin/python3.12": "python3"},
)
LIMA = tar_gz([("bin/limactl", MACHO + b"lima", 0o755), ("share/lima/README", b"lima\n", 0o644)])
POSTGRES_TXZ = tar_gz([("bin/pg_ctl", FAT, 0o755), ("share/postgresql.conf", b"conf\n", 0o644)], mode="w:xz")
POSTGRES = jar({"META-INF/MANIFEST.MF": b"Manifest-Version: 1.0\n", "postgres-darwin-arm_64.txz": POSTGRES_TXZ})
INPUTS = {"python": PYTHON, "lima": LIMA, "postgresql": POSTGRES}


def url(name: str) -> str:
    return f"https://inputs.example/{name}"


def lock_doc(**change: Any) -> dict[str, Any]:
    lock: dict[str, Any] = {"v": 1}
    for name, data in INPUTS.items():
        lock[name] = {"url": url(name), "sha256": hashlib.sha256(data).hexdigest()}
    lock["claude_code"] = {"version": "2.1.330", "url": CLAUDE_URL, "sha256": CLAUDE_SHA}
    lock.update(change)
    return lock


def write_source(tree: Path, lock: dict[str, Any] | None = None) -> None:
    """A minimal tree of the commit: lock, requirements, entitlements, packages and golden sources."""
    write_golden_source(tree)
    (tree / "images/.DS_Store").write_bytes(b"finder")
    macos = tree / "scripts/macos-package"
    (macos / "entitlements").mkdir(parents=True)
    (macos / "lock.json").write_text(json.dumps(lock or lock_doc()), encoding="utf-8")
    (macos / "requirements.lock").write_text("dep==1.0 --hash=sha256:" + "0" * 64 + "\n", encoding="utf-8")
    for name in ("limactl", "python"):
        (macos / "entitlements" / f"{name}.plist").write_text("<plist/>\n", encoding="utf-8")
    (tree / "src/agent_runner/install").mkdir(parents=True)
    (tree / "src/agent_runner/__init__.py").write_text("", encoding="utf-8")
    (tree / "src/agent_runner/install/release_keys.py").write_text(KEYS_MODULE.format(keys="()"), encoding="utf-8")
    (tree / "src/agent_runner/__pycache__").mkdir()
    (tree / "src/agent_runner/__pycache__/x.cpython-312.pyc").write_bytes(b"stale")
    (tree / "src/agent_cli").mkdir()
    (tree / "src/agent_cli/__init__.py").write_text('"""cli"""\n', encoding="utf-8")


@dataclass
class Bench:
    options: build.Options
    tools: build.Tools
    run: FakeRun
    fetch: FakeFetch
    checkout: Path
    keys: Keys

    @property
    def stage(self) -> Path:
        return self.options.work / "stage"

    @property
    def python(self) -> str:
        return str(self.stage / "python/bin/python3")


def compile_argv(python: str) -> list[str]:
    """The start of the command that compiles one file of the interpreter library."""
    return ["/usr/bin/env", "-i", python, "-I", "-S", "-B", "-X", "utf8", "-X", "pycache_prefix=/dev/null", "-c"]


def calls_besides_compiling(bench: Bench) -> list[list[str]]:
    """The calls of a build except those compiling the library, after checking that every Python file of the staged
    library was compiled by its own call, to its cache and installed path."""
    stage = bench.stage
    compiling = compile_argv(bench.python)
    compiles = bench.run.called(*compiling)
    sources = build.python_sources(stage / LIBRARY)
    assert stage / SITE / "agent_runner/__init__.py" in sources
    assert sorted(compiles) == sorted(
        [
            *compiling,
            build.COMPILE_ONE,
            str(source),
            str(source.parent / "__pycache__" / f"{source.stem}.cpython-312.pyc"),
            f"{FINAL}/{LIBRARY}/{source.relative_to(stage / LIBRARY)}",
        ]
        for source in sources
    )
    return [call for call in bench.run.calls if call not in compiles]


def pip_install(stage: Path) -> None:
    """What `pip install` writes: a package, its RECORD and two console scripts (one naming the build directory)."""
    site = stage / SITE
    (site / "dep").mkdir(parents=True)
    (site / "dep/__init__.py").write_text("dep = 1\n", encoding="utf-8")
    (site / "dep-1.0.dist-info").mkdir()
    (site / "dep-1.0.dist-info/RECORD").write_text(
        "dep/__init__.py,sha256=a,8\n../../../bin/dep-tool,sha256=b,40\n../../../bin/keep,sha256=c,10\n",
        encoding="utf-8",
    )
    (stage / "python/bin/dep-tool").write_text(f"#!{stage}/python/bin/python3\nimport dep\n", encoding="utf-8")
    (stage / "python/bin/keep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")


def make_bench(tmp_path: Path, keys: Keys, name: str = "a", **change: Any) -> Bench:
    checkout = tmp_path / "checkout"
    if not checkout.exists():
        write_source(checkout)
    options = build.Options(
        checkout=checkout,
        commit="v1",
        hub=HUB,
        root_key=ROOT_KEY,
        release_key=keys.private,
        work=tmp_path / f"work-{name}",
        out=tmp_path / f"out-{name}",
        trust_build_key=True,
    )
    options = replace(options, **change)
    run = FakeRun()
    fetch = FakeFetch({url(item): data for item, data in INPUTS.items()})
    tools = build.Tools(run=run, fetch=fetch, now=lambda: NOW)
    bench = Bench(options, tools, run, fetch, checkout, keys)
    stage = bench.stage

    def archive(argv: list[str]) -> None:
        with tarfile.open(argv[argv.index("-o") + 1], "w") as handle:
            for child in sorted(checkout.iterdir()):
                handle.add(child, arcname=child.name)

    def package(argv: list[str]) -> None:
        Path(argv[-1]).write_bytes(release.canonical(release.tree_entries(Path(argv[2]))))

    def compiled(argv: list[str]) -> None:
        target = Path(argv[-2])
        target.parent.mkdir(exist_ok=True)
        target.write_bytes(b"unchecked-hash pyc")

    run.answer(["git", "-C", str(checkout), "show"], stdout=f"{COMMIT} {SECONDS}\n")
    run.answer(["git", "-C", str(checkout), "rev-parse"], stdout=COMMIT + "\n")
    run.on(["git", "-C", str(checkout), "archive"], archive)
    run.on([bench.python, "-I", "-B", "-m", "pip"], lambda argv: pip_install(stage))
    run.on(compile_argv(bench.python), compiled)
    run.on(["pkgbuild"], package)
    return bench


def read_json(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


# --- the whole build ------------------------------------------------------------------------------------------------


def test_a_build_from_a_checkout(tmp_path: Path, keys: Keys) -> None:
    bench = make_bench(tmp_path, keys)
    stale = bench.options.work / "source/stale"
    stale.parent.mkdir(parents=True)
    stale.write_text("x", encoding="utf-8")
    result = build.build(bench.options, bench.tools)
    out = bench.options.out
    name = f"DFX-Agent-Runner-{VERSION}.pkg"
    assert result == build.Result(
        VERSION,
        out / name,
        release.sha256_file(out / name),
        hashlib.sha256((out / "release.json").read_bytes()).hexdigest(),
    )
    assert sorted(item.name for item in out.iterdir()) == [name, "latest.json", "latest.json.sig", "release.json"]
    work = bench.options.work
    source = work / "source"
    stage = bench.stage
    scripts = work / "scripts"
    assert calls_besides_compiling(bench) == [
        ["git", "-C", str(bench.checkout), "rev-parse", "v1^{commit}"],
        ["git", "-C", str(bench.checkout), "show", "-s", "--format=%H %ct", "v1"],
        ["git", "-C", str(bench.checkout), "archive", "-o", str(work / "source.tar"), "v1"],
        [
            bench.python,
            "-I",
            "-B",
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-compile",
            "--require-hashes",
            "--only-binary=:all:",
            "-r",
            str(source / "scripts/macos-package/requirements.lock"),
        ],
        [
            "pkgbuild",
            "--root",
            str(stage),
            "--identifier",
            "swiss.dfx.agent-runner",
            "--version",
            VERSION,
            "--install-location",
            FINAL,
            "--scripts",
            str(scripts),
            "--ownership",
            "recommended",
            str(out / name),
        ],
    ]
    assert bench.fetch.urls == [url("python"), url("lima"), url("postgresql")]
    assert not stale.exists()
    assert not (work / "source.tar").exists()
    assert bench.tools.log == [f"build version={VERSION} signed_files=0 package={name}"]

    # the signed manifest lists the staged payload
    manifest = release.read_manifest(stage, [keys.public])
    assert (out / "release.json").read_bytes() == (stage / "release.json").read_bytes()
    assert (manifest.version, manifest.commit, manifest.hub, manifest.platform_root_key) == (
        VERSION,
        COMMIT,
        HUB,
        ROOT_KEY,
    )
    assert manifest.fetch == (release.Fetch("claude-code", CLAUDE_URL, CLAUDE_SHA),)
    files = manifest.files
    assert files["python/bin/python3"] == hashlib.sha256(MACHO).hexdigest()
    assert files["python/bin/python3.12"] == "link:python3"
    for path in (
        "lima/bin/limactl",
        "lima/share/lima/README",
        "postgresql/bin/pg_ctl",
        "postgresql/share/postgresql.conf",
        "golden-src/images/golden.yaml",
        "golden-src/images/versions.json",
        "golden-src/src/agent_guest/__init__.py",
        "libexec/dfx-agent-runner",
        "libexec/root-helper",
        f"{SITE}/agent_runner/__init__.py",
        f"{SITE}/agent_runner/__pycache__/__init__.cpython-312.pyc",
        f"{SITE}/agent_cli/__init__.py",
        f"{SITE}/dep/__init__.py",
        "python/bin/keep",
    ):
        assert path in files, path
    for path in (
        "python/bin/dep-tool",
        f"{SITE}/agent_runner/__pycache__/x.cpython-312.pyc",
        "golden-src/images/.DS_Store",
        "golden-src/packaging",
    ):
        assert path not in files, path
    assert not any(path.startswith(("packaging/", "src/")) for path in files)
    record = (stage / SITE / "dep-1.0.dist-info/RECORD").read_text(encoding="utf-8")
    assert record == "dep/__init__.py,sha256=a,8\n../../../bin/keep,sha256=c,10\n"
    trusted = (stage / SITE / "agent_runner/install/release_keys.py").read_text(encoding="utf-8")
    assert trusted == KEYS_MODULE.format(keys=repr((keys.public,)))
    assert (stage / "libexec/dfx-agent-runner").read_text(encoding="utf-8") == build.LAUNCHER
    assert (stage / "libexec/root-helper").read_text(encoding="utf-8") == build.ROOT_HELPER

    # modes and times are normalised
    for directory, dirnames, filenames in os.walk(stage):
        for entry in [*dirnames, *filenames]:
            staged = Path(directory) / entry
            found = staged.lstat()
            assert found.st_mtime == SECONDS, staged
            if not stat.S_ISLNK(found.st_mode):
                assert stat.S_IMODE(found.st_mode) in (0o755, 0o644), staged
    assert stat.S_IMODE((stage / "libexec/root-helper").stat().st_mode) == 0o755
    assert stat.S_IMODE((stage / "golden-src/images/golden.yaml").stat().st_mode) == 0o644
    assert stat.S_IMODE((stage / "release.json").stat().st_mode) == 0o644

    # the postinstall script runs the root helper of the installed payload
    script = scripts / "postinstall"
    assert script.read_text(encoding="utf-8") == build.POSTINSTALL.format(payload=FINAL, version=VERSION)
    assert f'exec "{FINAL}/libexec/root-helper" postinstall --version "{VERSION}"' in script.read_text(encoding="utf-8")
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    assert script.stat().st_mtime == SECONDS

    # the feed names the package and expires after 90 days
    data = (out / "latest.json").read_bytes()
    feed = release.feed_from(release.verify(data, (out / "latest.json.sig").read_text(encoding="ascii"), [keys.public]))
    assert feed == release.Feed(
        version=VERSION,
        package=name,
        package_sha256=result.package_sha256,
        manifest_sha256=result.manifest_sha256,
        min_macos="14.0",
        notarized=False,
        signed_at="2026-11-24T12:00:00Z",
        expires="2027-02-22T12:00:00Z",
    )


def test_two_builds_of_one_commit_produce_the_same_release(tmp_path: Path, keys: Keys) -> None:
    first = make_bench(tmp_path, keys, "a")
    second = make_bench(tmp_path, keys, "b")
    one = build.build(first.options, first.tools)
    two = build.build(second.options, second.tools)
    assert (first.options.out / "release.json").read_bytes() == (second.options.out / "release.json").read_bytes()
    assert (first.stage / "release.json.sig").read_bytes() == (second.stage / "release.json.sig").read_bytes()
    assert (one.version, one.manifest_sha256, one.package_sha256) == (
        two.version,
        two.manifest_sha256,
        two.package_sha256,
    )
    # a second build in the same work directory reuses the cached inputs and replaces the stage
    again = build.build(first.options, first.tools)
    assert again == one
    assert first.fetch.urls == [url("python"), url("lima"), url("postgresql")]


def test_a_build_from_an_exported_tree_needs_no_git(tmp_path: Path, keys: Keys) -> None:
    bench = make_bench(tmp_path, keys)
    options = replace(bench.options, source=bench.checkout, commit=COMMIT, commit_time=SECONDS)
    result = build.build(options, bench.tools)
    assert result.version == VERSION
    assert bench.run.called("git") == []
    assert release.read_manifest(bench.stage, [keys.public]).commit == COMMIT


def test_code_signing_happens_before_the_manifest(tmp_path: Path, keys: Keys) -> None:
    bench = make_bench(tmp_path, keys, sign_identity=IDENTITY, installer_identity=INSTALLER_IDENTITY)

    def sign(argv: list[str]) -> None:
        with open(argv[-1], "ab") as handle:
            handle.write(b"+signature")

    bench.run.on(["codesign"], sign)
    build.build(bench.options, bench.tools)
    stage = bench.stage
    entitlements = bench.options.work / "source/scripts/macos-package/entitlements"
    base = ["codesign", "--force", "--options", "runtime", "--timestamp", "--sign", IDENTITY]
    assert bench.run.called("codesign") == [
        [*base, "--entitlements", str(entitlements / "limactl.plist"), str(stage / "lima/bin/limactl")],
        [*base, str(stage / "postgresql/bin/pg_ctl")],
        [*base, "--entitlements", str(entitlements / "python.plist"), str(stage / "python/bin/python3")],
    ]
    files = release.read_manifest(stage, [keys.public]).files
    assert files["python/bin/python3"] == hashlib.sha256(MACHO + b"+signature").hexdigest()
    assert files["postgresql/bin/pg_ctl"] == hashlib.sha256(FAT + b"+signature").hexdigest()
    pkgbuild = bench.run.called("pkgbuild")[0]
    assert pkgbuild[-4:-1] == ["--sign", INSTALLER_IDENTITY, "--timestamp"]
    steps = [call[0] for call in bench.run.calls]
    assert steps.index("pkgbuild") > max(index for index, step in enumerate(steps) if step == "codesign")
    assert bench.tools.log[-1].endswith("signed_files=3 package=" + f"DFX-Agent-Runner-{VERSION}.pkg")


def test_a_notarised_release_with_the_longest_expiry(tmp_path: Path, keys: Keys) -> None:
    bench = make_bench(tmp_path, keys, notarized=True, expires_days=120)
    build.build(bench.options, bench.tools)
    feed = read_json(bench.options.out / "latest.json")
    assert (feed["notarized"], feed["expires"]) == (True, "2027-03-24T12:00:00Z")


@pytest.mark.parametrize("days", [0, 121])
def test_an_expiry_outside_the_limits_is_refused(tmp_path: Path, keys: Keys, days: int) -> None:
    bench = make_bench(tmp_path, keys, expires_days=days)
    with refused("build_failed", "expires"):
        build.build(bench.options, bench.tools)
    assert not (bench.options.out / "latest.json").exists()


# --- the steps ------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rc", "stdout"),
    [(128, ""), (0, ""), (0, f"{COMMIT}\n"), (0, f"{COMMIT} 12x\n"), (0, f"abc {SECONDS}\n"), (0, f"{COMMIT} 1 2\n")],
)
def test_an_unusable_commit_is_refused(tmp_path: Path, keys: Keys, rc: int, stdout: str) -> None:
    bench = make_bench(tmp_path, keys)
    bench.run.answer(["git", "-C", str(bench.checkout), "show"], rc=rc, stdout=stdout)
    with refused("build_failed", "git"):
        build.version_of(bench.tools, bench.options)


def test_the_version_is_the_commit_time_in_utc(tmp_path: Path, keys: Keys) -> None:
    bench = make_bench(tmp_path, keys)
    assert build.version_of(bench.tools, bench.options) == (VERSION, SECONDS)
    assert build.full_commit(bench.tools, bench.options) == COMMIT
    bench.run.answer(["git", "-C", str(bench.checkout), "rev-parse"], rc=128)
    with refused("build_failed", "git"):
        build.full_commit(bench.tools, bench.options)


def test_a_failed_export_is_refused(tmp_path: Path, keys: Keys) -> None:
    bench = make_bench(tmp_path, keys)
    bench.options.work.mkdir()
    bench.run.answer(["git", "-C", str(bench.checkout), "archive"], rc=128)
    with refused("build_failed", "git"):
        build.export(bench.tools, bench.options, tmp_path / "target")
    assert not (tmp_path / "target").exists()


@pytest.mark.parametrize(
    "commit_time",
    [None, SECONDS],
)
def test_an_exported_tree_needs_a_full_commit_and_its_time(tmp_path: Path, keys: Keys, commit_time: int | None) -> None:
    bench = make_bench(tmp_path, keys)
    commit = COMMIT if commit_time is None else "v1"
    options = replace(bench.options, source=bench.checkout, commit=commit, commit_time=commit_time)
    with refused("build_failed", "source"):
        build.origin(bench.tools, options)
    assert bench.run.calls == []


def test_the_lock_is_read(tmp_path: Path) -> None:
    write_source(tmp_path)
    assert build.read_lock(tmp_path) == lock_doc()


@pytest.mark.parametrize(
    "lock",
    [
        [],
        {**lock_doc(), "v": 2},
        {key: value for key, value in lock_doc().items() if key != "lima"},
        lock_doc(lima="https://x"),
        lock_doc(lima={"url": "http://inputs.example/lima", "sha256": "ab" * 32}),
        lock_doc(lima={"sha256": "ab" * 32}),
        lock_doc(lima={"url": url("lima"), "sha256": "AB" * 32}),
        lock_doc(lima={"url": url("lima")}),
    ],
)
def test_an_invalid_lock_is_refused(tmp_path: Path, lock: Any) -> None:
    write_source(tmp_path)
    (tmp_path / "scripts/macos-package/lock.json").write_text(json.dumps(lock), encoding="utf-8")
    with refused("build_failed", "lock"):
        build.read_lock(tmp_path)


@pytest.mark.parametrize("broken", ["scripts/macos-package/lock.json", "images/versions.json"])
def test_a_missing_or_malformed_lock_is_refused(tmp_path: Path, broken: str) -> None:
    write_source(tmp_path)
    (tmp_path / broken).write_text("{", encoding="utf-8")
    with refused("build_failed", "lock"):
        build.read_lock(tmp_path)
    (tmp_path / broken).unlink()
    with refused("build_failed", "lock"):
        build.read_lock(tmp_path)


def test_the_claude_code_pin_must_match_the_golden(tmp_path: Path) -> None:
    claude = {"version": "2.1.329", "url": CLAUDE_URL, "sha256": CLAUDE_SHA}
    write_source(tmp_path, lock_doc(claude_code=claude))
    with refused("build_failed", "claude_pin"):
        build.read_lock(tmp_path)


def test_inputs_are_downloaded_once_and_checked(tmp_path: Path) -> None:
    fetch = FakeFetch({url("lima"): LIMA})
    tools = build.Tools(run=FakeRun(), fetch=fetch, now=lambda: NOW)
    digest = hashlib.sha256(LIMA).hexdigest()
    item = {"url": url("lima"), "sha256": digest}
    cache = tmp_path / "cache"
    path = build.fetch_input(tools, cache, "lima", item)
    assert path == cache / f"lima-{digest[:16]}"
    assert path.read_bytes() == LIMA
    assert build.fetch_input(tools, cache, "lima", item) == path
    assert fetch.urls == [url("lima")]
    path.write_bytes(b"damaged")
    assert build.fetch_input(tools, cache, "lima", item).read_bytes() == LIMA
    assert fetch.urls == [url("lima")] * 2
    with refused("build_failed", "digest_lima"):
        build.fetch_input(tools, cache, "lima", {"url": url("lima"), "sha256": "ab" * 32})
    assert not (cache / f"lima-{'ab' * 8}").exists()


@pytest.mark.parametrize(
    "members",
    [{"META-INF/MANIFEST.MF": b""}, {"a.txz": POSTGRES_TXZ, "b.txz": POSTGRES_TXZ}],
)
def test_the_postgresql_jar_holds_exactly_one_archive(tmp_path: Path, members: dict[str, bytes]) -> None:
    (tmp_path / "pg.jar").write_bytes(jar(members))
    with refused("build_failed", "postgresql"):
        build.stage_postgresql(tmp_path / "pg.jar", tmp_path / "postgresql")
    assert not (tmp_path / "postgresql").exists()


def test_console_scripts_naming_the_build_directory_are_dropped(tmp_path: Path) -> None:
    stage = tmp_path / "stage"
    (stage / "python/bin/sub").mkdir(parents=True)
    pip_install(stage)
    (stage / "python/bin/link").symlink_to("dep-tool")
    (stage / "python/bin/other-tool").write_text("#!/opt/other/bin/python3\n", encoding="utf-8")
    site = stage / SITE
    (site / "other-2.0.dist-info").mkdir()
    (site / "other-2.0.dist-info/RECORD").write_text(
        "../../../bin/other-tool,sha256=d,1\nbin/dep-tool,sha256=e,1\n", encoding="utf-8"
    )
    assert build.drop_scripts(stage / "python/bin", stage, site) == ["dep-tool"]
    assert sorted(item.name for item in (stage / "python/bin").iterdir()) == ["keep", "link", "other-tool", "sub"]
    assert (site / "dep-1.0.dist-info/RECORD").read_text(encoding="utf-8") == (
        "dep/__init__.py,sha256=a,8\n../../../bin/keep,sha256=c,10\n"
    )
    assert (site / "other-2.0.dist-info/RECORD").read_text(encoding="utf-8") == (
        "../../../bin/other-tool,sha256=d,1\nbin/dep-tool,sha256=e,1\n"
    )


def test_an_interpreter_archive_without_python3_is_refused(tmp_path: Path, keys: Keys) -> None:
    bench = make_bench(tmp_path, keys)
    broken = tar_gz([("python/bin/python", MACHO, 0o755)])
    lock = lock_doc(python={"url": url("python"), "sha256": hashlib.sha256(broken).hexdigest()})
    (bench.checkout / "scripts/macos-package/lock.json").write_text(json.dumps(lock), encoding="utf-8")
    bench.fetch.files[url("python")] = broken
    with refused("build_failed", "python"):
        build.build(bench.options, bench.tools)
    assert bench.run.called(bench.python) == []


@pytest.mark.parametrize(
    ("program", "step"), [(("<python>", "-I", "-B", "-m", "pip"), "pip"), (("/usr/bin/env", "-i", "<python>"), "pyc")]
)
def test_a_failed_python_step_is_refused(tmp_path: Path, keys: Keys, program: tuple[str, ...], step: str) -> None:
    bench = make_bench(tmp_path, keys)
    bench.run.answer([bench.python if part == "<python>" else part for part in program], rc=1)
    with refused("build_failed", step):
        build.build(bench.options, bench.tools)
    assert bench.run.called("pkgbuild") == []


# --- byte code independent of the compiling process (HOT-19, E-19) -------------------------------------------------

USES_THE_NAME = "def use(quincunx):\n    return quincunx\n"
SHARES_A_SET = (
    'def first(value):\n    return value in {"quincunx", "zeugma"}\n\n\n'
    'def second(value):\n    return value in {"quincunx", "zeugma"} or value == 2\n'
)


def compiled_library(
    tmp_path: Path, name: str, files: dict[str, str], tools: build.Tools | None = None
) -> dict[str, bytes]:
    """`compile_python` with the real interpreter on a library of `files`; every resulting `.pyc` by path."""
    tree = tmp_path / name / "stage"
    (tree / "python/bin").mkdir(parents=True)
    python = tree / "python/bin/python3"
    python.write_text(f'#!/bin/sh\nexec "{Path(sys.executable).resolve()}" "$@"\n', encoding="utf-8")
    python.chmod(0o755)
    for path, text in files.items():
        (tree / LIBRARY / path).parent.mkdir(parents=True, exist_ok=True)
        (tree / LIBRARY / path).write_text(text, encoding="utf-8")
    build.compile_python(tools or build.Tools(), tree, VERSION)
    return {str(path.relative_to(tree)): path.read_bytes() for path in sorted(tree.rglob("*.pyc"))}


def test_byte_code_does_not_depend_on_what_the_compiling_process_compiled_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two builds of one commit differed in one `.pyc` (HOT-19): the parser interns every identifier for the life of
    the process, and a code object rebuilds a constant `frozenset` whose strings are already interned, so whether
    `marshal` shares it (`FLAG_REF`) depended on the files the same worker of `compileall -j 0` had compiled before.
    Here 150 files use `quincunx` as a name (a word no module of the interpreter uses) and one file holds
    `{"quincunx", "zeugma"}` in two functions with different constants: with many workers that file lands in one
    that has seen the name, alone it does not. Every file must come out the same as when compiled alone, and two
    compilations of the same library must be the same in every byte."""
    library = {f"site-packages/a{index:03}.py": USES_THE_NAME for index in range(150)}
    library["site-packages/zz.py"] = SHARES_A_SET
    alone = compiled_library(tmp_path, "alone", {"site-packages/zz.py": SHARES_A_SET})
    first = compiled_library(tmp_path, "first", library)
    # another locale (it would choose the codec modules a compiling interpreter imports at start)
    monkeypatch.setenv("LC_ALL", "en_US.ISO8859-1")
    second = compiled_library(tmp_path, "second", library)
    assert len(first) == 151
    assert first == second
    pyc = f"{LIBRARY}/site-packages/__pycache__/zz.cpython-312.pyc"
    assert first[pyc] == alone[pyc]
    # a hash-based pyc that is never checked against its source, naming the installed path
    data = first[pyc]
    assert data[:4] == importlib.util.MAGIC_NUMBER
    assert int.from_bytes(data[4:8], "little") == 1
    assert data[8:16] == importlib.util.source_hash(SHARES_A_SET.encode())
    code = marshal.loads(data[16:])
    assert code.co_filename == f"{FINAL}/{LIBRARY}/site-packages/zz.py"
    namespace: dict[str, Any] = {}
    exec(code, namespace)
    assert namespace["first"]("zeugma")
    assert not namespace["second"]("other")


def test_a_library_file_that_does_not_compile_is_refused_and_named(tmp_path: Path) -> None:
    tools = build.Tools()
    with refused("build_failed", "pyc"):
        compiled_library(tmp_path, "broken", {"good.py": "x = 1\n", "sub/bad.py": "def (:\n"}, tools)
    assert tools.log == ["pyc_failed sub/bad.py"]


def test_python_sources_are_found_in_a_fixed_order_without_caches_or_linked_directories(tmp_path: Path) -> None:
    library = tmp_path / "lib"
    for path in ("b.py", "a.py", "notes.txt", "z/__init__.py", "m/x.py", "__pycache__/c.py"):
        (library / path).parent.mkdir(parents=True, exist_ok=True)
        (library / path).write_text("", encoding="utf-8")
    (library / "linked").symlink_to(library / "m")
    (library / "dangling.py").symlink_to(library / "missing.py")
    (library / "folder.py").mkdir()
    (library / "y.py").symlink_to(library / "a.py")
    expected = ("a.py", "b.py", "y.py", "m/x.py", "z/__init__.py")
    assert build.python_sources(library) == [library / name for name in expected]


def test_normalise_sets_modes_and_the_commit_time(tmp_path: Path) -> None:
    tree = tmp_path / "tree"
    (tree / "dir").mkdir(parents=True)
    (tree / "dir").chmod(0o700)
    (tree / "tool").write_bytes(b"#!/bin/sh\n")
    (tree / "tool").chmod(0o700)
    (tree / "data").write_bytes(b"x")
    (tree / "data").chmod(0o600)
    (tree / "dir/link").symlink_to("../data")
    tree.chmod(0o700)
    build.normalise(tree, SECONDS)
    modes = {path: stat.S_IMODE((tree / path).stat().st_mode) for path in ("dir", "tool", "data")}
    assert modes == {"dir": 0o755, "tool": 0o755, "data": 0o644}
    assert stat.S_IMODE(tree.stat().st_mode) == 0o755
    for path in (tree, tree / "dir", tree / "tool", tree / "data", tree / "dir/link"):
        assert path.lstat().st_mtime == SECONDS
    assert os.readlink(tree / "dir/link") == "../data"


def test_mach_o_files_are_found_by_their_magic(tmp_path: Path) -> None:
    (tmp_path / "b").mkdir()
    for name, data in (
        ("b/thin", MACHO),
        ("fat", FAT),
        ("b/big-endian", b"\xfe\xed\xfa\xcf" + b"\0" * 4),
        ("b/fat-swapped", b"\xbe\xba\xfe\xca"),
        ("script", b"#!/bin/sh\n"),
        ("short", b"\xcf\xfa"),
    ):
        (tmp_path / name).write_bytes(data)
    (tmp_path / "link").symlink_to("fat")
    assert build.macho_files(tmp_path) == sorted(
        [tmp_path / "b/big-endian", tmp_path / "b/fat-swapped", tmp_path / "b/thin", tmp_path / "fat"]
    )


def test_entitlements_by_program(tmp_path: Path) -> None:
    folder = tmp_path / "scripts/macos-package/entitlements"
    assert build.entitlements_for(tmp_path, Path("lima/bin/limactl")) == folder / "limactl.plist"
    assert build.entitlements_for(tmp_path, Path("python/bin/python3")) == folder / "python.plist"
    assert build.entitlements_for(tmp_path, Path("python/bin/python3.12")) == folder / "python.plist"
    assert build.entitlements_for(tmp_path, Path("postgresql/bin/postgres")) is None


def test_no_identity_signs_nothing_and_a_failed_signature_is_refused(tmp_path: Path, keys: Keys) -> None:
    bench = make_bench(tmp_path, keys)
    (tmp_path / "tree").mkdir()
    (tmp_path / "tree/tool").write_bytes(MACHO)
    assert build.codesign(bench.tools, bench.options, tmp_path, tmp_path / "tree") == 0
    assert bench.run.calls == []
    signing = replace(bench.options, sign_identity=IDENTITY)
    assert build.codesign(bench.tools, signing, tmp_path, tmp_path / "tree") == 1
    bench.run.answer(["codesign"], rc=1)
    with refused("build_failed", "codesign"):
        build.codesign(bench.tools, signing, tmp_path, tmp_path / "tree")


def test_package_replaces_old_scripts_and_reports_a_failed_pkgbuild(tmp_path: Path, keys: Keys) -> None:
    bench = make_bench(tmp_path, keys)
    scripts = bench.options.work / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "preinstall").write_text("old", encoding="utf-8")
    tree = tmp_path / "tree"
    tree.mkdir()
    bench.run.answer(["pkgbuild"], rc=1)
    with refused("build_failed", "pkgbuild"):
        build.package(bench.tools, bench.options, tree, VERSION, SECONDS)
    assert sorted(item.name for item in scripts.iterdir()) == ["postinstall"]
    assert "--sign" not in bench.run.calls[-1]


def test_a_key_module_without_its_list_is_refused(tmp_path: Path, keys: Keys) -> None:
    path = tmp_path / "tree" / SITE / "agent_runner/install/release_keys.py"
    path.parent.mkdir(parents=True)
    path.write_text('"""No list."""\n', encoding="utf-8")
    with refused("build_failed", "release_keys"):
        build.trust(make_bench(tmp_path, keys).options, tmp_path / "tree")


def test_the_release_key_must_be_trusted_by_the_payload(tmp_path: Path, keys: Keys) -> None:
    tree = tmp_path / "tree"
    path = tree / SITE / "agent_runner/install/release_keys.py"
    path.parent.mkdir(parents=True)
    other = new_keys().public
    path.write_text(KEYS_MODULE.format(keys=repr((other,))), encoding="utf-8")
    options = make_bench(tmp_path, keys, trust_build_key=False).options
    with refused("build_failed", "release_key_untrusted"):
        build.trust(options, tree)
    build.trust(replace(options, trust_build_key=True), tree)
    assert path.read_text(encoding="utf-8") == KEYS_MODULE.format(keys=repr((other, keys.public)))
    text = path.read_text(encoding="utf-8")
    build.trust(options, tree)  # trusted now: unchanged, also without the flag
    assert path.read_text(encoding="utf-8") == text


def test_an_untrusted_release_key_stops_the_build(tmp_path: Path, keys: Keys) -> None:
    bench = make_bench(tmp_path, keys, trust_build_key=False)
    with refused("build_failed", "release_key_untrusted"):
        build.build(bench.options, bench.tools)
    assert not bench.options.out.exists()


# --- the command line -----------------------------------------------------------------------------------------------


def key_file(tmp_path: Path, keys: Keys) -> Path:
    path = tmp_path / "release.key"
    path.write_text(b64u_encode(keys.private) + "\n", encoding="ascii")
    return path


def required(tmp_path: Path, keys: Keys) -> list[str]:
    return [
        "--commit",
        "v1",
        "--hub",
        HUB,
        "--root-key",
        ROOT_KEY,
        "--release-key-file",
        str(key_file(tmp_path, keys)),
        "--work",
        str(tmp_path / "work"),
        "--out",
        str(tmp_path / "out"),
    ]


def test_parse_every_option(tmp_path: Path, keys: Keys) -> None:
    argv = [
        *required(tmp_path, keys),
        "--source",
        str(tmp_path / "src"),
        "--commit-time",
        str(SECONDS),
        "--sign-identity",
        IDENTITY,
        "--installer-identity",
        INSTALLER_IDENTITY,
        "--notarized",
        "--trust-build-key",
        "--expires-days",
        "30",
    ]
    assert build.parse(argv) == build.Options(
        checkout=Path("."),
        commit="v1",
        hub=HUB,
        root_key=ROOT_KEY,
        release_key=keys.private,
        work=tmp_path / "work",
        out=tmp_path / "out",
        sign_identity=IDENTITY,
        installer_identity=INSTALLER_IDENTITY,
        notarized=True,
        expires_days=30,
        source=tmp_path / "src",
        commit_time=SECONDS,
        trust_build_key=True,
    )


def test_parse_the_defaults(tmp_path: Path, keys: Keys) -> None:
    options = build.parse([*required(tmp_path, keys), "--checkout", str(tmp_path / "repo")])
    assert options.checkout == tmp_path / "repo"
    assert (options.source, options.commit_time, options.sign_identity, options.installer_identity) == (
        None,
        None,
        None,
        None,
    )
    assert (options.notarized, options.trust_build_key, options.expires_days) == (False, False, 90)


@pytest.mark.parametrize(
    "extra",
    [[], ["--checkout", "a", "--source", "b"], ["--checkout", "a", "--expires-days", "x"], ["--checkout", "a", "-x"]],
)
def test_other_command_lines_are_usage_errors(tmp_path: Path, keys: Keys, extra: list[str]) -> None:
    with refused("usage"):
        build.parse([*required(tmp_path, keys), *extra])
    with refused("usage"):
        build.parse(["--checkout", "a"])


@pytest.mark.parametrize("content", [None, b"\xff\xfe", b"not base64!", b64u_encode(b"short").encode("ascii")])
def test_an_unreadable_release_key_is_refused(tmp_path: Path, keys: Keys, content: bytes | None) -> None:
    argv = [*required(tmp_path, keys), "--checkout", "."]
    path = tmp_path / "release.key"
    if content is None:
        path.unlink()
    else:
        path.write_bytes(content)
    with refused("build_failed", "release_key"):
        build.parse(argv)


def test_main_prints_the_result(tmp_path: Path, keys: Keys, capsys: pytest.CaptureFixture[str]) -> None:
    bench = make_bench(tmp_path, keys)
    argv = [
        *required(tmp_path, keys),
        "--checkout",
        str(bench.checkout),
        "--trust-build-key",
    ]
    argv[argv.index("--work") + 1] = str(bench.options.work)
    assert build.main(argv, bench.tools) == 0
    document = json.loads(capsys.readouterr().out)
    package = tmp_path / "out" / f"DFX-Agent-Runner-{VERSION}.pkg"
    assert document == {
        "ok": True,
        "version": VERSION,
        "package": str(package),
        "package_sha256": release.sha256_file(package),
        "manifest_sha256": hashlib.sha256((tmp_path / "out/release.json").read_bytes()).hexdigest(),
    }


def test_main_reads_sys_argv_and_reports_errors(
    tmp_path: Path, keys: Keys, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bench = make_bench(tmp_path, keys)
    monkeypatch.setattr(sys, "argv", ["agent-runner-build", "--checkout", "."])
    assert build.main(tools=bench.tools) == 1
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": {"code": "usage", "name": None}}
    argv = [*required(tmp_path, keys), "--checkout", str(bench.checkout)]
    assert build.main(argv, bench.tools) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "error": {"code": "build_failed", "name": "release_key_untrusted"},
    }
    assert bench.run.called("pkgbuild") == []
