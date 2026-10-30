"""密码生成规则：下限合并、自检、随机源与脱敏示例。"""

from __future__ import annotations

import random
import re
import secrets

import pytest

from lab_ops_panel.passwords import (
    AMBIGUOUS_DIGITS,
    AMBIGUOUS_LOWER,
    AMBIGUOUS_UPPER,
    PasswordRuleError,
    PasswordRules,
    PolicyFloor,
    character_classes,
    check_password,
    default_rules,
    generate_password,
    longest_class_repeat,
    longest_repeat,
    longest_sequence,
    mask_password,
    solve_rules,
)


def test_default_rules_are_strict_and_documented() -> None:
    rules = default_rules()

    assert rules.length == 20
    assert (rules.min_upper, rules.min_lower, rules.min_digits, rules.min_symbols) == (1, 1, 1, 1)
    assert rules.exclude_ambiguous is True


def test_generated_password_satisfies_rules_and_avoids_ambiguous_chars() -> None:
    effective = solve_rules(default_rules())

    password = generate_password(effective)

    assert len(password) == 20
    assert check_password(password, effective) == []
    for char in AMBIGUOUS_UPPER + AMBIGUOUS_LOWER + AMBIGUOUS_DIGITS:
        assert char not in password


def test_generation_uses_cryptographic_random_source() -> None:
    effective = solve_rules(default_rules())
    calls: list[str] = []

    class RecordingRandom(secrets.SystemRandom):
        def choice(self, seq):  # type: ignore[no-untyped-def]
            calls.append("choice")
            return super().choice(seq)

        def shuffle(self, x):  # type: ignore[no-untyped-def]
            calls.append("shuffle")
            return super().shuffle(x)

    original = secrets.SystemRandom
    try:
        secrets.SystemRandom = RecordingRandom  # type: ignore[misc]
        generate_password(effective)
    finally:
        secrets.SystemRandom = original  # type: ignore[misc]

    assert "choice" in calls
    assert "shuffle" in calls


def test_rules_reject_inconsistent_minimums() -> None:
    with pytest.raises(ValueError, match="不能超过总长度"):
        PasswordRules(length=8, min_upper=3, min_lower=3, min_digits=3, min_symbols=3)


def test_rules_reject_symbol_requirement_without_symbols() -> None:
    with pytest.raises(ValueError, match="符号集合"):
        PasswordRules(symbols="", min_symbols=1)


def test_rules_reject_whitespace_in_symbol_set() -> None:
    with pytest.raises(ValueError, match="空白"):
        PasswordRules(symbols="ab c")


def test_policy_floor_raises_length_and_classes_with_reasons() -> None:
    floor = PolicyFloor(min_length=24, min_upper=2, min_digits=3, min_classes=4, source="pam")

    effective = solve_rules(PasswordRules(length=12, min_upper=1), floor)

    assert effective.length == 24
    assert effective.min_upper == 2
    assert effective.min_digits == 3
    fields = {item["field"] for item in effective.adjustments}
    assert {"length", "minUpper", "minDigits"} <= fields
    assert all(item["reason"] for item in effective.adjustments)


def test_floor_cannot_be_relaxed_by_application_rules() -> None:
    floor = PolicyFloor(min_length=16)
    effective = solve_rules(PasswordRules(length=10), floor)

    password = generate_password(effective)

    assert len(password) >= 16


def test_check_password_reports_specific_violations() -> None:
    effective = solve_rules(PasswordRules(length=8), PolicyFloor(max_repeat=2, max_sequence=2))

    violations = check_password("aaaa1234", effective)

    assert any("重复" in item or "连续" in item for item in violations)


def test_check_password_enforces_username_rule() -> None:
    effective = solve_rules(PasswordRules(length=12), PolicyFloor(reject_username=True))

    violations = check_password("Zhangsan1!", effective, username="zhangsan")

    assert violations


def test_longest_helpers_match_expected_shapes() -> None:
    assert longest_repeat("aaab") == 3
    assert longest_sequence("abcde") == 5
    assert longest_sequence("a1B2") == 0
    assert longest_class_repeat("ab12") == 2
    assert longest_class_repeat("A1!b") == 1


def test_character_classes_counts() -> None:
    counts = character_classes("Aa1!")

    assert counts == {"upper": 1, "lower": 1, "digits": 1, "symbols": 1}


def test_generation_fails_when_floor_demands_impossible_classes() -> None:
    effective = solve_rules(default_rules(), PolicyFloor(min_classes=5))

    with pytest.raises(PasswordRuleError, match="字符类别"):
        generate_password(effective, rng=random.Random(1))


def test_mask_keeps_only_prefix() -> None:
    masked = mask_password("Abcdefghij")

    assert masked.startswith("Ab")
    assert masked.count("•") == 8
    assert not re.search(r"[cdefghij]", masked.replace("•", ""))


def test_generate_with_injected_rng_is_deterministic() -> None:
    effective = solve_rules(default_rules())

    first = generate_password(effective, rng=random.Random(7))
    second = generate_password(effective, rng=random.Random(7))

    assert first == second
