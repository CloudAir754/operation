"""sudoers 只读解析测试。"""

from pathlib import Path

from lab_ops_panel.infrastructure.sudoers import scan_sudoers_rules


def test_sudoers_reports_direct_and_alias_sources(tmp_path: Path) -> None:
    """区分主文件和 drop-in，并解析简单 User_Alias。"""

    sudoers = tmp_path / "sudoers"
    drop_ins = tmp_path / "sudoers.d"
    drop_ins.mkdir()
    sudoers.write_text("root ALL=(ALL:ALL) ALL\nalice ALL=(ALL) ALL\n", encoding="utf-8")
    (drop_ins / "lab-team").write_text(
        "User_Alias LAB = bob, carol\nLAB ALL=(ALL) /usr/bin/id\n", encoding="utf-8"
    )

    scan = scan_sudoers_rules({"root", "alice", "bob", "carol"}, sudoers, drop_ins)

    assert scan["sources"]["alice"] == ["/etc/sudoers"]
    assert scan["sources"]["bob"] == ["/etc/sudoers.d/lab-team (LAB)"]
    assert scan["sources"]["carol"] == ["/etc/sudoers.d/lab-team (LAB)"]
    assert scan["complete"] is True


def test_sudoers_marks_complex_principal_incomplete(tmp_path: Path) -> None:
    """无法安全理解的主体必须报告，不能猜测授权。"""

    sudoers = tmp_path / "sudoers"
    sudoers.write_text("+directory_admins ALL=(ALL) ALL\n", encoding="utf-8")

    scan = scan_sudoers_rules({"alice"}, sudoers, tmp_path / "missing")

    assert scan["complete"] is False
    assert scan["warnings"]
