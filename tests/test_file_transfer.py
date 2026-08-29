"""部分文件迁移的路径边界、复制校验与独立清理测试。"""

import os
from contextlib import suppress
from pathlib import Path

import pytest

import lab_ops_panel.infrastructure.file_transfer as file_transfer_module
from lab_ops_panel.infrastructure.file_transfer import (
    cleanup_file_transfer_source,
    execute_file_transfer,
    file_transfer_preflight,
)


@pytest.fixture(autouse=True)
def trust_test_target_owner(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """临时目录由测试用户所有，生产默认仍只信任 root 管理的目标目录。"""

    monkeypatch.setattr(file_transfer_module, "TRUSTED_TARGET_UID", os.getuid())
    monkeypatch.setattr(file_transfer_module, "TRUSTED_ANCESTOR_UID", os.getuid())


def test_file_transfer_copies_metadata_and_retains_source(tmp_path: Path) -> None:
    """复制阶段应保留硬链接、稀疏文件和符号链接，并完整保留源目录。"""

    source = tmp_path / "source"
    source.mkdir()
    original = source / "data.bin"
    with original.open("wb") as stream:
        stream.seek(1024 * 1024)
        stream.write(b"end")
    os.chmod(original, 0o640)
    with suppress(OSError):
        os.setxattr(original, "user.labopspanel-test", b"metadata")
    os.link(original, source / "hard-link.bin")
    (source / "relative-link").symlink_to("data.bin")
    target = tmp_path / "target"
    target.mkdir(mode=0o700)

    preview = file_transfer_preflight(str(source), str(target))
    result = execute_file_transfer(
        str(source),
        str(target),
        "a" * 32,
        preview["source_summary"]["device"],
        preview["source_summary"]["inode"],
    )

    destination = target / "source"
    assert result["source_retained"] is True
    assert source.is_dir() and destination.is_dir()
    assert (destination / "relative-link").is_symlink()
    assert os.readlink(destination / "relative-link") == "data.bin"
    assert (destination / "data.bin").stat().st_ino == (destination / "hard-link.bin").stat().st_ino
    assert (destination / "data.bin").stat().st_mode & 0o777 == 0o640
    assert (destination / "data.bin").read_bytes().endswith(b"end")
    if "user.labopspanel-test" in os.listxattr(original):
        assert os.getxattr(destination / "data.bin", "user.labopspanel-test") == b"metadata"


def test_file_transfer_cleanup_is_independent_and_verified(tmp_path: Path) -> None:
    """只有复制发布成功后调用独立清理，源路径才会消失。"""

    source = tmp_path / "source"
    source.mkdir()
    (source / "value.txt").write_text("构造数据", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    preview = file_transfer_preflight(str(source), str(target))
    summary = preview["source_summary"]
    copied = execute_file_transfer(
        str(source), str(target), "b" * 32, summary["device"], summary["inode"]
    )

    result = cleanup_file_transfer_source(
        str(source),
        copied["destination"],
        "c" * 32,
        summary["device"],
        summary["inode"],
    )

    assert result["source_removed"] is True
    assert not source.exists()
    assert (target / "source" / "value.txt").read_text(encoding="utf-8") == "构造数据"


def test_file_transfer_cleanup_rejects_changed_destination(tmp_path: Path) -> None:
    """目标发布后发生变化时不得删除仍完好的源数据。"""

    source = tmp_path / "source"
    source.mkdir()
    (source / "value.txt").write_text("原始", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    preview = file_transfer_preflight(str(source), str(target))
    summary = preview["source_summary"]
    copied = execute_file_transfer(
        str(source), str(target), "d" * 32, summary["device"], summary["inode"]
    )
    Path(copied["destination"], "value.txt").write_text("已改变", encoding="utf-8")

    with pytest.raises(ValueError, match="校验不一致"):
        cleanup_file_transfer_source(
            str(source), copied["destination"], "e" * 32, summary["device"], summary["inode"]
        )
    assert source.exists()


@pytest.mark.parametrize("source", ["/", "/etc", "/etc/passwd", "relative/path"])
def test_file_transfer_rejects_dangerous_sources(tmp_path: Path, source: str) -> None:
    """根目录、系统关键路径和相对路径不能进入迁移流程。"""

    with pytest.raises(ValueError):
        file_transfer_preflight(source, str(tmp_path))


def test_file_transfer_rejects_symlink_ancestor_and_target_collision(tmp_path: Path) -> None:
    """路径祖先逃逸或同名目标冲突时必须预检失败。"""

    actual = tmp_path / "actual"
    actual.mkdir()
    source = actual / "source"
    source.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="符号链接祖先"):
        file_transfer_preflight(str(alias / "source"), str(target))

    (target / "source").mkdir()
    with pytest.raises(ValueError, match="已存在"):
        file_transfer_preflight(str(source), str(target))


def test_file_transfer_rejects_special_file(tmp_path: Path) -> None:
    """设备、FIFO 等特殊条目不能交给 rsync 迁移。"""

    source = tmp_path / "source"
    source.mkdir()
    os.mkfifo(source / "pipe")
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    with pytest.raises(ValueError, match="特殊文件"):
        file_transfer_preflight(str(source), str(target))


def test_file_transfer_rejects_untrusted_target_directory(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """目标目录不是 root 所有或可被其他用户改写时不能运行 root rsync。"""

    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    monkeypatch.setattr(file_transfer_module, "TRUSTED_TARGET_UID", os.getuid() + 1)
    with pytest.raises(ValueError, match="root 所有"):
        file_transfer_preflight(str(source), str(target))
