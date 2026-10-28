"""把数据库中的主机记录转换为 SSH 目标。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lab_ops_panel.ssh.transport import SshTarget

if TYPE_CHECKING:
    from lab_ops_panel.models import Server


def target_from_server(server: Server) -> SshTarget:
    """导入自 SSH 配置的主机只传别名，让 OpenSSH 解析 Include/ProxyJump 等配置。"""

    return SshTarget(
        alias=server.alias,
        hostname=server.hostname,
        user=server.user,
        port=server.port,
        identity_file=server.identity_file,
        proxy_jump=server.proxy_jump,
        proxy_command=server.proxy_command,
        connect_timeout=server.connect_timeout,
        use_config_alias=server.source == "ssh_config",
    )
