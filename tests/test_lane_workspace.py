import os

import pytest

from agent_cli.lane_protocol import ProtocolError
from agent_cli.lane_workspace import Workspace


def test_only_manifest_source_is_visible_and_changes_are_script_applied(tmp_path):
    (tmp_path / "source.py").write_text("old\n")
    (tmp_path / "secret.txt").write_text("not in manifest")
    source = Workspace(tmp_path, ["source.py"])
    assert source.files == {"source.py": "old\n"}
    source.apply({"source.py": "new\n", "src/new.py": "created\n"})
    assert (tmp_path / "source.py").read_text() == "new\n"
    assert (tmp_path / "src/new.py").read_text() == "created\n"
    assert (tmp_path / "secret.txt").read_text() == "not in manifest"


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
    with pytest.raises(ProtocolError, match="changed"):
        source.apply({"new": "model content"})
    assert (tmp_path / "new").read_text() == "operator file"


def test_symlink_replacement_after_snapshot_is_rejected(tmp_path):
    (tmp_path / "file").write_text("old")
    source = Workspace(tmp_path, ["file"])
    (tmp_path / "file").unlink()
    (tmp_path / "file").symlink_to(tmp_path.parent / "unavailable-outside")
    with pytest.raises(OSError):
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
