"""命令行入口：``uv run labopspanel``。"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from lab_ops_panel import __version__
from lab_ops_panel.config import LOOPBACK_HOSTS, Settings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="labopspanel", description="LabOpsPanel 本地 Web 控制台")
    parser.add_argument("--host", default=None, help="监听地址，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认 8765")
    parser.add_argument("--reload", action="store_true", help="开发模式自动重载")
    parser.add_argument("--version", action="version", version=f"LabOpsPanel {__version__}")
    args = parser.parse_args(argv)

    settings = Settings()
    host = args.host or settings.bind_host
    port = args.port or settings.bind_port
    if host.lower() not in LOOPBACK_HOSTS and not settings.allow_remote_bind:
        print(
            "拒绝启动：默认只允许绑定回环地址；如确需对外监听，请设置 "
            "LABOPS_ALLOW_REMOTE_BIND=true",
            file=sys.stderr,
        )
        return 2

    uvicorn.run(
        "lab_ops_panel.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
