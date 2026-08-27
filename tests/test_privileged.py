"""特权助手信任边界测试。"""

from pathlib import Path

import pytest

from lab_ops_panel.infrastructure.privileged import PrivilegedExecutor, helper_capability


def test_user_owned_helper_is_rejected(tmp_path: Path) -> None:
    """Web 进程不能通过 sudo 执行当前用户可修改的助手。"""

    helper = tmp_path / "helper"
    helper.write_text("#!/bin/sh\n", encoding="utf-8")
    helper.chmod(0o755)

    capability = helper_capability(helper)
    assert capability["available"] is False
    with pytest.raises(RuntimeError, match="root"):
        PrivilegedExecutor(helper).execute("create-user", {"username": "alice"})


def test_unknown_privileged_action_is_rejected_before_execution() -> None:
    """动作名必须来自代码内白名单。"""

    with pytest.raises(ValueError, match="不支持"):
        PrivilegedExecutor().execute("arbitrary-shell", {})
