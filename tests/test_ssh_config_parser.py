"""SSH 配置解析测试：Include、ProxyJump、通配默认值与错误处理。"""

from __future__ import annotations

from pathlib import Path

from lab_ops_panel.ssh.config_parser import SshConfigParser, split_config_line


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_explicit_host_fields_are_parsed(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config",
        """
Host lab-gpu-01
    HostName 10.0.0.11
    User labadmin
    Port 2222
    IdentityFile ~/.ssh/id_ed25519
    ProxyJump bastion
    ConnectTimeout 7
""",
    )

    parsed = SshConfigParser(config).parse()

    assert parsed.exists is True
    assert parsed.issues == []
    host = parsed.hosts[0]
    assert host.alias == "lab-gpu-01"
    assert host.hostname == "10.0.0.11"
    assert host.user == "labadmin"
    assert host.port == 2222
    assert host.identity_file == "~/.ssh/id_ed25519"
    assert host.proxy_jump == "bastion"
    assert host.connect_timeout == 7


def test_equals_and_quoted_values_are_supported(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config",
        'Host lab-a\n    HostName="10.0.0.5"\n    User "lab admin"\n',
    )

    host = SshConfigParser(config).parse().hosts[0]

    assert host.hostname == "10.0.0.5"
    assert host.user == "lab admin"


def test_include_globs_are_expanded_in_place(tmp_path: Path) -> None:
    conf_dir = tmp_path / ".ssh"
    _write(
        conf_dir / "config",
        """
Include conf.d/*
Host direct
    HostName 10.0.0.1
""",
    )
    _write(
        conf_dir / "conf.d" / "10-gpu",
        """
Host included-gpu
    HostName 10.0.0.2
    ProxyCommand ssh -W %h:%p bastion
""",
    )
    _write(conf_dir / "conf.d" / "20-cpu", "Host included-cpu\n    HostName 10.0.0.3\n")

    parsed = SshConfigParser(conf_dir / "config").parse()

    aliases = {host.alias for host in parsed.hosts}
    assert aliases == {"direct", "included-gpu", "included-cpu"}
    gpu = next(host for host in parsed.hosts if host.alias == "included-gpu")
    assert gpu.proxy_command == "ssh -W %h:%p bastion"
    assert len(parsed.includes) == 1
    assert len(parsed.includes[0].resolved) == 2


def test_missing_include_records_reason_without_failing(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config",
        "Include conf.d/*.conf\nHost lab-a\n    HostName 10.0.0.1\n",
    )

    parsed = SshConfigParser(config).parse()

    assert [host.alias for host in parsed.hosts] == ["lab-a"]
    assert parsed.includes[0].error == "未匹配任何文件"


def test_include_cycle_is_reported(tmp_path: Path) -> None:
    _write(tmp_path / "config", "Include other\nHost lab-a\n    HostName 10.0.0.1\n")
    _write(tmp_path / "other", "Include config\n")

    parsed = SshConfigParser(tmp_path / "config").parse()

    assert any("循环 Include" in issue.message for issue in parsed.issues)
    assert [host.alias for host in parsed.hosts] == ["lab-a"]


def test_wildcard_block_supplies_defaults_for_unset_options(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config",
        """
Host *
    User ubuntu
    Port 2222
    IdentityFile ~/.ssh/work

Host lab-a
    HostName 10.0.0.7
""",
    )

    host = SshConfigParser(config).parse().hosts[0]

    assert host.user == "ubuntu"
    assert host.port == 2222
    assert host.identity_file == "~/.ssh/work"
    assert host.hostname == "10.0.0.7"


def test_explicit_value_wins_over_later_wildcard(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config",
        """
Host lab-a
    HostName 10.0.0.7
    User labadmin

Host *
    User ubuntu
""",
    )

    host = SshConfigParser(config).parse().hosts[0]

    assert host.user == "labadmin"


def test_negated_pattern_excludes_wildcard_defaults(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config",
        """
Host * !lab-prod
    User ubuntu

Host lab-prod
    HostName 10.0.0.9
""",
    )

    host = SshConfigParser(config).parse().hosts[0]

    assert host.alias == "lab-prod"
    assert host.user is None


def test_invalid_port_drops_host_and_records_issue(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config",
        """
Host good
    HostName 10.0.0.1

Host bad
    HostName 10.0.0.2
    Port 70000
""",
    )

    parsed = SshConfigParser(config).parse()

    assert [host.alias for host in parsed.hosts] == ["good"]
    assert any("Port 超出范围" in issue.message for issue in parsed.issues)


def test_non_integer_port_drops_host(tmp_path: Path) -> None:
    config = _write(tmp_path / "config", "Host bad\n    HostName 10.0.0.2\n    Port abc\n")

    parsed = SshConfigParser(config).parse()

    assert parsed.hosts == []
    assert any("Port 不是整数" in issue.message for issue in parsed.issues)


def test_duplicate_host_blocks_are_merged(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config",
        """
Host lab-a
    HostName 10.0.0.1

Host lab-a
    User labadmin
""",
    )

    parsed = SshConfigParser(config).parse()

    assert len(parsed.hosts) == 1
    assert parsed.hosts[0].hostname == "10.0.0.1"
    assert parsed.hosts[0].user == "labadmin"


def test_alias_without_hostname_falls_back_to_alias(tmp_path: Path) -> None:
    config = _write(tmp_path / "config", "Host lab-a\n    User labadmin\n")

    host = SshConfigParser(config).parse().hosts[0]

    assert host.hostname == "lab-a"
    assert host.hostname_explicit is False


def test_match_block_is_ignored(tmp_path: Path) -> None:
    config = _write(
        tmp_path / "config",
        """
Match host lab-a
    User ignored

Host lab-a
    HostName 10.0.0.1
""",
    )

    host = SshConfigParser(config).parse().hosts[0]

    assert host.user is None


def test_missing_config_file_is_not_an_error(tmp_path: Path) -> None:
    parsed = SshConfigParser(tmp_path / "config").parse()

    assert parsed.exists is False
    assert parsed.hosts == []
    assert parsed.issues == []


def test_unclosed_quote_is_reported(tmp_path: Path) -> None:
    config = _write(tmp_path / "config", 'Host lab-a\n    User "labadmin\n')

    parsed = SshConfigParser(config).parse()

    assert [host.alias for host in parsed.hosts] == ["lab-a"]
    assert any("引号未闭合" in issue.message for issue in parsed.issues)


def test_split_config_line_handles_comment_and_quotes() -> None:
    assert split_config_line("Host lab-a # comment") == ["Host", "lab-a"]
    assert split_config_line('User "lab admin"') == ["User", "lab admin"]
    assert split_config_line("Port=2222") == ["Port", "2222"]
    assert split_config_line("   ") == []
