"""三种批量账号输入的统一预览测试。"""

import io

import pytest
from openpyxl import Workbook

from lab_ops_panel.application.user_imports import preview_csv, preview_text, preview_xlsx


def test_text_preview_supports_header_and_whitespace() -> None:
    """文本输入忽略空行并接受连续空格或制表符。"""

    preview = preview_text(
        "用户名 中文姓名\n\nalice   张三\nbob\t李四\n", existing_usernames={"bob"}
    ).to_dict()

    assert preview["header_detected"] is True
    assert preview["counts"] == {"will_create": 1, "exists": 1, "invalid": 0}
    assert [row["source_row"] for row in preview["rows"]] == [3, 4]


def test_text_preview_reports_extra_fields_and_duplicates() -> None:
    """多余字段和所有重复用户名均在逐行结果中拒绝。"""

    preview = preview_text("alice 张 三\nbob 李四\nbob 王五", set()).to_dict()

    assert preview["counts"]["invalid"] == 3
    assert any("恰好包含两列" in error for error in preview["rows"][0]["errors"])
    assert all(any("用户名重复" in error for error in row["errors"]) for row in preview["rows"][1:])


def test_csv_preview_requires_utf8_and_exact_columns() -> None:
    """CSV 支持 BOM，但拒绝非 UTF-8 和多余列。"""

    preview = preview_csv("username,中文姓名\nalice,张三,extra\n".encode(), set()).to_dict()
    assert preview["header_detected"] is True
    assert preview["rows"][0]["status"] == "invalid"
    with pytest.raises(ValueError, match="UTF-8"):
        preview_csv(b"\xff\xfe", set())


def test_xlsx_preview_reads_first_worksheet() -> None:
    """XLSX 默认读取第一个工作表并识别前两列。"""

    workbook = Workbook()
    worksheet = workbook.active
    worksheet.append(["username", "中文姓名"])
    worksheet.append(["alice", "张三"])
    second = workbook.create_sheet("ignored")
    second.append(["bob", "李四"])
    content = io.BytesIO()
    workbook.save(content)

    preview = preview_xlsx(content.getvalue(), set()).to_dict()

    assert preview["header_detected"] is True
    assert [row["username"] for row in preview["rows"]] == ["alice"]


def test_preview_rejects_invalid_username_and_display_name_space() -> None:
    """用户名与已确认的无空格中文姓名规则均需校验。"""

    preview = preview_csv('Alice,张三\nalice,"张 三"'.encode(), set()).to_dict()
    assert preview["counts"]["invalid"] == 2
    assert "小写字母" in preview["rows"][0]["errors"][0]
    assert "空白字符" in preview["rows"][1]["errors"][0]
