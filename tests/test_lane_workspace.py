import json
import os
from pathlib import Path

import pytest

from agent_cli.lane_protocol import ProtocolError
from agent_cli.lane_workspace import Workspace


def _recovery_dirs(root: Path) -> list[Path]:
    # Each pytest workspace has siblings; never borrow another test's evidence.
    found = []
    for directory in root.parent.glob(".agent-source-recovery-*"):
        index = directory / "recovery-index.json"
        if index.is_file() and json.loads(index.read_text())["source_root"] == str(root.resolve()):
            found.append(directory)
    return found


def _recovery_files(root: Path) -> list[Path]:
    return [p for directory in _recovery_dirs(root) for p in directory.iterdir()
            if p.is_file() and p.name != "recovery-index.json"]


def _index_entry(root: Path, path: str, *, action: str | None = None) -> tuple[Path, dict, dict]:
    for directory in _recovery_dirs(root):
        index = json.loads((directory / "recovery-index.json").read_text())
        for entry in index["entries"]:
            if entry["path"] == path and (action is None or entry["action"] == action):
                return directory, index, entry
    raise AssertionError(f"no recovery index entry for {path}")


def test_only_manifest_source_is_visible_and_changes_are_script_applied(tmp_path):
    (tmp_path / "source.py").write_text("old\n")
    (tmp_path / "secret.txt").write_text("not in manifest")
    source = Workspace(tmp_path, ["source.py"])
    assert source.files == {"source.py": "old\n"}
    source.apply({"source.py": "new\n", "src/new.py": "created\n"})
    assert (tmp_path / "source.py").read_text() == "new\n"
    assert (tmp_path / "src/new.py").read_text() == "created\n"
    assert (tmp_path / "secret.txt").read_text() == "not in manifest"
    recovered = _recovery_files(tmp_path)
    assert any(p.read_text() == "old\n" for p in recovered)
    directory, index, entry = _index_entry(tmp_path, "source.py")
    assert index["source_root"] == str(tmp_path.resolve())
    assert entry["action"] == "replace"
    assert entry["mode"] == 0o644
    assert (directory / entry["basename"]).read_text() == "old\n"
    assert all(item["path"] != "src/new.py" for item in index["entries"])


def test_symlinks_hardlinks_binary_and_control_files_are_unavailable(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.write_text("private")
    (tmp_path / "link").symlink_to(outside)
    os.link(outside, tmp_path / "hardlink")
    (tmp_path / "binary").write_bytes(b"a\0b")
    (tmp_path / ".env").write_text("private")
    paths = ["link", "hardlink", "binary", ".env"]
    source = Workspace(tmp_path, paths)
    assert source.files == {}
    assert set(source.unavailable) == set(paths)
    for path in paths:
        with pytest.raises(ProtocolError):
            source.apply({path: "overwrite"})
    assert outside.read_text() == "private"


def test_parent_symlink_cannot_be_traversed_on_read_or_write(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-directory")
    outside.mkdir()
    (outside / "secret").write_text("secret")
    (tmp_path / "linked").symlink_to(outside, target_is_directory=True)
    source = Workspace(tmp_path, ["linked/secret"])
    assert source.files == {}
    with pytest.raises((ProtocolError, OSError)):
        source.apply({"linked/new": "bad"})
    assert not (outside / "new").exists()


def test_all_changes_are_checked_before_the_first_file_is_written(tmp_path):
    for path in ("a", "b"):
        (tmp_path / path).write_text("old")
    source = Workspace(tmp_path, ["a", "b"])
    (tmp_path / "b").write_text("concurrent change")
    with pytest.raises(ProtocolError, match="changed"):
        source.apply({"a": "new", "b": "new"})
    assert (tmp_path / "a").read_text() == "old"
    assert (tmp_path / "b").read_text() == "concurrent change"


def test_new_file_never_overwrites_untracked_existing_content(tmp_path):
    source = Workspace(tmp_path, [])
    (tmp_path / "new").write_text("operator file")
    with pytest.raises(ProtocolError, match="(changed|conflict)"):
        source.apply({"new": "model content"})
    assert (tmp_path / "new").read_text() == "operator file"


def test_symlink_replacement_after_snapshot_is_rejected(tmp_path):
    (tmp_path / "file").write_text("old")
    source = Workspace(tmp_path, ["file"])
    (tmp_path / "file").unlink()
    (tmp_path / "file").symlink_to(tmp_path.parent / "unavailable-outside")
    with pytest.raises((OSError, ProtocolError)):
        source.apply({"file": "new"})


def test_case_alias_cannot_create_a_second_spelling(tmp_path):
    (tmp_path / "file").write_text("old")
    source = Workspace(tmp_path, ["file"])
    with pytest.raises(ProtocolError, match="case-insensitive"):
        source.apply({"FILE": "new"})


def test_executable_mode_is_preserved_and_delete_is_explicit(tmp_path):
    target = tmp_path / "script"
    target.write_text("old")
    target.chmod(0o755)
    source = Workspace(tmp_path, ["script"])
    source.apply({"script": "new"})
    assert target.stat().st_mode & 0o777 == 0o755
    source = Workspace(tmp_path, ["script"])
    source.apply({"script": None})
    assert not target.exists()
    recovered = _recovery_files(tmp_path)
    assert any(p.read_text() == "new" for p in recovered)
    directory, index, entry = _index_entry(tmp_path, "script", action="delete")
    assert index["source_root"] == str(tmp_path.resolve())
    assert entry["action"] == "delete"
    assert entry["mode"] == 0o755
    assert (directory / entry["basename"]).read_text() == "new"


@pytest.mark.parametrize("paths", [["A.py", "a.py"], ["dir/File", "DIR/file"], ["é.py", "e\u0301.py"]])
def test_ambiguous_manifest_is_rejected(tmp_path, paths):
    with pytest.raises(ProtocolError, match="inventory"):
        Workspace(tmp_path, paths)


def test_file_directory_collision_rejected_before_any_edit(tmp_path):
    (tmp_path / "existing").write_text("original")
    workspace = Workspace(tmp_path, ["existing"])
    with pytest.raises(ProtocolError, match="collide"):
        workspace.apply({"existing": "changed", "new": "file", "new/child": "child"})
    assert (tmp_path / "existing").read_text() == "original"
    assert not (tmp_path / "new").exists()


def test_editor_change_between_validation_and_capture_preserves_original(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_text("snapshot")
    source = Workspace(tmp_path, ["file"])
    real_open_recovery = source._open_recovery

    def race_then_open():
        target.write_text("editor race")
        return real_open_recovery()

    monkeypatch.setattr(source, "_open_recovery", race_then_open)
    with pytest.raises(ProtocolError, match=r"changed.*as [0-9a-f]{32}") as raised:
        source.apply({"file": "model"})
    assert str(tmp_path.resolve()) not in str(raised.value)
    assert "retained in recovery" in str(raised.value) or "restored" in str(raised.value)
    assert target.read_text() == "editor race"
    assert target.stat().st_nlink == 1
    recovered = _recovery_files(tmp_path)
    assert any(p.read_text() == "editor race" for p in recovered)
    directory, _index, entry = _index_entry(tmp_path, "file")
    assert entry["action"] == "replace"
    recovery_path = directory / entry["basename"]
    assert recovery_path.read_text() == "editor race"
    assert recovery_path.stat().st_ino != target.stat().st_ino
    assert recovery_path.stat().st_nlink == 1
    # Restored worktree path must be usable by a fresh snapshot (_read requires nlink==1).
    refreshed = Workspace(tmp_path, ["file"])
    assert refreshed.files["file"] == "editor race"


def test_replace_recreated_between_capture_and_publication_preserves_both(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_text("snapshot")
    source = Workspace(tmp_path, ["file"])
    real_publish = source._publish_new

    def recreate_then_publish(parent_fd, name, content, mode):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent_fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"recreated")
        return real_publish(parent_fd, name, content, mode)

    monkeypatch.setattr(source, "_publish_new", recreate_then_publish)
    with pytest.raises(ProtocolError, match=r"conflict.*as [0-9a-f]{32}") as raised:
        source.apply({"file": "model"})
    assert str(tmp_path.resolve()) not in str(raised.value)
    assert target.read_text() == "recreated"
    recovered = _recovery_files(tmp_path)
    assert any(p.read_text() == "snapshot" for p in recovered)
    directory, _index, entry = _index_entry(tmp_path, "file")
    assert entry["action"] == "replace"
    assert (directory / entry["basename"]).read_text() == "snapshot"


def test_delete_recreated_between_capture_and_publication_preserves_both(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_text("snapshot")
    source = Workspace(tmp_path, ["file"])
    real_read_at = source._read_at

    def read_then_recreate(parent_fd, name):
        text, mode, ino = real_read_at(parent_fd, name)
        # Recreate destination under the worktree after capture validation.
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("recreated")
        return text, mode, ino

    monkeypatch.setattr(source, "_read_at", read_then_recreate)
    with pytest.raises(ProtocolError, match=r"conflict.*as [0-9a-f]{32}") as raised:
        source.apply({"file": None})
    assert str(tmp_path.resolve()) not in str(raised.value)
    assert target.read_text() == "recreated"
    recovered = _recovery_files(tmp_path)
    assert any(p.read_text() == "snapshot" for p in recovered)
    directory, _index, entry = _index_entry(tmp_path, "file")
    assert entry["action"] == "delete"
    assert (directory / entry["basename"]).read_text() == "snapshot"


def test_newfile_race_refuses_overwrite(tmp_path, monkeypatch):
    source = Workspace(tmp_path, [])
    real_publish = source._publish_new

    def create_then_publish(parent_fd, name, content, mode):
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644, dir_fd=parent_fd)
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"operator")
        return real_publish(parent_fd, name, content, mode)

    monkeypatch.setattr(source, "_publish_new", create_then_publish)
    with pytest.raises(ProtocolError, match="conflict"):
        source.apply({"new": "model content"})
    assert (tmp_path / "new").read_text() == "operator"


def test_late_open_descriptor_write_retains_original_in_recovery(tmp_path):
    target = tmp_path / "file"
    target.write_text("snapshot")
    source = Workspace(tmp_path, ["file"])
    with open(target, "r+", encoding="utf-8") as handle:
        handle.write("late")
        handle.flush()
        # Snapshot still matches on-disk content until apply captures the inode.
        source2 = Workspace(tmp_path, ["file"])
        assert source2.files["file"].startswith("late")
        source2.apply({"file": "published"})
        handle.seek(0)
        handle.write("after-publish")
        handle.flush()
    assert (tmp_path / "file").read_text() == "published"
    directory, _index, entry = _index_entry(tmp_path, "file")
    assert (directory / entry["basename"]).read_text() == "after-publish"


def test_empty_changes_return_without_recovery_directory(tmp_path):
    (tmp_path / "file").write_text("keep\n")
    source = Workspace(tmp_path, ["file"])
    source.apply({})
    assert (tmp_path / "file").read_text() == "keep\n"
    assert _recovery_dirs(tmp_path) == []


def test_filesystem_root_publication_is_rejected(tmp_path, monkeypatch):
    (tmp_path / "file").write_text("old\n")
    source = Workspace(tmp_path, ["file"])

    class RootPath(type(tmp_path)):
        def resolve(self, strict=False):
            return Path("/")

    monkeypatch.setattr(source, "root", RootPath(tmp_path))
    with pytest.raises(ProtocolError, match="unsupported source publication root"):
        source.apply({"file": "new\n"})
    assert (tmp_path / "file").read_text() == "old\n"
    assert _recovery_dirs(tmp_path) == []


def test_publication_io_failure_restores_captured_original(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_text("snapshot")
    source = Workspace(tmp_path, ["file"])

    def fail_publish(parent_fd, name, content, mode):
        # Fail primary publication only; restoration uses _publish_bytes separately.
        raise OSError("simulated publication I/O failure")

    monkeypatch.setattr(source, "_publish_new", fail_publish)
    with target.open("r+") as original:
        with pytest.raises(ProtocolError, match=r"conflict.*as [0-9a-f]{32}") as raised:
            source.apply({"file": "model"})
        assert str(tmp_path.resolve()) not in str(raised.value)
        assert "retained in recovery" in str(raised.value) or "restored" in str(raised.value)
        assert target.read_text() == "snapshot"
        assert target.stat().st_nlink == 1
        recovered = _recovery_files(tmp_path)
        assert any(p.read_text() == "snapshot" for p in recovered)
        directory, _index, entry = _index_entry(tmp_path, "file")
        assert entry["action"] == "replace"
        recovery_path = directory / entry["basename"]
        assert recovery_path.read_text() == "snapshot"
        assert recovery_path.stat().st_ino != target.stat().st_ino
        assert recovery_path.stat().st_nlink == 1
        # Restored worktree path must be usable by a fresh snapshot (_read requires nlink==1).
        refreshed = Workspace(tmp_path, ["file"])
        assert refreshed.files["file"] == "snapshot"
        assert recovery_path.stat().st_ino == os.fstat(original.fileno()).st_ino
        original.seek(0)
        original.write("late-after-restore")
        original.flush()
        assert recovery_path.read_text() == "late-after-restore"
        assert target.read_text() == "snapshot"


def test_late_hardlink_capture_is_not_copied_into_model_source(tmp_path, monkeypatch):
    target = tmp_path / "file"
    target.write_text("snapshot")
    outside = tmp_path.parent / (tmp_path.name + "-outside-data")
    outside.write_text("outside contents")
    source = Workspace(tmp_path, ["file"])
    real_open_recovery = source._open_recovery

    def replace_after_validation():
        target.unlink()
        os.link(outside, target)
        return real_open_recovery()

    monkeypatch.setattr(source, "_open_recovery", replace_after_validation)
    with pytest.raises(ProtocolError, match="captured source unreadable"):
        source.apply({"file": "model proposal"})
    # Restoration must not launder a forbidden hard link into a readable copy.
    assert not target.exists()
    assert outside.read_text() == "outside contents"
    assert "file" not in Workspace(tmp_path, ["file"]).files
    directory, _index, entry = _index_entry(tmp_path, "file")
    assert (directory / entry["basename"]).stat().st_ino == outside.stat().st_ino


def test_capture_fsyncs_recovery_directory_before_source_parent(tmp_path, monkeypatch):
    """Capture must fsync destination recovery first, then source parent."""
    (tmp_path / "file").write_text("snapshot")
    source = Workspace(tmp_path, ["file"])
    events: list[tuple[str, int]] = []
    real_fsync_dir = source._fsync_dir
    real_rename = os.rename
    rename_seen = {"done": False}

    def tracking_rename(*args, **kwargs):
        rename_seen["done"] = True
        return real_rename(*args, **kwargs)

    def tracking_fsync_dir(dir_fd):
        if rename_seen["done"]:
            info = os.fstat(dir_fd)
            recovery_info = os.stat(source.recovery_dir, follow_symlinks=False)
            source_info = os.stat(tmp_path, follow_symlinks=False)
            if info.st_ino == recovery_info.st_ino and info.st_dev == recovery_info.st_dev:
                events.append(("recovery", dir_fd))
            elif info.st_ino == source_info.st_ino and info.st_dev == source_info.st_dev:
                events.append(("source_parent", dir_fd))
            else:
                events.append(("other", dir_fd))
        return real_fsync_dir(dir_fd)

    monkeypatch.setattr(os, "rename", tracking_rename)
    monkeypatch.setattr(source, "_fsync_dir", tracking_fsync_dir)
    source.apply({"file": "published"})
    assert (tmp_path / "file").read_text() == "published"
    post_capture = [label for label, _fd in events]
    assert post_capture[:2] == ["recovery", "source_parent"]
    recovered = _recovery_files(tmp_path)
    assert any(p.read_text() == "snapshot" for p in recovered)


def test_publish_and_temp_unlink_fsync_target_parent(tmp_path, monkeypatch):
    """No-clobber publication and temporary unlink each fsync the target parent."""
    (tmp_path / "file").write_text("snapshot")
    source = Workspace(tmp_path, ["file"])
    events: list[str] = []
    real_link = os.link
    real_unlink = os.unlink
    real_fsync_dir = source._fsync_dir
    linked = {"done": False}
    unlinked = {"done": False}
    target_parent = os.stat(tmp_path, follow_symlinks=False)

    def tracking_link(*args, **kwargs):
        result = real_link(*args, **kwargs)
        linked["done"] = True
        events.append("link")
        return result

    def tracking_unlink(*args, **kwargs):
        # Only observe the exclusive temp unlink after link, not recovery-index cleanup.
        name = args[0] if args else kwargs.get("path")
        if linked["done"] and isinstance(name, str) and name.startswith(".agent-text-"):
            unlinked["done"] = True
            events.append("unlink_temp")
        return real_unlink(*args, **kwargs)

    def tracking_fsync_dir(dir_fd):
        info = os.fstat(dir_fd)
        is_target_parent = (
            info.st_ino == target_parent.st_ino and info.st_dev == target_parent.st_dev
        )
        if linked["done"] and not unlinked["done"]:
            assert is_target_parent, "post-link fsync must target the destination parent"
            events.append("fsync_after_link")
        elif unlinked["done"]:
            assert is_target_parent, "post-unlink fsync must target the destination parent"
            events.append("fsync_after_unlink")
        return real_fsync_dir(dir_fd)

    monkeypatch.setattr(os, "link", tracking_link)
    monkeypatch.setattr(os, "unlink", tracking_unlink)
    monkeypatch.setattr(source, "_fsync_dir", tracking_fsync_dir)
    source.apply({"file": "published"})
    assert (tmp_path / "file").read_text() == "published"
    assert events == ["link", "fsync_after_link", "unlink_temp", "fsync_after_unlink"]


def test_new_parent_and_recovery_directory_parent_are_fsynced(tmp_path, monkeypatch):
    """Creating nested parents and the private recovery directory syncs their parents."""
    source = Workspace(tmp_path, [])
    events: list[str] = []
    real_mkdir = os.mkdir
    real_fsync_dir = Workspace._fsync_dir
    created_dirs: list[int | None] = []

    def tracking_mkdir(name, mode=0o777, *, dir_fd=None):
        real_mkdir(name, mode, dir_fd=dir_fd)
        path_text = os.fspath(name)
        # Recovery mkdir uses an absolute path under the worktree parent; nested
        # source parents use relative names with dir_fd.
        if dir_fd is None and path_text.startswith(str(tmp_path.parent)):
            created_dirs.append(None)  # marker; recovery parent opened separately
            events.append("mkdir_recovery")
        elif dir_fd is not None:
            created_dirs.append(dir_fd)
            events.append(f"mkdir_nested:{path_text}")

    def tracking_fsync_dir(dir_fd):
        info = os.fstat(dir_fd)
        parent_info = os.stat(tmp_path.parent, follow_symlinks=False)
        if events and events[-1] == "mkdir_recovery":
            if info.st_ino == parent_info.st_ino and info.st_dev == parent_info.st_dev:
                events.append("fsync_parent_of:mkdir_recovery")
        elif created_dirs and created_dirs[-1] == dir_fd and events and events[-1].startswith("mkdir_nested:"):
            events.append(f"fsync_parent_of:{events[-1]}")
        return real_fsync_dir(dir_fd)

    monkeypatch.setattr(os, "mkdir", tracking_mkdir)
    monkeypatch.setattr(Workspace, "_fsync_dir", staticmethod(tracking_fsync_dir))
    source.apply({"nested/deep/new.py": "created\n"})
    assert (tmp_path / "nested/deep/new.py").read_text() == "created\n"
    assert "mkdir_recovery" in events
    assert "fsync_parent_of:mkdir_recovery" in events
    assert "mkdir_nested:nested" in events
    assert "fsync_parent_of:mkdir_nested:nested" in events
    assert "mkdir_nested:deep" in events
    assert "fsync_parent_of:mkdir_nested:deep" in events


def test_capture_directory_sync_failure_keeps_recovery_without_silent_success(tmp_path, monkeypatch):
    """Directory sync failure after capture must not report success; recovery stays."""
    target = tmp_path / "file"
    target.write_text("snapshot")
    source = Workspace(tmp_path, ["file"])
    real_fsync_dir = source._fsync_dir
    real_rename = os.rename
    renamed = {"done": False}
    fail_next_source = {"armed": False}

    def tracking_rename(*args, **kwargs):
        result = real_rename(*args, **kwargs)
        renamed["done"] = True
        fail_next_source["armed"] = True
        return result

    def failing_fsync_dir(dir_fd):
        if renamed["done"] and fail_next_source["armed"]:
            info = os.fstat(dir_fd)
            recovery_info = os.stat(source.recovery_dir, follow_symlinks=False)
            if info.st_ino == recovery_info.st_ino and info.st_dev == recovery_info.st_dev:
                return real_fsync_dir(dir_fd)
            # Fail the source-parent sync that follows the recovery-dir sync.
            fail_next_source["armed"] = False
            raise OSError(22, "simulated capture directory sync failure")
        return real_fsync_dir(dir_fd)

    monkeypatch.setattr(os, "rename", tracking_rename)
    monkeypatch.setattr(source, "_fsync_dir", failing_fsync_dir)
    with pytest.raises(ProtocolError, match=r"capture namespace sync failed.*as [0-9a-f]{32}") as raised:
        source.apply({"file": "model"})
    assert "retained in recovery" in str(raised.value) or "restored" in str(raised.value)
    # Original remains available either restored or only in recovery; never silent success.
    recovered = _recovery_files(tmp_path)
    assert any(p.read_text() == "snapshot" for p in recovered)
    directory, _index, entry = _index_entry(tmp_path, "file")
    assert (directory / entry["basename"]).read_text() == "snapshot"
    if target.exists():
        assert target.read_text() == "snapshot"
        assert target.stat().st_nlink == 1


def test_publication_directory_sync_failure_is_uncertain_and_preserves_recovery(tmp_path, monkeypatch):
    """Sync failure after publication must not claim success; recovery remains."""
    target = tmp_path / "file"
    target.write_text("snapshot")
    source = Workspace(tmp_path, ["file"])
    real_fsync_dir = source._fsync_dir
    real_link = os.link
    linked = {"done": False}
    target_parent = os.stat(tmp_path, follow_symlinks=False)

    def tracking_link(*args, **kwargs):
        result = real_link(*args, **kwargs)
        linked["done"] = True
        return result

    def failing_fsync_dir(dir_fd):
        if linked["done"]:
            info = os.fstat(dir_fd)
            if info.st_ino == target_parent.st_ino and info.st_dev == target_parent.st_dev:
                raise OSError(5, "simulated publication directory sync failure")
        return real_fsync_dir(dir_fd)

    monkeypatch.setattr(os, "link", tracking_link)
    monkeypatch.setattr(source, "_fsync_dir", failing_fsync_dir)
    with pytest.raises(ProtocolError, match=r"(conflict|sync failed).*as [0-9a-f]{32}") as raised:
        source.apply({"file": "model"})
    # Own publication may already occupy the destination; status must not invent concurrency.
    assert "existing destination" in str(raised.value) or "restored" in str(raised.value)
    assert "concurrent destination" not in str(raised.value)
    recovered = _recovery_files(tmp_path)
    assert any(p.read_text() == "snapshot" for p in recovered)
    directory, _index, entry = _index_entry(tmp_path, "file")
    assert (directory / entry["basename"]).read_text() == "snapshot"
    # Destination may already hold the published inode; outcome is uncertain, not success.
    assert target.exists()
    assert target.read_text() == "model"


def test_restore_fsyncs_target_parent(tmp_path, monkeypatch):
    """Successful restoration fsyncs the destination parent after link and temp unlink."""
    target = tmp_path / "file"
    target.write_text("snapshot")
    source = Workspace(tmp_path, ["file"])
    events: list[str] = []
    real_link = os.link
    real_unlink = os.unlink
    real_fsync_dir = source._fsync_dir
    target_parent = os.stat(tmp_path, follow_symlinks=False)
    linked = {"done": False}
    unlinked = {"done": False}

    def fail_publish(parent_fd, name, content, mode):
        raise OSError("simulated publication I/O failure")

    def tracking_link(*args, **kwargs):
        result = real_link(*args, **kwargs)
        linked["done"] = True
        events.append("link")
        return result

    def tracking_unlink(*args, **kwargs):
        name = args[0] if args else kwargs.get("path")
        if linked["done"] and isinstance(name, str) and name.startswith(".agent-text-"):
            unlinked["done"] = True
            events.append("unlink_temp")
        return real_unlink(*args, **kwargs)

    def tracking_fsync_dir(dir_fd):
        info = os.fstat(dir_fd)
        is_target_parent = (
            info.st_ino == target_parent.st_ino and info.st_dev == target_parent.st_dev
        )
        if linked["done"] and not unlinked["done"]:
            assert is_target_parent, "restore post-link fsync must target the destination parent"
            events.append("fsync_after_link")
        elif unlinked["done"]:
            assert is_target_parent, "restore post-unlink fsync must target the destination parent"
            events.append("fsync_after_unlink")
        return real_fsync_dir(dir_fd)

    monkeypatch.setattr(source, "_publish_new", fail_publish)
    monkeypatch.setattr(os, "link", tracking_link)
    monkeypatch.setattr(os, "unlink", tracking_unlink)
    monkeypatch.setattr(source, "_fsync_dir", tracking_fsync_dir)
    with pytest.raises(ProtocolError, match=r"conflict.*as [0-9a-f]{32}") as raised:
        source.apply({"file": "model"})
    assert "restored" in str(raised.value)
    assert target.read_text() == "snapshot"
    assert target.stat().st_nlink == 1
    assert events == ["link", "fsync_after_link", "unlink_temp", "fsync_after_unlink"]
    recovered = _recovery_files(tmp_path)
    assert any(p.read_text() == "snapshot" for p in recovered)
    directory, _index, entry = _index_entry(tmp_path, "file")
    assert (directory / entry["basename"]).read_text() == "snapshot"
