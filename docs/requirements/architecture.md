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

- 后端 182 个用例覆盖解析、传输、DPAPI、探针、任务、迁移与 API；Windows 之外跳过 DPAPI 用例。
- 传输层用 Windows 批处理模拟 ssh 客户端验证超时、取消、截断与 stdin；其余用例使用 `FakeTransport`。
- 前端 20 个用例覆盖 API 错误映射、主机卡片、分区降级、确认对话框、任务取消与 SSE、密码设置/清除。
