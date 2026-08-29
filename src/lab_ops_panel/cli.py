"""本地启动入口。"""

from __future__ import annotations

import argparse
import socket
import threading
import webbrowser
from pathlib import Path

import uvicorn

from lab_ops_panel.app import create_app
from lab_ops_panel.config import Settings


def _available_port(host: str) -> int:
    """让操作系统选择当前可用的回环端口。"""

    with socket.socket() as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def main() -> None:
    """验证参数、显示令牌化 URL 并启动本地服务。"""

    parser = argparse.ArgumentParser(description="启动 LabOpsPanel 本地运维面板")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=0, type=int)
    parser.add_argument("--data-dir", type=Path, help="用户级 SQLite 数据目录")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    settings = Settings(host=args.host, port=args.port)
    if args.data_dir is not None:
        settings.data_dir = args.data_dir
    settings.validate()
    port = settings.port or _available_port(settings.host)
    url = f"http://{settings.host}:{port}/#token={settings.session_token}"
    print(f"LabOpsPanel 已启动：{url}")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    uvicorn.run(create_app(settings), host=settings.host, port=port)


if __name__ == "__main__":
    main()
