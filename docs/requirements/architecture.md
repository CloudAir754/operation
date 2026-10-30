# 架构与实现要求

## 1. 总体架构

开发模式下，浏览器访问 Vite 前端 `http://127.0.0.1:55173`；Vite 将 `/api` 和 SSE 请求代理到后端 `http://127.0.0.1:8765`。前端开发端口固定且启用严格端口模式，端口占用时直接报错，避免自动切换端口造成访问地址不一致。

```text
浏览器
  │ http://127.0.0.1:55173
  ▼
Vite 开发服务器
  │ /api 代理
  ▼
FastAPI 本地服务 http://127.0.0.1:8765
```

```text
浏览器
  │ HTTP + Server-Sent Events
  ▼
FastAPI 本地服务
  ├── SSH 配置解析器
  ├── SSH 传输适配器
  ├── 远端探针与固定动作
  ├── 后台任务管理器
  ├── DPAPI 凭据存储
  └── SQLite / SQLAlchemy / Alembic
```

前端使用 React、TypeScript 和 Vite；后端使用 Python 3.12、FastAPI、SQLAlchemy 和 Alembic；依赖使用 `uv` 管理。前端通过 REST API 获取快照，通过 SSE 订阅长任务进度。

## 2. SSH 连接

SSH 配置导入器读取 `%USERPROFILE%\\.ssh\\config`，支持显式 `Host`、`Hostname`、`User`、`Port`、`IdentityFile`、`Include`、`ProxyJump`、`ProxyCommand` 和连接超时配置。服务器以 Host 别名作为稳定标识。

默认传输后端调用 Windows OpenSSH，以保持现有配置行为；代码保留传输接口，后续可为不含高级配置的直连场景增加 Paramiko 后端。连接层必须提供：连接测试、命令执行、标准输出/错误、退出码、超时、取消和非零退出诊断。

远端不安装 LabOpsPanel。探针以一次性 Python/Bash 输入执行，返回 JSON；输出必须有大小上限，命令不能依赖交互式终端。

## 3. 本地数据模型

- `servers`：Host 别名、解析配置摘要、启用状态、备注和最近连接结果。
- `server_secrets`：DPAPI 密文、密文版本和更新时间。
- `server_snapshots`：主机信息、资源指标、服务摘要和采集时间。
- `tasks` / `task_steps`：任务类型、目标主机、状态、进度、输出摘要、错误和恢复信息。
- `audit_logs`：动作、目标、时间、操作者模式和结果摘要。

系统状态以远程事实为准；SQLite 缓存过期后必须标注“非实时”。

## 4. 任务与远程动作

短查询可以同步返回；服务操作和命令执行统一进入后台任务。任务状态至少包括 `queued`、`running`、`succeeded`、`failed`、`cancelled` 和 `interrupted`。应用重启后，未完成任务必须标为中断，不得伪造成功或自动重试高风险动作。

首版固定动作包括主机检查、资源采集、服务列表、日志查询、服务启动/停止/重启/启用/禁用。任意命令入口默认关闭；若启用，必须先做 Bash 语法检查、风险展示、sudo 确认、超时控制和输出截断。

## 5. 采集策略

主机详情打开时立即采集，并在 15–30 秒内复用缓存。页面可见期间按需刷新，不能后台无限轮询所有主机。CPU、内存、磁盘和网络缺少权限时返回字段级状态；NVIDIA 驱动或指标不支持时显示 `N/A`，不得伪造为零。

## 6. 首版实现落地

### 6.1 代码结构

```text
src/lab_ops_panel/
  app.py            FastAPI 装配、异常映射、SPA 静态托管
  cli.py            labopspanel 入口（uvicorn）
  config.py         Settings（LABOPS_* 环境变量、回环绑定校验）
  db.py             SQLite 引擎（WAL、外键、busy_timeout）与会话工厂
  models.py         servers / server_secrets / server_snapshots / tasks / task_steps / audit_logs
  migrations/       Alembic 环境与 0001 初始迁移
  ssh/              config_parser.py（Include/通配/ProxyJump）、transport.py（OpenSSH 子进程）
  remote/           probe.py（一次性 Python 探针）、parsing.py、actions.py、services.py、diagnosis.py
  tasks/            manager.py（线程池 + 取消 + 恢复）、events.py（SSE 事件总线）
  snapshots.py      快照缓存、服务列表、日志读取
  secrets_store.py  DPAPI 保护器与 sudo 密码读写
  api/              deps.py、system.py、servers.py、snapshots.py、tasks.py、audit.py
```

### 6.2 SSH 连接

- 导入自 `ssh_config` 的主机只把 **Host 别名** 交给 `ssh -F <config> <alias>`，由 OpenSSH 自己解析
  `Include`、`ProxyJump`、`ProxyCommand`、`IdentityFile`，避免二次解释导致行为漂移。
- 固定选项：`BatchMode=yes`、`NumberOfPasswordPrompts=0`、`StrictHostKeyChecking=accept-new`
  （接受首次出现的主机密钥，密钥变化仍然失败，不做 `no` 式静默降级）、`ConnectTimeout`。
- 传输接口 `SshTransport.run(target, command, timeout, cancel_event, stdin_data, max_output_bytes)`：
  子进程 + 读取线程，支持超时/取消/输出上限；`sudo` 密码仅通过 stdin 传递，永不进入 argv 或日志。
- 失败诊断把 OpenSSH 常见错误（认证、主机密钥、DNS、拒绝连接、超时、systemd 缺失）翻译成中文提示。

### 6.3 探针与输出

- 探针脚本以 base64 传入远端 `python3`（回退 PATH 中的 `python3`），一次性执行并输出单个 JSON。
- 每个分区（system/cpu/memory/disks/network/gpu/services）都有 `status`、`error`、`data`；
  缺权限返回 `permission_denied`，无 NVIDIA 驱动返回 `unavailable`，解析失败返回 `error`。
- 探针协议带 `probeVersion`；版本不符、输出截断或非 JSON 都视为采集失败，并给出原因。
- 输出上限按流截断（默认 256 KiB），任务输出尾部最多保留 4000 字符。

### 6.4 任务

- `TaskManager` 使用固定大小线程池；任务状态迁移受“终态不可覆盖”保护。
- 提交时完成参数校验、受保护服务检查、sudo 可用性检查、幂等键去重与审计前置。
- 执行阶段逐步落库步骤记录，并向 SSE 总线发布 `status`/`step`/`log`/`done` 事件。
- 取消通过 `threading.Event` 传到传输层，远端进程被终止后任务标记 `cancelled`。
- 启动时把遗留的 `queued`/`running` 任务改为 `interrupted`，运行中的步骤标记 `failed`，绝不自动重试。

### 6.5 缓存与数据

- 快照落在 `server_snapshots`，默认 20 秒内复用；过期数据仍可返回但必须带 `stale=true`。
  每个主机最多保留 50 份快照，写入后清理旧记录。
- 服务列表在缓存有效期内直接来自最近快照，避免额外远端调用；日志查询实时执行。
- SDK 时间统一以 UTC 落库（自定义 `UTCDateTime`），读取时始终带时区，避免过期判断出错。

### 6.6 前端

- React 18 + TypeScript + Vite；`usePolling` 负责可中止的轮询，`useTaskEvents` 负责 SSE 与清理。
- 每页都有显式错误横幅、空状态和加载态；快照分区降级、`N/A`、`非实时缓存` 徽标均按契约渲染。
- 危险动作必须经过确认对话框（目标主机、服务单元、风险等级、sudo 要求），并附带 UUID 幂等键。
- 生产构建由后端在 `/` 提供，SPA 路由回退到 `dist/index.html`；`/api/*` 不会被回退吞掉。

### 6.7 测试

- 后端 275 个用例（含阶段三）覆盖解析、传输、DPAPI、探针、任务、指标、GPU、用户、扫描、迁移与 API；
  Windows 之外跳过 DPAPI 用例。
- 传输层用 Windows 批处理模拟 ssh 客户端验证超时、取消、截断与 stdin；其余用例使用 `FakeTransport` 与
  在临时目录中执行的真实探针源码。
- 前端 46 个用例覆盖 API 错误映射、主机卡片、分区降级、确认对话框、任务取消与 SSE、密码设置/清除，
  以及五个观测页签、图表降级、GPU 过滤排序、扫描进度/取消/treemap 与 sudo 三态。

## 7. 阶段三：资源观测实现

### 7.1 探针模式

同一份内嵌 Python 探针按模式执行，所有模式都返回 `probeVersion`、`collectedAt` 与
带 `status/error/requiresSudo/data` 的分区：

| 模式 | 用途 | 主要来源 | 上限 |
| --- | --- | --- | --- |
| `metrics` | 累计计数与静态信息 | `/proc/stat`、`meminfo`、`vmstat`、`diskstats`、`net/dev`、sysfs | 200 设备 / 200 接口 |
| `gpu` | GPU 设备与进程 | `nvidia-smi` 结构化查询、`/proc/<pid>` | 32 卡 / 200 进程 |
| `users` | 用户、sudo 三态、home 摘要 | `passwd`、`group`、`sudoers`、home 遍历 | 500 用户 / 2 万条目 / 2 秒 |
| `mounts` | 挂载点 allowlist | `/proc/self/mounts`、`/dev/disk/by-uuid` | 40 |
| `storage` | 有界目录树扫描 | `scandir` + `lstat`、`/proc/*/fd` | 1200 节点 / 深度 8 |

探针参数按 shell 引号规则拼接，拒绝控制字符；`storage` 只接受挂载点清单中的精确路径。

探针源码通过 SSH **标准输入** 传给远端 `python3 -`，argv 里只有模式与简短参数（通常 < 200 字符）。
Windows 的 `CreateProcess` 命令行上限约 32767 字符，早期把 base64 探针放进 argv 会触发
`WinError 206`；传输层现在把源码写入独立线程的 stdin 管道，超时/取消照常生效，
并把「命令行过长」与「找不到 ssh」区分成不同诊断信息。

固定动作「采集状态」走同一条通道：`PlanStep.stdin_data` 携带探针源码，步骤本身不带
sudo，避免与 `sudo -S` 的密码 stdin 冲突；既不需要密码也不需要负载的步骤，传输层
立即关闭 stdin 管道（等价于 `ssh -n`），否则 Windows 上远端读取标准输入的命令会挂到超时。

### 7.2 指标模型

- 每次 `GET /metrics` 触发一次远端采样；服务端按主机保存环形缓冲（默认 1 小时、最多 2400 点）。
- 曲线点同时携带远端 `collectedAt` 与本机 `receivedAt`；采样间隔按本机接收时间计算。
- 累计计数差分在本地完成：计数器回绕、间隔 < 200 ms、缺少基线时返回 `reset` 与 `null` 速率，
  绝不写成 0；回绕后下一采样重新建立基线。
- CPU 百分比由 `/proc/stat` 增量计算（user/system/iowait/steal/idle），总利用率 = 100 - idle；
  内存包含 buffers/cached/commit/swap 与 swap in/out 速率；磁盘按 512 字节扇区换算并计算
  IOPS/await/util；网络按接口区分 `physical/bond/bridge/vlan/virtual/loopback`。
- 最后成功采样超过 3 个采样间隔或最近一次采集失败时，响应 `stale=true`。

### 7.3 GPU

- 首选 `nvidia-smi --query-gpu` 的结构化字段（含 PCI、显存、利用率、温度、功耗、时钟、MIG、compute mode）；
  扩展字段不受支持时自动回退到核心字段，缺失值保持 `null`。
- 进程通过 `--query-compute-apps` 获取，并用 `/proc` 补齐 USER、命令行、可执行文件、工作目录与启动时间；
  进程不可见或已退出时标记 `stale`，`processesStatus` 降级为 `partial`。
- `capabilities.perProcessUtilization=false`：`nvidia-smi` 不提供进程级利用率，前端显示 `N/A`。
- 不回传 `nvidia-smi` 原始文本。

### 7.4 空间扫描

```text
POST /storage/scans
  → 校验主机启用 / 挂载点 allowlist / 并发上限
  → 写入 storage_scans（queued）并提交线程池
  → running: 探针 lstat 遍历（one-file-system、符号链接策略、硬链接去重、deleted-open）
  → 本地构建聚合树（硬链接不计重复、稀疏文件双口径、incomplete 传播、过滤与排序）
  → 持久化结果 JSON（最多 3000 节点）+ 分块 SSE（200/块）+ done
  → 审计、按主机保留最近 50 次扫描
```

- `storage_scans` 表由 Alembic `0002_storage_scans` 创建；应用启动时把 `queued/running` 标记为
  `interrupted`，不自动重试。
- 取消通过 `threading.Event` 传入传输层并终止本机 ssh 进程；排队中的扫描直接置为 `cancelled`。
- 扫描路径只能来自 `GET /mounts` 的 allowlist；`children` 接口再次校验路径必须位于扫描挂载点内。
- 权限错误、达到深度上限或节点上限都会使父目录 `incomplete=true`，并保留错误文本。

### 7.5 用户与 sudo 三态

- 只读取 `passwd`/`group`/`sudoers` 的派生结果；不读取 `/etc/shadow`，不回传原始规则内容。
- `sudoGroupMember`（sudo/wheel 组成员）、`sudoPolicyPresent`（sudoers 可达时匹配）、
  `sudoEffective`（仅对当前 SSH 用户执行 `sudo -n true`）互相独立；无法判定时保留 `null`/`unknown`。
- home 摘要有界（深度 2、2 万条目、2 秒），超限标记 `truncated`，不可读标记 `incomplete`。
- 全盘归属复用 C 阶段的扫描管线（`userFilter=uid`），不单独实现第二套遍历。

### 7.6 前端结构

- 主机详情页拆分为「概览 / 资源 / GPU / 磁盘空间 / 用户 / 账户」六个页签，`?tab=` 深链接，
  仅激活页签拉取数据。
- 资源/GPU 趋势每 2 秒轮询，页面隐藏或切走页签时停止；图表为纯 SVG（无第三方图表依赖）。
- 空间扫描通过 SSE 接收进度与节点分块，完成后以 `GET /storage/scans/{id}` 的结果为准；
  取消、失败、`interrupted` 均有明确状态。

## 8. 阶段四：账户生命周期实现

### 8.1 模块

```text
src/lab_ops_panel/
  passwords.py                规则模型、PAM 下限合并、secrets 生成与自检、脱敏示例
  remote/password_policy.py   pwquality.conf/PAM 解析、实际生效值、最小化改写、unified diff
  remote/accounts.py          登录名/路径/UID 校验与固定命令模板（useradd、rsync、mv、ln…）
  provisioning.py             ProvisioningService：策略、批次、迁移状态机、空间分配
  schemas_accounts.py         阶段四 API 模型（请求拒绝未知字段，响应不含明文密码）
  api/accounts.py             22 个路由（含两个 SSE 端点）
  migrations/versions/0003_account_lifecycle.py
```

新增表：`app_settings`（密码生成规则）、`password_policy_changes`、`account_batches`、
`home_migrations`、`dataset_allocations`；`servers` 增加 `data_root` 与 `migration_root`。

### 8.2 探针扩展（`PROBE_VERSION = 3`）

| 模式 | 用途 | 数据来源 |
| --- | --- | --- |
| `password_policy` | 读取实际生效的密码策略与能力 | `pwquality.conf`、`pwquality.conf.d/*`、`/etc/pam.d/common-{password,auth}`、`login.defs`、`which` |
| `account_info` | 单个账户的 UID/GID/home/shell/密码状态 | `pwd`、`passwd -S` |
| `home_inspect` | 迁移预检（源、目标、子挂载、统计、冲突） | `lstat`/`statvfs`/`/proc/self/mounts`/有界遍历 |
| `tree_stats` | 单路径文件数/目录数/逻辑大小/实际占用/inode | 有界遍历（默认 120 秒、200 万条目） |
| `dataset_inspect` | 空间分配预检（账户、数据根、目标目录、链接状态） | 同上 |

所有探针都是只读的；写操作由固定命令模板在 `ProvisioningService` 中执行。

### 8.3 迁移状态机

`preflight → space_check → freeze → rename → copy → verify → switch → restore → observe`。

- 阶段写库后执行，异常统一落为 `failed` + 当前 `stage`；应用重启把 `queued/running` 标为
  `interrupted` 并保留阶段与备份信息。
- `resume` 先用 `home_inspect` 核对远端状态（源是否已是链接、备份/目标是否存在），
  再选择 `preflight`/`copy`/`verify`/`restore` 之一继续；无法判定时拒绝而不是猜测。
- `rollback` 与 `cleanup` 都是显式入口，带二次确认与路径复核；清理只在
  `status=succeeded` 且 `stage=observe` 时开放。
- `restore` 阶段的功能验证失败会触发自动回退，并把结果写入 `result.autoRollback`。

### 8.4 前端

- 新增 `frontend/src/components/host/accounts/`：`AccountsTab`（两层密码说明 + sudo 卡片）、
  `GeneratorCard`（规则编辑、脱敏示例、一致性检查）、`PolicyCard`（实际生效值、diff 确认、
  修改历史与恢复）、`UsersCard`（批量表单、结果、一次性密码弹窗）、`MigrationCard`
  （预检、阶段时间线、继续/回退/清理）、`DatasetCard`（默认路径、分配、解除链接）。
- 所有提权按钮统一使用 `SudoBadge`（「需要 sudo」），未保存 sudo 密码时给出明确提示。
- 账户页签只在激活时拉取数据，符合“仅激活页签发请求”的既有约束。
