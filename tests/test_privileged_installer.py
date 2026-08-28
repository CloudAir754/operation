"""特权助手安装器的纯规则测试。"""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/install_privileged_helper.py"
SPEC = importlib.util.spec_from_file_location("install_privileged_helper", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_sudoers_rule_only_allows_fixed_actions() -> None:
    """安装规则绑定启动用户，且不授予任意参数或 shell。"""

    content = MODULE.sudoers_content("operator", 1000)

    assert "operator ALL=(root) NOPASSWD:" in content
    assert content.count("/usr/local/libexec/labopspanel-privileged") == 11
    assert "create-user" in content
    assert "ssh-import" in content
    assert "sudo-grant" in content
    assert "sudo-revoke" in content
    assert "pam-update" in content
    assert "pam-restore" in content
    assert "port-lookup" in content
    assert "process-terminate" in content
    assert "uid-processes-terminate" in content
    assert "user-delete-preflight" in content
    assert "user-delete" in content
    assert " ALL\n" not in content


def test_sudoers_rule_rejects_root_and_invalid_username() -> None:
    """不能为 root 或含 sudoers 语法字符的名称生成规则。"""

    with pytest.raises(ValueError):
        MODULE.sudoers_content("root", 0)
    with pytest.raises(ValueError):
        MODULE.sudoers_content("alice ALL=(ALL)", 1000)
