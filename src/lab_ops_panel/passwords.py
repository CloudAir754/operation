"""目标账户密码规则、下限合并与强随机生成。

设计要点：

- 随机源只使用 ``secrets.SystemRandom``（底层 ``os.urandom``），绝不使用
  ``random`` 的默认伪随机发生器。
- 应用内规则不能弱于系统实际生效的 PAM 策略：``solve_rules`` 把系统下限合并进
  规则并给出“因系统策略而提高”的说明。
- 生成结果必须通过同一套自检（长度、字符类别、最大重复/连续/同类重复、
  用户名包含限制）。不通过就重新抽取，超过上限直接报错，不降级放行。
"""

from __future__ import annotations

import secrets
import string
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import Field, field_validator, model_validator

from lab_ops_panel.schemas import StrictApiModel

MIN_LENGTH: Final = 8
MAX_LENGTH: Final = 128
MAX_ATTEMPTS: Final = 500
DEFAULT_LENGTH: Final = 20
DEFAULT_SYMBOLS: Final = "!#%+-_=.:?@^~*"
AMBIGUOUS_UPPER: Final = "OI"
AMBIGUOUS_LOWER: Final = "l"
AMBIGUOUS_DIGITS: Final = "01"
CLASS_ORDER: Final = ("upper", "lower", "digits", "symbols")
CLASS_LABELS: Final = {
    "upper": "大写字母",
    "lower": "小写字母",
    "digits": "数字",
    "symbols": "符号",
}
MAX_SYMBOLS_LENGTH: Final = 64


class PasswordRuleError(ValueError):
    """规则不一致或无法生成满足规则的密码。"""


class PasswordGenerationError(PasswordRuleError):
    """达到重试上限仍无法生成合规密码。"""


class PasswordRules(StrictApiModel):
    """管理员可调的目标账户密码生成规则。"""

    length: int = Field(default=DEFAULT_LENGTH, ge=MIN_LENGTH, le=MAX_LENGTH)
    min_upper: int = Field(default=1, ge=0, le=MAX_LENGTH)
    min_lower: int = Field(default=1, ge=0, le=MAX_LENGTH)
    min_digits: int = Field(default=1, ge=0, le=MAX_LENGTH)
    min_symbols: int = Field(default=1, ge=0, le=MAX_LENGTH)
    symbols: str = Field(default=DEFAULT_SYMBOLS, max_length=MAX_SYMBOLS_LENGTH)
    exclude_ambiguous: bool = True
    exclude_chars: str = Field(default="", max_length=MAX_SYMBOLS_LENGTH)

    @field_validator("symbols", "exclude_chars")
    @classmethod
    def _reject_unprintable(cls, value: str) -> str:
        for char in value:
            if char.isspace() or not char.isprintable() or ord(char) < 0x21:
                raise ValueError("符号集合不允许包含空白或控制字符")
        return "".join(dict.fromkeys(value))

    @model_validator(mode="after")
    def _consistent(self) -> PasswordRules:
        required = self.min_upper + self.min_lower + self.min_digits + self.min_symbols
        if required > self.length:
            raise ValueError(f"各类最少字符数之和（{required}）不能超过总长度（{self.length}）")
        if self.min_symbols > 0 and not self.symbols:
            raise ValueError("要求最少符号数时必须提供允许的符号集合")
        return self


def default_rules() -> PasswordRules:
    return PasswordRules()


@dataclass(frozen=True)
class PolicyFloor:
    """系统实际生效策略给出的下限（应用规则不得低于它）。"""

    min_length: int = MIN_LENGTH
    min_upper: int = 0
    min_lower: int = 0
    min_digits: int = 0
    min_symbols: int = 0
    min_classes: int = 0
    max_repeat: int = 0
    max_sequence: int = 0
    max_class_repeat: int = 0
    reject_username: bool = False
    source: str = "builtin"
    notes: tuple[str, ...] = ()


BUILTIN_FLOOR: Final = PolicyFloor(
    notes=("未读取到系统策略时使用内置下限：长度 8、无字符类别要求",)
)


@dataclass(frozen=True)
class EffectiveRules:
    """合并系统下限后的实际生成规则。"""

    rules: PasswordRules
    floor: PolicyFloor
    length: int
    min_upper: int
    min_lower: int
    min_digits: int
    min_symbols: int
    min_classes: int
    symbols: str
    exclude_ambiguous: bool
    exclude_chars: str
    adjustments: list[dict[str, Any]] = field(default_factory=list)

    @property
    def min_counts(self) -> dict[str, int]:
        return {
            "upper": self.min_upper,
            "lower": self.min_lower,
            "digits": self.min_digits,
            "symbols": self.min_symbols,
        }


def solve_rules(rules: PasswordRules, floor: PolicyFloor | None = None) -> EffectiveRules:
    """把系统下限合并进规则；只上不下，并记录每处提高的原因。"""

    effective_floor = floor or BUILTIN_FLOOR
    adjustments: list[dict[str, Any]] = []

    def raise_to(name: str, label: str, requested: int, minimum: int) -> int:
        if minimum > requested:
            adjustments.append(
                {
                    "field": name,
                    "label": label,
                    "requested": requested,
                    "applied": minimum,
                    "reason": "系统 PAM 策略更严格",
                }
            )
            return minimum
        return requested

    length = raise_to("length", "长度", rules.length, max(MIN_LENGTH, effective_floor.min_length))
    min_upper = raise_to("minUpper", "最少大写字母", rules.min_upper, effective_floor.min_upper)
    min_lower = raise_to("minLower", "最少小写字母", rules.min_lower, effective_floor.min_lower)
    min_digits = raise_to("minDigits", "最少数字", rules.min_digits, effective_floor.min_digits)
    min_symbols = raise_to("minSymbols", "最少符号", rules.min_symbols, effective_floor.min_symbols)

    required = min_upper + min_lower + min_digits + min_symbols
    if required > length:
        needed = required
        adjustments.append(
            {
                "field": "length",
                "label": "长度",
                "requested": length,
                "applied": needed,
                "reason": "系统策略要求的字符类别数量超过原长度",
            }
        )
        length = needed

    return EffectiveRules(
        rules=rules,
        floor=effective_floor,
        length=length,
        min_upper=min_upper,
        min_lower=min_lower,
        min_digits=min_digits,
        min_symbols=min_symbols,
        min_classes=max(effective_floor.min_classes, 0),
        symbols=rules.symbols,
        exclude_ambiguous=rules.exclude_ambiguous,
        exclude_chars=rules.exclude_chars,
        adjustments=adjustments,
    )


def _excluded_chars(rules: PasswordRules) -> str:
    excluded = rules.exclude_chars
    if rules.exclude_ambiguous:
        excluded += AMBIGUOUS_UPPER + AMBIGUOUS_LOWER + AMBIGUOUS_DIGITS
    return excluded


def _pools(effective: EffectiveRules) -> dict[str, str]:
    excluded = set(_excluded_chars(effective.rules))
    raw = {
        "upper": string.ascii_uppercase,
        "lower": string.ascii_lowercase,
        "digits": string.digits,
        "symbols": effective.symbols,
    }
    return {
        name: "".join(char for char in pool if char not in excluded) for name, pool in raw.items()
    }


def character_classes(password: str) -> dict[str, int]:
    counts = dict.fromkeys(CLASS_ORDER, 0)
    for char in password:
        if char.isdigit() and char.isascii():
            counts["digits"] += 1
        elif char.isalpha() and char.isascii():
            counts["upper" if char.isupper() else "lower"] += 1
        else:
            counts["symbols"] += 1
    return counts


def longest_repeat(password: str) -> int:
    longest = 0
    run = 0
    previous = ""
    for char in password:
        run = run + 1 if char == previous else 1
        previous = char
        longest = max(longest, run)
    return longest


def longest_sequence(password: str) -> int:
    """最长单调 +1 / -1 连续序列长度（与 libpwquality 的 maxsequence 口径一致）。"""

    longest = 0
    run = 0
    previous_step: int | None = None
    for index in range(1, len(password)):
        step = ord(password[index]) - ord(password[index - 1])
        if step in (1, -1):
            run = run + 1 if step == previous_step else 2
            previous_step = step
        else:
            run = 0
            previous_step = None
        longest = max(longest, run)
    return longest


def _class_of(char: str) -> str:
    if char.isdigit() and char.isascii():
        return "digits"
    if char.isalpha() and char.isascii():
        return "upper" if char.isupper() else "lower"
    return "symbols"


def longest_class_repeat(password: str) -> int:
    longest = 0
    run = 0
    previous: str | None = None
    for char in password:
        current = _class_of(char)
        run = run + 1 if current == previous else 1
        previous = current
        longest = max(longest, run)
    return longest


def check_password(
    password: str, effective: EffectiveRules, *, username: str | None = None
) -> list[str]:
    """返回不满足规则的条目；空列表表示合规。"""

    violations: list[str] = []
    if len(password) < effective.length:
        violations.append(f"长度不足（需要 {effective.length} 位，实际 {len(password)} 位）")
    counts = character_classes(password)
    for name in CLASS_ORDER:
        minimum = effective.min_counts[name]
        if counts[name] < minimum:
            violations.append(f"{CLASS_LABELS[name]}不足（需要 {minimum} 个）")
    classes = sum(1 for count in counts.values() if count > 0)
    if effective.min_classes and classes < effective.min_classes:
        violations.append(f"字符类别不足（需要 {effective.min_classes} 类）")
    floor = effective.floor
    if floor.max_repeat and longest_repeat(password) > floor.max_repeat:
        violations.append(f"重复字符过多（上限 {floor.max_repeat}）")
    if floor.max_sequence and longest_sequence(password) > floor.max_sequence:
        violations.append(f"连续字符过多（上限 {floor.max_sequence}）")
    if floor.max_class_repeat and longest_class_repeat(password) > floor.max_class_repeat:
        violations.append(f"同类字符连续过多（上限 {floor.max_class_repeat}）")
    if username and floor.reject_username and _contains_username(password, username):
        violations.append("密码不能包含用户名")
    return violations


def _contains_username(password: str, username: str) -> bool:
    lowered = password.lower()
    login = username.lower()
    if len(login) < 3:
        return login in lowered
    return login in lowered or (len(login) > 4 and login[:4] in lowered)


def generate_password(
    effective: EffectiveRules,
    *,
    username: str | None = None,
    rng: Any = None,
) -> str:
    """按合并后的规则生成密码；只用密码学安全随机源。"""

    random_source = rng if rng is not None else secrets.SystemRandom()
    pools = _pools(effective)
    for name in CLASS_ORDER:
        if effective.min_counts[name] > 0 and not pools[name]:
            raise PasswordRuleError(f"{CLASS_LABELS[name]}可用字符为空，无法满足最少数量要求")

    counts = dict(effective.min_counts)
    selected = [name for name in CLASS_ORDER if counts[name] > 0]
    coverage = max(effective.min_classes, len(selected))
    if coverage > len(selected):
        for name in CLASS_ORDER:
            if coverage <= len(selected):
                break
            if name not in selected and pools[name]:
                counts[name] = 1
                selected.append(name)
    if coverage > len(selected):
        raise PasswordRuleError("可用字符不足以覆盖系统要求的字符类别数量")

    required = sum(counts.values())
    if required > effective.length:
        raise PasswordRuleError(
            f"字符数量（{required}）超过密码长度（{effective.length}），无法生成合规密码"
        )
    alphabet = "".join(pool for pool in pools.values() if pool)
    if not alphabet:  # pragma: no cover - 上面的检查已覆盖
        raise PasswordRuleError("没有可用字符")

    last_violations: list[str] = []
    for _ in range(MAX_ATTEMPTS):
        characters = [
            random_source.choice(pools[name]) for name in CLASS_ORDER for _ in range(counts[name])
        ]
        characters.extend(
            random_source.choice(alphabet) for _ in range(effective.length - required)
        )
        random_source.shuffle(characters)
        candidate = "".join(characters)
        last_violations = check_password(candidate, effective, username=username)
        if not last_violations:
            return candidate
    raise PasswordGenerationError(
        f"连续 {MAX_ATTEMPTS} 次生成都不满足规则：{'；'.join(last_violations)}"
    )


def mask_password(password: str, *, reveal: int = 2) -> str:
    """脱敏示例：只保留前若干位。"""

    if reveal <= 0:
        return "•" * len(password)
    return password[:reveal] + "•" * max(0, len(password) - reveal)


def rules_payload(rules: PasswordRules) -> dict[str, Any]:
    return rules.model_dump(mode="json", by_alias=True)


def examples(
    rules: PasswordRules, floor: PolicyFloor | None = None, *, count: int = 1
) -> list[str]:
    effective = solve_rules(rules, floor)
    return [mask_password(generate_password(effective)) for _ in range(max(1, count))]
