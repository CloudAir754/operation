"""随机密码领域规则测试。"""

import string

import pytest

from lab_ops_panel.domain.passwords import (
    AMBIGUOUS_CHARACTERS,
    PasswordPolicy,
    generate_password,
    password_summary,
)


def test_default_password_uses_secure_policy_character_classes() -> None:
    """默认生成结果满足长度、字符类别和易混淆字符排除规则。"""

    policy = PasswordPolicy()
    for _ in range(50):
        password = generate_password(policy)
        assert len(password) == 20
        assert any(value in string.ascii_uppercase for value in password)
        assert any(value in string.ascii_lowercase for value in password)
        assert any(value in string.digits for value in password)
        assert any(value in policy.symbols for value in password)
        assert not set(password) & AMBIGUOUS_CHARACTERS


def test_password_policy_rejects_impossible_minimums() -> None:
    """字符类别下限之和超过长度时拒绝保存。"""

    with pytest.raises(ValueError, match="不能超过"):
        PasswordPolicy(
            length=15,
            min_uppercase=5,
            min_lowercase=5,
            min_digits=5,
            min_symbols=5,
        ).validate()


def test_password_policy_rejects_weak_length() -> None:
    """应用规则不得低于已确认的 15 位安全下限。"""

    with pytest.raises(ValueError, match="15"):
        PasswordPolicy(length=14).validate()


def test_password_summary_never_contains_generated_secret() -> None:
    """实时预览只展示掩码和规则，不返回可用密码。"""

    summary = password_summary(PasswordPolicy())
    assert summary["masked_example"] == "•" * 20
    assert summary["estimated_entropy_bits"] > 100
    assert set(summary) == {"masked_example", "estimated_entropy_bits", "policy"}
