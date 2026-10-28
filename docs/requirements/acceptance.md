# 验收标准与实施阶段

## 1. 分阶段交付

### 阶段一：主机清单与只读状态

- 能从 Windows OpenSSH 配置导入显式 Host。
- 能正确处理 HostName、User、Port、IdentityFile、Include 和 ProxyJump。
- 能测试连接并展示失败原因、目标主机和最近时间。
- 能查看系统信息、CPU、内存、磁盘、网络、GPU 和 systemd 服务状态。
- SQLite、Alembic、DPAPI 凭据存储和本机回环监听完成。

### 阶段二：维护任务

- 能查看服务列表、日志，并执行服务启停、重启、启用和禁用。
- 长任务显示队列、进度、实时日志、退出码、超时和失败原因。
- 任务可取消；应用重启后运行中任务标记为 `interrupted`。
- 高风险动作有预检、目标确认、sudo 确认和幂等键。

### 阶段三：趋势与扩展

- 增加短周期资源趋势和 GPU 进程视图。
- 增加受控命令、批量任务、账户同步和 SSH 公钥管理前，先补充需求与安全验收。

## 2. 测试场景

后端必须覆盖：

- SSH 配置解析、Include、ProxyJump、无效配置和重复导入。
- OpenSSH 连接成功、认证失败、主机不可达、超时、取消和非零退出。
- DPAPI 加密/解密、错误密文、密码清除和数据库权限。
- 任务状态迁移、幂等重复提交、应用重启恢复和输出截断。
- systemd 服务保护、无权限查询、服务不存在和动作失败。
- GPU 缺失、指标不支持和远端探针返回异常。

前端必须覆盖主机导入、状态卡片、错误提示、任务进度、取消操作和非实时缓存标记。测试不得操作真实账户、磁盘、sudoers、SSH 私钥或实验室生产主机。

## 3. 完成定义

一个阶段只有在代码、测试、文档和验收条目同时完成后才算结束。每次新增远程写操作，都必须说明参数边界、权限要求、失败恢复方式、日志内容和禁止目标。

## 4. 首版实施状态

### 4.1 阶段一：主机清单与只读状态（已完成）

- [x] 从 Windows OpenSSH 配置导入显式 Host，正确处理 HostName、User、Port、IdentityFile、Include、ProxyJump、ProxyCommand 与 ConnectTimeout。
- [x] 通配 `Host` 按 OpenSSH“首次取值获胜”作为默认值；`Match` 块跳过并记录说明。
- [x] 导入幂等（重复导入只更新）、可只导入指定别名、`pruneMissing` 只停用不删除。
- [x] 连接测试展示成功/失败原因、目标主机与最近时间，并持久化 `lastConnect`。
- [x] 主机详情展示系统信息、CPU、内存、磁盘、网络、GPU 与 systemd 服务；分区级状态降级，GPU 缺失显示 `N/A`。
- [x] SQLite（WAL + 外键）、Alembic 初始迁移、DPAPI 凭据存储、仅回环监听。

证据：`uv run pytest`（182 passed，1 skipped），`docs/screenshots/01-overview.png`、`02-host-detail.png`、`05-host-detail-error.png`。

### 4.2 阶段二：维护任务（已完成）

- [x] 固定动作白名单：主机检查、采集快照、日志查询、服务启动/停止/重启/启用/禁用；任意命令入口默认关闭。
- [x] 任务队列、步骤进度、实时日志（SSE）、退出码、超时与失败原因。
- [x] 任务可取消（排队中直接取消、运行中终止远端进程）；应用重启后遗留任务标记 `interrupted`。
- [x] 高风险动作具备预检、目标确认、sudo 确认、幂等键与输出上限；受保护服务禁止 stop/disable。
- [x] 审计只记录动作、目标、时间、结果与错误摘要。

证据：`docs/screenshots/03-tasks.png`；端到端演示中 `service_restart` 任务三步全部 `ok`、SSE 收到 `done`、
重复幂等键返回 409、`ssh.service` stop 返回 409、审计出现 `service_restart/succeeded`。

### 4.3 阶段三：趋势与扩展（未开始）

- [ ] 短周期资源趋势与 GPU 进程视图。
- [ ] 受控命令之外的批量任务、账户同步、SSH 公钥管理（需先补需求与安全验收）。

首版有意不实现以上内容；`run_command` 仅在 `LABOPS_ALLOW_ARBITRARY_COMMANDS=true` 时开放，
仍受 Bash 语法检查、确认、超时与输出截断约束。

### 4.4 复验命令

```powershell
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
cd frontend; npm ci; npm run typecheck; npm test; npm run build
uv run labopspanel   # http://127.0.0.1:8765
```
