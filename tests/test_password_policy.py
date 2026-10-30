"""Ubuntu 密码策略解析、差异与最小化改写。"""

from __future__ import annotations

import pytest

from accounts_support import PAM_AUTH_TEXT, PAM_PASSWORD_TEXT, POLICY_CONF_TEXT
from lab_ops_panel.remote.password_policy import (
    POLICY_PATH,
    build_floor,
    build_snapshot,
    changed_fields,
    find_pwquality_line,
    find_unix_auth_line,
    parse_conf,
    parse_login_defs,
    render_conf,
    render_pam_retry,
    render_pwquality_args,
    unified_diff,
    validate_targets,
)

CONF = """\
# 手工维护，不得覆盖
minlen = 8
minclass = 2
dcredit = -1
unknown_key = 5
maxrepeat = 3
"""

PAM_PASSWORD = """\
password\trequisite\t\t\tpam_pwquality.so retry=3
password\t[success=1 default=ignore]\tpam_unix.so obscure yescrypt
"""

PAM_AUTH = """\
auth\t[success=1 default=ignore]\tpam_unix.so nullok retry=3
auth\trequisite\t\t\tpam_deny.so
"""


def _payload(conf: str = CONF, pam: str = PAM_PASSWORD, auth: str = PAM_AUTH) -> dict:
    return {
        "os": {"id": "ubuntu", "versionId": "24.04", "prettyName": "Ubuntu 24.04 LTS"},
        "pwquality": {
            "path": POLICY_PATH,
            "exists": True,
            "readable": True,
            "content": conf,
            "dropInDir": False,
            "dropIns": [],
        },
        "pam": {
            "commonPassword": {"path": "/etc/pam.d/common-password", "content": pam},
            "commonAuth": {"path": "/etc/pam.d/common-auth", "content": auth},
        },
        "loginDefs": {
            "path": "/etc/login.defs",
            "content": "PASS_MIN_LEN 8\nENCRYPT_METHOD yescrypt\n",
        },
        "capabilities": {"pwscore": True, "chage": True, "useradd": True, "rsync": True},
    }


def test_parse_conf_keeps_unknown_keys_and_ignores_comments() -> None:
    parsed = parse_conf(CONF)

    assert parsed.settings == {"minlen": 8, "minclass": 2, "dcredit": -1, "maxrepeat": 3}
    assert "unknown_key = 5" in parsed.unknown_keys


def test_parse_conf_tolerates_garbage_lines() -> None:
    parsed = parse_conf("minlen=8\nnot a key value\n=4\nx = y\n")

    assert parsed.settings == {"minlen": 8}
    assert len(parsed.unknown_keys) == 3


def test_parse_login_defs() -> None:
    values = parse_login_defs("# c\nPASS_MIN_LEN\t8\nENCRYPT_METHOD yescrypt\n")

    assert values == {"PASS_MIN_LEN": "8", "ENCRYPT_METHOD": "yescrypt"}


def test_snapshot_prefers_pam_arguments_over_config_file() -> None:
    payload = _payload(pam="password requisite pam_pwquality.so minlen=16 retry=3\n")

    snapshot = build_snapshot(payload)

    assert snapshot.values["minlen"] == 16
    assert snapshot.origins["minlen"] == "pam"
    assert snapshot.values["minclass"] == 2
    assert snapshot.origins["minclass"] == "conf"


def test_snapshot_reports_read_only_when_module_missing() -> None:
    snapshot = build_snapshot(_payload(pam="password requisite pam_unix.so yescrypt\n"))

    assert snapshot.capabilities["pwqualityModule"] is False
    assert "retry" in snapshot.capabilities["writableFields"]
    assert snapshot.capabilities["readOnly"] is False  # conf 仍可写
    assert any("pwquality" in note for note in snapshot.notes)


def test_snapshot_marks_ambiguous_pam_lines_as_read_only() -> None:
    payload = _payload(
        pam=(
            "password requisite pam_pwquality.so minlen=12\n"
            'password requisite pam_pwquality.so "minlen=20"\n'
        )
    )

    snapshot = build_snapshot(payload)

    assert snapshot.pwquality.ambiguous is True
    # 不可信的行内参数不能冒充“实际生效值”，回退到配置文件。
    assert snapshot.origins["minlen"] == "conf"
    assert snapshot.values["minlen"] == 8
    assert any("无法可靠解析" in note for note in snapshot.notes)


def test_snapshot_without_conf_file_blocks_conf_writes() -> None:
    payload = _payload(conf="")
    payload["pwquality"]["exists"] = False

    snapshot = build_snapshot(payload)

    assert "minlen" not in snapshot.capabilities["writableFields"]
    assert any("不存在" in reason for reason in snapshot.capabilities["reasons"])


def test_retry_origin_and_value_come_from_common_auth() -> None:
    snapshot = build_snapshot(
        _payload(auth="auth [success=1 default=ignore] pam_unix.so retry=5\n")
    )

    assert snapshot.values["retry"] == 5
    assert snapshot.origins["retry"] == "pam"


def test_find_lines_returns_reasons_when_absent() -> None:
    assert find_pwquality_line("# empty\n").present is False
    assert find_unix_auth_line("# empty\n").present is False


def test_render_conf_updates_managed_keys_and_preserves_unknown_lines() -> None:
    rendered = render_conf(CONF, {"minlen": 14, "maxrepeat": 2}, header="labops")

    assert "minlen = 14" in rendered
    assert "maxrepeat = 2" in rendered
    assert "unknown_key = 5" in rendered
    assert "# 手工维护，不得覆盖" in rendered


def test_render_conf_appends_new_keys_with_header() -> None:
    rendered = render_conf("minlen = 8\n", {"maxsequence": 3}, header="labops-panel")

    assert "# labops-panel" in rendered
    assert "maxsequence = 3" in rendered


def test_render_pam_retry_replaces_only_retry_token() -> None:
    rendered = render_pam_retry(PAM_AUTH, 6)

    assert "retry=6" in rendered
    assert "pam_deny.so" in rendered
    assert "nullok" in rendered


def test_render_pam_retry_rejects_ambiguous_lines() -> None:
    with pytest.raises(ValueError, match="手工配置处理"):
        render_pam_retry('auth [success=1] pam_unix.so "retry=3"\n', 6)


def test_render_pwquality_args_updates_existing_pam_keys() -> None:
    rendered = render_pwquality_args(PAM_PASSWORD, {"minlen": 12})

    assert "minlen=12" in rendered
    assert "retry=3" in rendered


def test_unified_diff_mentions_both_sides() -> None:
    diff = unified_diff("minlen = 8\n", "minlen = 12\n", POLICY_PATH)

    assert "-minlen = 8" in diff
    assert "+minlen = 12" in diff


def test_changed_fields_only_reports_real_changes() -> None:
    changes = changed_fields({"minlen": 8, "retry": 3}, {"minlen": 8, "retry": 5})

    assert changes == {"retry": {"before": 3, "after": 5}}


def test_validate_targets_rejects_unknown_and_out_of_range() -> None:
    with pytest.raises(ValueError, match="未知策略字段"):
        validate_targets({"nope": 1})
    with pytest.raises(ValueError, match="之间"):
        validate_targets({"minlen": 4})
    with pytest.raises(ValueError, match="整数"):
        validate_targets({"minlen": "12"})  # type: ignore[dict-item]
    assert validate_targets({"minlen": 12, "retry": 4}) == {"minlen": 12, "retry": 4}


def test_build_floor_translates_policy_to_generation_floor() -> None:
    payload = _payload(
        conf=(
            "minlen = 14\nminclass = 3\nucredit = 2\ndcredit = 1\n"
            "maxrepeat = 2\nmaxsequence = 3\nmaxclassrepeat = 4\ngecoscheck = 1\n"
        )
    )
    floor = build_floor(build_snapshot(payload))

    assert floor.min_length == 14
    assert floor.min_classes == 3
    assert floor.min_upper == 2
    assert floor.min_digits == 1
    assert floor.max_repeat == 2
    assert floor.max_sequence == 3
    assert floor.max_class_repeat == 4
    assert floor.reject_username is True


def test_negative_credit_means_limit_not_requirement() -> None:
    floor = build_floor(build_snapshot(_payload()))

    assert floor.min_digits == 0
    assert floor.min_length == 8


def test_reference_fixtures_are_parseable() -> None:
    snapshot = build_snapshot(
        _payload(conf=POLICY_CONF_TEXT, pam=PAM_PASSWORD_TEXT, auth=PAM_AUTH_TEXT)
    )

    assert snapshot.values["minlen"] == 8
    assert snapshot.values["retry"] == 3
