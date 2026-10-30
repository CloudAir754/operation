"""空间扫描视图测试：路径校验、硬链接、稀疏文件、过滤与聚合。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from lab_ops_panel.remote.storage_scan import (
    ScanFilters,
    build_scan_view,
    chunk_nodes,
    mount_matches,
    normalize_mount,
    normalize_scan_path,
    parse_nodes,
    scan_children,
)

MTIME = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)


def raw_node(
    path: str,
    *,
    kind: str = "file",
    allocated: int | None = 100,
    apparent: int | None = 100,
    uid: int | None = 1000,
    inode: int | None = None,
    hardlink_of: str | None = None,
    deleted: bool = False,
    unscanned: bool = False,
    incomplete: bool = False,
    error: str | None = None,
    mtime: float | None = None,
) -> dict:
    return {
        "path": path,
        "name": path.rsplit("/", 1)[-1] or path,
        "kind": kind,
        "allocatedBytes": allocated,
        "apparentBytes": apparent,
        "uid": uid,
        "gid": uid,
        "device": 1,
        "inode": inode,
        "modifiedAtEpoch": MTIME.timestamp() if mtime is None else mtime,
        "hardlinkDuplicateOf": hardlink_of,
        "deletedOpen": deleted,
        "unscanned": unscanned,
        "incomplete": incomplete,
        "error": error,
    }


def test_normalize_mount_and_path_rules() -> None:
    assert normalize_mount("/data/") == "/data"
    assert normalize_mount("/") == "/"
    with pytest.raises(ValueError, match="绝对路径"):
        normalize_mount("data")
    with pytest.raises(ValueError, match="不允许包含"):
        normalize_mount("/data/../etc")
    assert normalize_scan_path("/data", "/data/logs") == "/data/logs"
    assert normalize_scan_path("/data", "/data") == "/data"
    with pytest.raises(ValueError, match="不在扫描挂载点内"):
        normalize_scan_path("/data", "/etc")
    with pytest.raises(ValueError, match="不允许包含"):
        normalize_scan_path("/data", "/data/../../etc")
    with pytest.raises(ValueError, match="绝对路径"):
        normalize_scan_path("/data", "relative")


def test_mount_allowlist_requires_exact_match() -> None:
    assert mount_matches("/data", ["/", "/data"]) == "/data"
    assert mount_matches("/data/", ["/data"]) == "/data"
    assert mount_matches("/etc", ["/", "/data"]) is None
    assert mount_matches("/data/../etc", ["/", "/data"]) is None


def test_parse_nodes_rejects_foreign_and_duplicate_paths() -> None:
    nodes = parse_nodes(
        [
            raw_node("/data/a.txt"),
            raw_node("/etc/passwd"),
            raw_node("/data/../escape"),
            raw_node("/data/a.txt", allocated=999),
            raw_node("/data/b", kind="weird"),
        ],
        mount="/data",
    )

    assert [node.path for node in nodes] == ["/data/a.txt", "/data/b"]
    assert nodes[0].allocated_bytes == 100
    assert nodes[1].kind == "other"


def test_parse_nodes_clamps_invalid_numbers() -> None:
    broken = raw_node("/data/x", allocated=-5, apparent=-1, uid=-3, inode=-1)
    broken["modifiedAtEpoch"] = -100

    node = parse_nodes([broken], mount="/data")[0]

    assert node.allocated_bytes is None
    assert node.apparent_bytes is None
    assert node.uid is None
    assert node.inode is None
    assert node.modified_at is None


def test_tree_aggregates_directory_sizes() -> None:
    nodes = parse_nodes(
        [
            raw_node("/data", kind="dir", allocated=0, apparent=0, inode=1),
            raw_node("/data/a.txt", allocated=100, apparent=120),
            raw_node("/data/b", kind="dir", allocated=0, apparent=0, inode=2),
            raw_node("/data/b/c.txt", allocated=50, apparent=60),
        ],
        mount="/data",
    )

    view = build_scan_view(nodes, mount="/data")

    by_path = {node.path: node for node in view.nodes}
    assert by_path["/data"].allocated_bytes == 150
    assert by_path["/data"].apparent_bytes == 180
    assert by_path["/data"].file_count == 2
    assert by_path["/data"].directory_count == 2
    assert by_path["/data/b"].allocated_bytes == 50
    assert by_path["/data/b/c.txt"].allocated_bytes == 50
    assert [node.path for node in view.nodes] == [
        "/data",
        "/data/b",
        "/data/b/c.txt",
        "/data/a.txt",
    ]


def test_hardlink_duplicates_are_excluded_from_totals() -> None:
    nodes = parse_nodes(
        [
            raw_node("/data", kind="dir", allocated=0),
            raw_node("/data/a.bin", allocated=1000, apparent=1000, inode=7),
            raw_node(
                "/data/link.bin",
                allocated=1000,
                apparent=1000,
                inode=7,
                hardlink_of="/data/a.bin",
            ),
        ],
        mount="/data",
    )

    view = build_scan_view(nodes, mount="/data")

    by_path = {node.path: node for node in view.nodes}
    assert by_path["/data"].allocated_bytes == 1000
    assert by_path["/data/link.bin"].hardlink_duplicate_of == "/data/a.bin"
    assert [node.path for node in view.largest_files] == ["/data/a.bin"]
    assert view.summary[0].allocated_bytes == 1000
    assert view.summary[0].file_count == 1


def test_sparse_file_keeps_allocated_and_apparent_separate() -> None:
    nodes = parse_nodes(
        [
            raw_node("/data", kind="dir", allocated=0),
            raw_node("/data/sparse.img", allocated=4096, apparent=10 * 1024**3),
        ],
        mount="/data",
    )

    view = build_scan_view(nodes, mount="/data")

    node = next(item for item in view.nodes if item.path == "/data/sparse.img")
    assert node.allocated_bytes == 4096
    assert node.apparent_bytes == 10 * 1024**3


def test_permission_error_propagates_incomplete() -> None:
    nodes = parse_nodes(
        [
            raw_node("/data", kind="dir", allocated=0),
            raw_node("/data/secret", kind="dir", allocated=0, incomplete=True, error="权限不足"),
        ],
        mount="/data",
    )

    view = build_scan_view(nodes, mount="/data")

    root = next(node for node in view.nodes if node.path == "/data")
    secret = next(node for node in view.nodes if node.path == "/data/secret")
    assert secret.incomplete is True
    assert secret.error == "权限不足"
    assert root.incomplete is True
    assert view.incomplete is True


def test_unscanned_directory_marks_parent_incomplete() -> None:
    nodes = parse_nodes(
        [
            raw_node("/data", kind="dir", allocated=0),
            raw_node("/data/deep", kind="dir", allocated=0, unscanned=True),
        ],
        mount="/data",
    )

    view = build_scan_view(nodes, mount="/data")

    assert view.incomplete is True
    assert next(node for node in view.nodes if node.path == "/data/deep").unscanned is True


def test_filters_keep_ancestors_and_recompute_sizes() -> None:
    nodes = parse_nodes(
        [
            raw_node("/data", kind="dir", allocated=0),
            raw_node("/data/logs", kind="dir", allocated=0),
            raw_node("/data/logs/app.log", allocated=300, apparent=300, uid=1001),
            raw_node("/data/logs/tiny.log", allocated=10, apparent=10, uid=1001),
            raw_node("/data/other.bin", allocated=500, apparent=500, uid=1002),
        ],
        mount="/data",
    )

    view = build_scan_view(
        nodes,
        mount="/data",
        filters=ScanFilters(min_file_size=100, uid=1001, extension=".log"),
    )

    paths = {node.path for node in view.nodes}
    assert paths == {"/data", "/data/logs", "/data/logs/app.log"}
    root = next(node for node in view.nodes if node.path == "/data")
    assert root.allocated_bytes == 300
    assert root.file_count == 1
    assert [item.uid for item in view.summary] == [1001]


def test_modified_after_filter_uses_timestamp() -> None:
    old = (datetime.now(UTC) - timedelta(days=30)).timestamp()
    nodes = parse_nodes(
        [
            raw_node("/data", kind="dir", allocated=0),
            raw_node("/data/old.log", allocated=100, mtime=old),
            raw_node("/data/new.log", allocated=100, mtime=datetime.now(UTC).timestamp()),
        ],
        mount="/data",
    )

    view = build_scan_view(
        nodes,
        mount="/data",
        filters=ScanFilters(modified_after=datetime.now(UTC) - timedelta(days=1)),
    )

    assert [node.path for node in view.nodes if node.kind == "file"] == ["/data/new.log"]


def test_largest_files_are_limited_and_sorted() -> None:
    nodes = [raw_node("/data", kind="dir", allocated=0)]
    for index in range(150):
        nodes.append(raw_node(f"/data/file-{index:03d}", allocated=index + 1))
    parsed = parse_nodes(nodes, mount="/data")

    view = build_scan_view(parsed, mount="/data")

    assert len(view.largest_files) == 100
    assert view.largest_files[0].allocated_bytes == 150
    assert view.largest_files[-1].allocated_bytes == 51


def test_deleted_open_files_are_reported_separately() -> None:
    nodes = parse_nodes(
        [
            raw_node("/data", kind="dir", allocated=0),
            raw_node("/data/open.bin", allocated=2048, deleted=True),
            raw_node("/data/normal.bin", allocated=10),
        ],
        mount="/data",
    )

    view = build_scan_view(nodes, mount="/data")

    assert view.deleted_open_bytes == 2048
    assert view.deleted_open_count == 1
    node = next(item for item in view.nodes if item.path == "/data/open.bin")
    assert node.deleted_open is True


def test_probe_truncation_propagates() -> None:
    nodes = parse_nodes([raw_node("/data", kind="dir", allocated=0)], mount="/data")

    view = build_scan_view(nodes, mount="/data", probe_truncated=True, probe_incomplete=True)

    assert view.truncated is True
    assert view.incomplete is True


def test_view_node_limit_sets_truncated() -> None:
    nodes = [raw_node("/data", kind="dir", allocated=0)]
    for index in range(40):
        nodes.append(raw_node(f"/data/f-{index:02d}", allocated=index))
    parsed = parse_nodes(nodes, mount="/data")

    view = build_scan_view(parsed, mount="/data", max_nodes=10)

    assert len(view.nodes) == 10
    assert view.truncated is True


def test_missing_root_is_synthesized() -> None:
    nodes = parse_nodes([raw_node("/data/a.txt")], mount="/data")

    view = build_scan_view(nodes, mount="/data")

    assert view.nodes[0].path == "/data"
    assert view.nodes[0].kind == "dir"
    assert view.nodes[0].parent_path is None


def test_chunking_and_children_helpers() -> None:
    nodes = parse_nodes(
        [
            raw_node("/data", kind="dir", allocated=0),
            raw_node("/data/a", allocated=10),
            raw_node("/data/b", allocated=20),
        ],
        mount="/data",
    )
    view = build_scan_view(nodes, mount="/data")

    chunks = chunk_nodes(view.nodes, size=2)
    assert [len(chunk) for chunk in chunks] == [2, 1]
    children = scan_children(view.nodes, "/data")
    assert {node.path for node in children} == {"/data/a", "/data/b"}
    assert scan_children(view.nodes, "/data/a") == []
    with pytest.raises(ValueError, match="分块大小"):
        chunk_nodes(view.nodes, size=0)
