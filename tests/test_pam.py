"""PAM 密码策略能力检测测试。"""

from pathlib import Path

from lab_ops_panel.infrastructure.pam import pam_policy_snapshot


def test_pam_policy_applies_drop_ins_then_module_options(tmp_path: Path) -> None:
    """模块参数覆盖配置文件，并转换负 credit 为生成下限。"""

    common = tmp_path / "common-password"
    common.write_text(
        "password requisite pam_pwquality.so retry=4 minlen=20 dcredit=-2\n"
        "password [success=1 default=ignore] pam_unix.so obscure yescrypt\n",
        encoding="utf-8",
    )
    config = tmp_path / "pwquality.conf"
    config.write_text("minlen = 16\ndcredit = -1\n", encoding="utf-8")
    drop_ins = tmp_path / "pwquality.conf.d"
    drop_ins.mkdir()
    (drop_ins / "50-local.conf").write_text("ucredit = -1\nminlen = 18\n", encoding="utf-8")
    module = tmp_path / "usr/lib/test/security/pam_pwquality.so"
    module.parent.mkdir(parents=True)
    module.touch()

    snapshot = pam_policy_snapshot(common, config, drop_ins, str(module))

    assert snapshot["settings"]["minlen"] == 20
    assert snapshot["settings"]["dcredit"] == -2
    assert snapshot["generation_minimums"] == {
        "length": 20,
        "min_digits": 2,
        "min_uppercase": 1,
        "min_lowercase": 0,
        "min_symbols": 0,
        "min_classes": 0,
    }
    assert snapshot["modification_supported"] is True


def test_pam_policy_reports_missing_module(tmp_path: Path) -> None:
    """未安装 pwquality 时只读展示，不能开放修改。"""

    common = tmp_path / "common-password"
    common.write_text("password required pam_unix.so obscure yescrypt\n", encoding="utf-8")

    snapshot = pam_policy_snapshot(
        common,
        tmp_path / "missing.conf",
        tmp_path / "missing.d",
        str(tmp_path / "missing-module"),
    )

    assert snapshot["pwquality_enabled"] is False
    assert snapshot["modification_supported"] is False
    assert "未安装" in snapshot["modification_reason"]


def test_pam_policy_unknown_setting_blocks_modification(tmp_path: Path) -> None:
    """无法识别的手工配置必须阻断自动修改。"""

    common = tmp_path / "common-password"
    common.write_text("password requisite pam_pwquality.so\n", encoding="utf-8")
    config = tmp_path / "pwquality.conf"
    config.write_text("custom_future_rule = 1\n", encoding="utf-8")
    module = tmp_path / "pam_pwquality.so"
    module.touch()

    snapshot = pam_policy_snapshot(common, config, tmp_path / "missing", str(module))

    assert snapshot["warnings"]
    assert snapshot["modification_supported"] is False
