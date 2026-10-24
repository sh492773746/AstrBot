"""Filesystem boundaries reject traversal, roots and symlink escapes."""

import pytest

from astrbot.core.utils.astrbot_path import resolve_path_within


@pytest.mark.parametrize(
    "value",
    [
        "",
        ".",
        "..",
        "../sibling/secret",
        "/etc/passwd",
        "C:/Windows/system.ini",
        "C:secret",
        r"..\secret",
        "file\x00.txt",
    ],
)
def test_confined_paths_reject_unsafe_requests(tmp_path, value):
    with pytest.raises(ValueError):
        resolve_path_within(tmp_path, value)


@pytest.mark.parametrize(
    "value", ["", ".", "..", "branch/..", "../escape", "/tmp/escape", "C:escape", r"a\b"]
)
def test_entry_cleanup_rejects_unsafe_requests(tmp_path, value):
    with pytest.raises(ValueError):
        resolve_path_within(tmp_path, value, preserve_leaf=True)


def test_confined_paths_allow_nested_nonexistent_files(tmp_path):
    assert (
        resolve_path_within(tmp_path, "files/key/image.png")
        == (tmp_path / "files/key/image.png").resolve()
    )
    assert resolve_path_within(tmp_path, ".", allow_root=True) == tmp_path.resolve()


def test_confined_paths_reject_sibling_prefix_and_symlink_escape(require_symlink):
    root = require_symlink / "plugin"
    root.mkdir()
    outside = require_symlink / "plugin-other"
    outside.mkdir()
    (outside / "secret").write_text("unchanged")
    (root / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        resolve_path_within(root, "escape/secret")
    with pytest.raises(ValueError):
        resolve_path_within(root, "../plugin-other/secret")
    assert (
        resolve_path_within(root, "escape", preserve_leaf=True) == root / "escape"
    )
    with pytest.raises(ValueError):
        resolve_path_within(root, "escape/secret", preserve_leaf=True)
    assert (outside / "secret").read_text() == "unchanged"


def test_config_storage_rejects_absolute_names_and_symlinks(tmp_path, monkeypatch):
    from astrbot.dashboard.services import config_service

    monkeypatch.setattr(
        config_service, "get_astrbot_plugin_data_path", lambda: str(tmp_path)
    )
    service = config_service.ConfigFileService
    assert service._plugin_root_path("plugin") == tmp_path / "plugin"
    for name in ("..", "../other", "/tmp/other", "a/b", "C:other"):
        with pytest.raises(ValueError):
            service._plugin_root_path(name)
    assert service._safe_plugin_path(tmp_path, "../other") is None
