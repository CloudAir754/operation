"""直接执行远端探针源码：用假 /proc 与临时目录验证采集语义。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from lab_ops_panel.remote.probe import PROBE_SOURCE, PROBE_VERSION, build_probe_command


@pytest.fixture
def probe_ns() -> dict:
    namespace: dict = {"__name__": "labops_probe_test"}
    exec(compile(PROBE_SOURCE, "<probe>", "exec"), namespace)
    return namespace


@pytest.fixture
def fake_proc(probe_ns, monkeypatch) -> dict[str, str]:
    files: dict[str, str] = {}

    def read_text(path: str, limit: int = 262144) -> str:
        key = str(path).replace("\\", "/")
        return files.get(key, "")

    monkeypatch.setitem(probe_ns, "read_text", read_text)
    return files


def test_collect_metrics_reads_proc_files(probe_ns, fake_proc) -> None:
    fake_proc["/proc/stat"] = "cpu  100 0 50 800 20 0 0 0 0 0\ncpu0 1 2 3\nbtime 1700000000\n"
    fake_proc["/proc/meminfo"] = (
        "MemTotal:       1000 kB\n"
        "MemAvailable:    400 kB\n"
        "MemFree:         100 kB\n"
        "Buffers:         100 kB\n"
        "Cached:          200 kB\n"
        "SwapTotal:       500 kB\n"
        "SwapFree:        300 kB\n"
    )
    fake_proc["/proc/vmstat"] = "pswpin 10\npswpout 5\npgpgin 4\n"
    fake_proc["/proc/diskstats"] = "   8       0 sda 2 0 100 50 4 0 200 100 100 300 0 0 0 0\n"
    fake_proc["/proc/net/dev"] = (
        "Inter-|   Receive                                                |  Transmit\n"
        " face |bytes    packets errs drop fifo frame compressed multicast|bytes    packets errs drop fifo colls carrier compressed\n"
        "  eth0: 1000 10 0 0 0 0 0 0 2000 20 2 1 0 0 0 0\n"
    )
    fake_proc["/proc/net/route"] = (
        "Iface\tDestination\tGateway\tFlags\tRefCnt\tUse\tMetric\tMask\tMTU\tWindow\tIRTT\n"
        "eth0\t00000000\t0102A8C0\t0003\t0\t0\t0\t00000000\t0\t0\t0\n"
    )
    fake_proc["/proc/self/mounts"] = "/dev/sda1 / ext4 rw,relatime 0 0\nproc /proc proc rw 0 0\n"

    section = probe_ns["collect_metrics"]()

    assert section["status"] == "ok"
    data = section["data"]
    assert data["cpu"]["user"] == 100
    assert data["cpu"]["idle"] == 800
    assert data["memory"]["MemTotal"] == 1000 * 1024
    assert data["vmstat"]["pswpin"] == 10
    assert data["disk"]["sda"]["readsCompleted"] == 2
    assert data["network"]["eth0"]["rxBytes"] == 1000
    assert data["defaultInterface"] == "eth0"
    assert data["mounts"][0]["mount"] == "/"
    assert data["mounts"][0]["deviceName"] == "sda1"
    assert data["gpuStatus"] in {"unavailable", "ok", "error"}


def test_collect_metrics_reports_missing_proc(probe_ns, fake_proc) -> None:
    section = probe_ns["collect_metrics"]()

    assert section["status"] == "error"
    assert section["error"]


def test_collect_users_filters_accounts_and_reports_sudo(probe_ns, fake_proc, tmp_path) -> None:
    alice_home = tmp_path / "home" / "alice"
    bob_home = tmp_path / "home" / "bob"
    alice_home.mkdir(parents=True)
    bob_home.mkdir(parents=True)
    (alice_home / "a.txt").write_text("data", encoding="utf-8")
    fake_proc["/etc/passwd"] = (
        f"root:x:0:0:root:/root:/bin/bash\n"
        f"daemon:x:1:1:daemon:/usr/sbin:/usr/sbin/nologin\n"
        f"alice:x:1001:1001::{alice_home}:/bin/bash\n"
        f"bob:x:1002:1002::{bob_home}:/bin/bash\n"
        f"carol:x:1003:1003::/home/carol:/usr/sbin/nologin\n"
    )
    fake_proc["/etc/group"] = (
        "sudo:x:27:alice,bob\ndocker:x:999:alice\nalice:x:1001:\nbob:x:1002:\ncarol:x:1003:\n"
    )
    fake_proc["/etc/sudoers"] = "%sudo ALL=(ALL:ALL) ALL\n"
    fake_proc["/etc/sudoers.d/"] = ""

    section = probe_ns["collect_users"](False)

    assert section["status"] == "ok"
    users = {user["login"]: user for user in section["data"]["users"]}
    assert set(users) == {"alice", "bob"}
    assert users["alice"]["sudoGroupMember"] is True
    assert users["alice"]["sudoPolicyPresent"] is True
    assert users["alice"]["sudoEffective"] == "unknown"
    assert users["alice"]["primaryGroup"] == "alice"
    assert "sudo" in users["alice"]["supplementaryGroups"]
    assert section["data"]["notes"]

    with_system = probe_ns["collect_users"](True)
    logins = {user["login"] for user in with_system["data"]["users"]}
    assert "root" in logins
    assert "daemon" not in logins


def test_home_storage_counts_files(probe_ns, tmp_path) -> None:
    home = tmp_path / "home"
    (home / "sub").mkdir(parents=True)
    (home / "a.txt").write_text("12345", encoding="utf-8")
    (home / "sub" / "b.txt").write_text("678", encoding="utf-8")

    stats = probe_ns["home_storage"](str(home))

    assert stats["fileCount"] == 2
    assert stats["directoryCount"] == 1
    assert stats["apparentBytes"] == 8
    assert stats["incomplete"] is False
    assert stats["truncated"] is False


def test_collect_users_without_readable_sudoers_is_unknown(probe_ns, fake_proc, tmp_path) -> None:
    home = tmp_path / "alice"
    home.mkdir()
    fake_proc["/etc/passwd"] = f"alice:x:1001:1001:::{home}:/bin/bash\n"
    fake_proc["/etc/group"] = "alice:x:1001:\n"

    section = probe_ns["collect_users"](False)

    user = section["data"]["users"][0]
    assert user["sudoPolicyPresent"] is None
    assert user["sudoEffective"] == "unknown"


def test_collect_mounts_skips_pseudo_filesystems(probe_ns, fake_proc) -> None:
    fake_proc["/proc/self/mounts"] = (
        "/dev/sda1 / ext4 rw,relatime 0 0\n"
        "proc /proc proc rw 0 0\n"
        "tmpfs /run tmpfs rw 0 0\n"
        "/dev/sdb1 /data\\040disk xfs rw,noatime 0 0\n"
    )

    section = probe_ns["collect_mounts"]()

    assert section["status"] == "ok"
    mounts = section["data"]["mounts"]
    paths = sorted(item["mount"] for item in mounts)
    assert paths == ["/", "/data disk"]
    root = next(item for item in mounts if item["mount"] == "/")
    assert root["filesystem"] == "ext4"
    assert root["readOnly"] is False


def test_collect_storage_walks_tree(probe_ns, fake_proc, tmp_path) -> None:
    root = tmp_path / "data"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (root / "a.txt").write_text("hello", encoding="utf-8")
    (sub / "b.txt").write_text("bb", encoding="utf-8")
    hardlink = root / "hard.txt"
    try:
        os.link(root / "a.txt", hardlink)
    except OSError:
        hardlink = None
    fake_proc["/proc/self/mounts"] = f"/dev/fake {root} ext4 rw 0 0\n"

    section = probe_ns["collect_storage"](str(root), 3, False, True, False)

    assert section["status"] == "ok"
    data = section["data"]
    paths = {node["path"] for node in data["nodes"]}
    assert str(root) in paths
    assert str(sub / "b.txt") in paths
    kinds = {node["path"]: node["kind"] for node in data["nodes"]}
    assert kinds[str(root)] == "dir"
    assert data["incomplete"] is False
    assert data["summaryByUid"]
    if hardlink is not None:
        links = [node for node in data["nodes"] if node["hardlinkDuplicateOf"]]
        assert len(links) == 1


def test_collect_storage_depth_limit_marks_unscanned(probe_ns, fake_proc, tmp_path) -> None:
    root = tmp_path / "data"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "f.txt").write_text("x", encoding="utf-8")
    fake_proc["/proc/self/mounts"] = f"/dev/fake {root} ext4 rw 0 0\n"

    section = probe_ns["collect_storage"](str(root), 0, False, True, False)

    data = section["data"]
    assert data["truncated"] is False
    root_node = next(node for node in data["nodes"] if node["path"] == str(root))
    assert root_node["unscanned"] is True
    assert data["incomplete"] is True


def test_collect_storage_rejects_unknown_mount(probe_ns, fake_proc, tmp_path) -> None:
    fake_proc["/proc/self/mounts"] = "/dev/fake /data ext4 rw 0 0\n"

    section = probe_ns["collect_storage"](str(tmp_path), 2, False, True, False)

    assert section["status"] == "permission_denied"
    assert "挂载点" in section["error"]


def test_collect_storage_node_cap_marks_truncated(probe_ns, fake_proc, tmp_path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    for index in range(10):
        (root / f"f-{index}.txt").write_text("x", encoding="utf-8")
    fake_proc["/proc/self/mounts"] = f"/dev/fake {root} ext4 rw 0 0\n"

    section = probe_ns["collect_storage"](str(root), 3, False, True, False, 3)

    assert section["status"] == "ok"
    data = section["data"]
    assert len(data["nodes"]) <= 3
    assert data["truncated"] is True


def test_probe_command_quotes_arguments() -> None:
    command = build_probe_command("storage", ["/data/my logs", "3"])

    assert command.endswith("- storage '/data/my logs' 3")


def test_probe_source_runs_from_stdin(tmp_path: Path) -> None:
    """回归：探针必须能通过 ``python3 -`` 从 stdin 执行（避免命令行超长）。"""

    result = subprocess.run(
        [sys.executable, "-", "snapshot"],
        input=PROBE_SOURCE.encode("utf-8"),
        capture_output=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    payload = json.loads(result.stdout.decode("utf-8"))
    assert payload["probeVersion"] == PROBE_VERSION
    assert set(payload["sections"]) == {
        "system",
        "cpu",
        "memory",
        "disks",
        "network",
        "gpu",
        "services",
    }


@pytest.mark.parametrize("argument", ["bad\nmount", "bad\x00value"])
def test_probe_command_rejects_control_characters(argument: str) -> None:
    with pytest.raises(ValueError, match="探针参数不合法"):
        build_probe_command("storage", [argument])


def test_probe_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="未知探针模式"):
        build_probe_command("rm-rf")
