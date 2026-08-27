"""随机初始密码规则与密码学安全生成。"""

from __future__ import annotations

import math
import secrets
import string
from dataclasses import asdict, dataclass
from typing import Any

DEFAULT_SYMBOLS = "!@#$%^&*_-+=?"
AMBIGUOUS_CHARACTERS = frozenset("0O1lI")


@dataclass(frozen=True, slots=True)
class PasswordPolicy:
    """描述应用随机生成规则，不代表系统 PAM 的全部规则。"""

    length: int = 20
    min_uppercase: int = 1
    min_lowercase: int = 1
    min_digits: int = 1
    min_symbols: int = 1
    symbols: str = DEFAULT_SYMBOLS
    exclude_ambiguous: bool = True

    def validate(self) -> None:
        """拒绝无法生成或明显弱于首版下限的规则。"""

        minimums = (
            self.min_uppercase,
            self.min_lowercase,
            self.min_digits,
            self.min_symbols,
        )
        if not 15 <= self.length <= 128:
            raise ValueError("密码长度必须在 15 到 128 之间")
        if any(value < 0 for value in minimums):
            raise ValueError("各字符类别最少数量不能为负数")
        if sum(minimums) > self.length:
            raise ValueError("各字符类别最少数量之和不能超过密码长度")
        if self.min_symbols and not self.symbols:
            raise ValueError("要求符号时允许的符号集合不能为空")
        if any(character.isspace() or not character.isprintable() for character in self.symbols):
            raise ValueError("符号集合只能包含可打印的非空白字符")
        if ":" in self.symbols:
            raise ValueError("符号集合不能包含冒号，以免破坏系统密码输入协议")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("符号集合不能包含重复字符")

    def to_dict(self) -> dict[str, Any]:
        """转换为可供设置页展示的普通字典。"""

        return asdict(self)


def _alphabet(value: str, exclude_ambiguous: bool) -> str:
    return "".join(
        character
        for character in value
        if not exclude_ambiguous or character not in AMBIGUOUS_CHARACTERS
    )


def generate_password(policy: PasswordPolicy) -> str:
    """使用操作系统密码学安全随机源生成满足规则的密码。"""

    policy.validate()
    uppercase = _alphabet(string.ascii_uppercase, policy.exclude_ambiguous)
    lowercase = _alphabet(string.ascii_lowercase, policy.exclude_ambiguous)
    digits = _alphabet(string.digits, policy.exclude_ambiguous)
    symbols = _alphabet(policy.symbols, policy.exclude_ambiguous)
    categories = (
        (uppercase, policy.min_uppercase),
        (lowercase, policy.min_lowercase),
        (digits, policy.min_digits),
        (symbols, policy.min_symbols),
    )
    if any(required and not alphabet for alphabet, required in categories):
        raise ValueError("排除易混淆字符后某个必需字符类别为空")
    characters = [secrets.choice(alphabet) for alphabet, count in categories for _ in range(count)]
    combined = "".join(alphabet for alphabet, _ in categories)
    characters.extend(secrets.choice(combined) for _ in range(policy.length - len(characters)))
    secrets.SystemRandom().shuffle(characters)
    return "".join(characters)


def password_summary(policy: PasswordPolicy) -> dict[str, Any]:
    """返回不泄露实际密码的规则摘要和保守熵估算。"""

    policy.validate()
    alphabet = "".join(
        (
            _alphabet(string.ascii_uppercase, policy.exclude_ambiguous),
            _alphabet(string.ascii_lowercase, policy.exclude_ambiguous),
            _alphabet(string.digits, policy.exclude_ambiguous),
            _alphabet(policy.symbols, policy.exclude_ambiguous),
        )
    )
    return {
        "masked_example": "•" * policy.length,
        "estimated_entropy_bits": round(policy.length * math.log2(len(set(alphabet))), 1),
        "policy": policy.to_dict(),
    }
