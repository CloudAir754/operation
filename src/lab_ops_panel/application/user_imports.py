"""CSV、XLSX 和文本账号导入适配器。"""

from __future__ import annotations

import csv
import io
import zipfile
from dataclasses import dataclass
from typing import Any

from openpyxl import load_workbook

from lab_ops_panel.domain.user_imports import RawImportRow, validate_import_rows

MAX_IMPORT_ROWS = 5_000
MAX_XLSX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
USERNAME_HEADERS = frozenset({"username", "user", "用户名", "姓名全拼"})
DISPLAY_NAME_HEADERS = frozenset({"display_name", "name", "中文姓名", "姓名"})


@dataclass(frozen=True, slots=True)
class ImportPreview:
    """统一三种输入格式的预览结果。"""

    header_detected: bool
    rows: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        """返回含汇总计数的普通字典。"""

        counts = {"will_create": 0, "exists": 0, "invalid": 0}
        for row in self.rows:
            counts[row["status"]] += 1
        return {"header_detected": self.header_detected, "counts": counts, "rows": self.rows}


def _clean_cell(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _rows_from_cells(rows: list[tuple[int, list[Any]]]) -> tuple[bool, list[RawImportRow]]:
    """识别表头并将任意表格行转为领域层原始行。"""

    if len(rows) > MAX_IMPORT_ROWS + 1:
        raise ValueError(f"单次导入不能超过 {MAX_IMPORT_ROWS} 行")
    header_detected = False
    if rows:
        first = [_clean_cell(value).lower() for value in rows[0][1][:2]]
        header_detected = (
            len(first) >= 2 and first[0] in USERNAME_HEADERS and first[1] in DISPLAY_NAME_HEADERS
        )
    selected = rows[1:] if header_detected else rows
    result = []
    for source_row, cells in selected:
        cleaned = [_clean_cell(value) for value in cells]
        errors = []
        if len(cleaned) != 2:
            errors.append("每个非空行必须恰好包含两列")
        result.append(
            RawImportRow(
                source_row=source_row,
                username=cleaned[0] if cleaned else "",
                display_name=cleaned[1] if len(cleaned) > 1 else "",
                parse_errors=errors,
            )
        )
    return header_detected, result


def _preview(rows: list[tuple[int, list[Any]]], existing_usernames: set[str]) -> ImportPreview:
    header_detected, raw_rows = _rows_from_cells(rows)
    preview_rows = validate_import_rows(raw_rows, existing_usernames)
    return ImportPreview(header_detected, [row.to_dict() for row in preview_rows])


def preview_text(value: str, existing_usernames: set[str]) -> ImportPreview:
    """解析每行严格两个空白分隔字段的粘贴文本。"""

    rows = []
    for source_row, line in enumerate(value.splitlines(), 1):
        if not line.strip():
            continue
        rows.append((source_row, re_split_whitespace(line.strip())))
    return _preview(rows, existing_usernames)


def re_split_whitespace(value: str) -> list[str]:
    """避免 CSV 规则影响文本输入，按任意连续空白切分。"""

    return value.split()


def preview_csv(value: bytes, existing_usernames: set[str]) -> ImportPreview:
    """解析 UTF-8 CSV，拒绝其他编码和 NUL。"""

    try:
        text = value.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("CSV 必须使用 UTF-8 编码") from exc
    if "\x00" in text:
        raise ValueError("CSV 不能包含 NUL 字符")
    reader = csv.reader(io.StringIO(text, newline=""))
    rows = [
        (index, row) for index, row in enumerate(reader, 1) if any(cell.strip() for cell in row)
    ]
    return _preview(rows, existing_usernames)


def preview_xlsx(value: bytes, existing_usernames: set[str]) -> ImportPreview:
    """读取 XLSX 第一个工作表的前两列，并限制解压后总大小。"""

    try:
        with zipfile.ZipFile(io.BytesIO(value)) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_XLSX_UNCOMPRESSED_BYTES:
                raise ValueError("XLSX 解压后内容过大")
    except zipfile.BadZipFile as exc:
        raise ValueError("文件不是有效的 XLSX") from exc
    try:
        workbook = load_workbook(io.BytesIO(value), read_only=True, data_only=True)
    except Exception as exc:
        raise ValueError("XLSX 无法读取") from exc
    try:
        worksheet = workbook.worksheets[0]
        rows = []
        for index, values in enumerate(worksheet.iter_rows(values_only=True), 1):
            cells = list(values)
            while cells and cells[-1] is None:
                cells.pop()
            if cells:
                rows.append((index, cells))
        return _preview(rows, existing_usernames)
    finally:
        workbook.close()
