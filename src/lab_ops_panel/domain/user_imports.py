"""批量账号导入的领域校验规则。"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

USERNAME = re.compile(r"^[a-z_][a-z0-9_-]{0,30}[$]?$")


@dataclass(slots=True)
class RawImportRow:
    """表示解析器产生、尚未执行领域校验的一行。"""

    source_row: int
    username: str
    display_name: str
    parse_errors: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ImportPreviewRow:
    """表示管理员预览页中的逐行判定。"""

    source_row: int
    username: str
    display_name: str
    status: str
    errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        """转换为协议无关的普通字典。"""

        return asdict(self)


def validate_import_rows(
    rows: list[RawImportRow], existing_usernames: set[str]
) -> list[ImportPreviewRow]:
    """校验格式、重复、用户名、显示名称和本机重名。"""

    counts = Counter(row.username for row in rows if row.username)
    result = []
    for row in rows:
        errors = list(row.parse_errors)
        if not row.username:
            errors.append("用户名不能为空")
        elif not USERNAME.fullmatch(row.username):
            errors.append("用户名必须为最多 32 位的小写字母、数字、下划线或连字符")
        if not row.display_name:
            errors.append("中文姓名不能为空")
        elif any(character.isspace() for character in row.display_name):
            errors.append("中文姓名不能包含空白字符")
        if row.username and counts[row.username] > 1:
            errors.append("导入数据中用户名重复")
        if errors:
            status = "invalid"
        elif row.username in existing_usernames:
            status = "exists"
        else:
            status = "will_create"
        result.append(
            ImportPreviewRow(
                source_row=row.source_row,
                username=row.username,
                display_name=row.display_name,
                status=status,
                errors=errors,
            )
        )
    return result
