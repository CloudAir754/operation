# LabOpsPanel 需求文档

## 1. 产品目标

LabOpsPanel 是运行在 Windows 管理主机上的本地 Web 控制台。实验室管理员通过浏览器查看多台 Linux 机器的状态并执行常见维护操作，不需要为每台服务器安装常驻 Agent，也不需要编写远程脚本。

首版只监听 `127.0.0.1`，通过本机 SSH 配置连接服务器。远程主机清单来自 `%USERPROFILE%\\.ssh\\config`；每台主机可以单独保存 sudo 密码。

## 2. 目标用户与边界

目标用户是实验室管理员，第一版采用本机免登录、单管理员模式。远端主机必须已经能够通过 SSH 登录，并具备 `/usr/bin/python3`、Bash 或常规 Linux 工具。

首版范围包括：

- SSH 配置导入、连接测试和主机清单维护。
- 主机信息、CPU、内存、磁盘、网络、GPU 和服务状态查看。
- systemd 服务启动、停止、重启、启用、禁用和日志查看。
- 结构化维护动作、受控命令、后台任务、实时日志和失败记录。
- 每台主机独立的 sudo 密码维护。
- 资源观测：CPU/内存/Swap、磁盘 I/O、网络 I/O 短周期趋势，GPU 设备与进程，磁盘空间树（受挂载点 allowlist 约束），可登录用户与 sudo 三态。
- 账户生命周期：批量新建用户与随机密码、Ubuntu 密码策略（PAM/pwquality）读写、用户目录迁移与新空间分配。

暂不包含多用户 RBAC、远端常驻服务、磁盘格式化、任意 root Web Shell、自动账户删除、跨主机批量变更、
AMD/Intel GPU 适配器与远端常驻指标采集、SSH 公钥管理，以及密码最长/最短使用天数与周期性到期。
后续功能必须先补充本目录中的需求和验收条目。

## 3. 页面与操作流程

页面分为：

1. **主机总览**：连接状态、最近采集时间、风险提示和任务摘要。
2. **主机详情**：概览、资源趋势、GPU 进程、磁盘空间树、用户、账户六个页签，包含系统信息、磁盘、网络、服务和日志。
3. **维护任务**：动作参数、目标确认、执行进度、实时输出、结果和失败原因。
4. **主机设置**：SSH 配置导入、启用状态、备注、sudo 密码更新和连接测试。

典型流程是“导入 SSH Host → 测试连接 → 查看状态 → 选择预定义维护动作 → 预检与确认 → 后台执行 → 查看结果”。
账户操作是“录入运维 sudo 密码 → 读取实际密码策略 → 批量建用户 / 迁移目录 / 分配空间 → 观察阶段 → 回退或清理”。

## 4. 安全要求

- 默认只绑定回环地址，禁止未经认证直接监听局域网地址。
- SQLite 存储在 Windows 用户应用数据目录，不写入源码目录。
- sudo 密码使用 Windows DPAPI 加密后保存，不保存明文密码、私钥或完整敏感输出。
- 所有远程写操作使用结构化参数、固定动作白名单、超时、输出上限和二次确认。
- SSH 连接复用 OpenSSH 的 `IdentityFile`、`Include`、`ProxyJump` 和 `ProxyCommand`；导入失败时显示具体原因，不静默降级。
- 日志只保存动作、目标、时间、状态和错误摘要，不记录凭据。

## 5. 相关文档

- [资源观测与磁盘空间管理规划](resource-observability-plan.md)
- [账户生命周期：新建用户、目录迁移与新空间分配](account-lifecycle.md)
- [架构与实现要求](architecture.md)
- [API 契约](api.md)
- [安全设计与残余风险](security.md)
- [验收标准与实施状态](acceptance.md)

## 6. 开发命令

| 命令 | 说明 |
| --- | --- |
| `uv sync --extra dev` | 安装/更新后端依赖 |
| `uv run uvicorn lab_ops_panel.app:create_app --factory --reload` | 开发模式后端 |
| `uv run labopspanel` | 启动本地服务（默认 127.0.0.1:8765） |
| `uv run pytest` | 后端测试 |
| `uv run ruff check .` / `uv run ruff format --check .` | Lint 与格式校验 |
| `uv run alembic upgrade head` | 手动执行数据库迁移（启动时自动执行） |
| `cd frontend; npm ci` | 安装前端依赖 |
| `cd frontend; npm run dev` | Vite 开发服务器（`/api` 代理到 8765） |
| `cd frontend; npm run typecheck` / `npm test` / `npm run build` | 类型检查 / 单测 / 生产构建 |
