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

### 4.3 阶段三：资源观测与磁盘空间管理（A–D 已完成）

- 磁盘空间结果展示当前目录占用前 10 项与比例条，按已分配空间降序，支持目录下钻；不完整结果明确标注占比仅基于已采集数据。

详细范围、sudo 矩阵、组件评估与测试场景见 [资源观测与磁盘空间管理规划](resource-observability-plan.md)，
接口契约见 [api.md §8](api.md)。

- [x] A：CPU/内存/Swap、磁盘 I/O、网络 I/O 短周期趋势；返回采样间隔、远端/本机时间、累计计数与派生速率；
  计数器回绕/间隔过短时返回 `reset` 与 `null` 速率，不编造 0。
- [x] B：GPU 设备详情（PCI、驱动、显存、利用率、温度、功耗、时钟、MIG）与进程表（PID/USER/命令/程序位置/
  启动时间），`processesStatus` 区分 `ok`/`partial`/`unavailable`；不向前端暴露 `nvidia-smi` 原始文本。
- [x] C：挂载点 allowlist、后台扫描任务、SSE 进度与节点分块、取消、应用重启标记 `interrupted`；
  one-file-system、符号链接策略、硬链接 inode 去重、稀疏文件 `allocatedBytes`/`apparentBytes`、
  权限错误 `incomplete` 传播、deleted-open 文件、min size/uid/扩展名/时间过滤、treemap 与目录/最大文件/UID 汇总。
- [x] D：可登录用户、主组/附加组、home 摘要、sudo 三态（`组成员`/`策略存在`/`有效权限`），
  `unknown` 不折算为 `false`；按 UID 的空间扫描复用 C 的扫描管线。
- [ ] 逐目录的实时进度（当前为阶段式进度：queued → scanning → aggregating → chunking → done）。

证据：`uv run pytest`（275 passed，1 skipped）覆盖指标差分与回绕、GPU 缺失/多卡/MIG/不可见进程、
存储树聚合与硬链接/稀疏/过滤、用户与 sudo 三态、扫描允许清单/取消/并发限制/重启恢复/SSE；
前端 `npm test`（46 passed）覆盖五个页签、图表降级、GPU 进程过滤排序、扫描进度/取消/treemap、sudo 三态；
界面截图见 `docs/screenshots/06-metrics.png`、`07-gpu.png`、`08-storage.png`、`09-users.png`。

### 4.4 阶段四：账户生命周期（新建用户 / 目录迁移 / 新空间）

详细需求见 [账户生命周期](account-lifecycle.md)，接口契约见 [api.md §9](api.md)，
安全边界见 [security.md §9](security.md)。

- [x] 随机密码只用 `secrets.SystemRandom`（`os.urandom`）；可设置长度、字符类别与符号集合，
  可排除易混淆字符；规则冲突返回字段级 422；PAM 更严格时自动提高并列出原因。
- [x] 读取**实际生效**的 PAM/pwquality 配置（PAM 行内参数优先），能力检测覆盖
  `pwquality.conf`、PAM 模块行、`pwscore`/`chage`/`useradd`/`rsync` 与 drop-in 目录；
  能力缺失时整体只读。
- [x] 策略写入先展示 unified diff 并二次确认，备份到 `/var/backups/labopspanel/`，
  写入后重新读取比对 + `pwscore` 行为验证，失败自动恢复；支持按记录恢复修改前配置。
- [x] 不覆盖无法识别的手工 PAM 配置：未知键与注释原样保留，有歧义的 PAM 行该字段只读。
- [x] 批量新建用户（≤50）：逐个执行并逐个记录结果；密码经 0600 临时文件 + `chpasswd`
  写入，不进入 argv；生成密码只存在内存并一次性下发，不落库、不入审计。
- [x] 目录迁移 9 阶段状态机：预检 / 空间 / 冻结 / 改名 / rsync / 校验 / 切换 / 恢复 / 观察；
  只允许本地 ext4；冻结后确认无残留进程；失败保留原目录且不建链接。
- [x] 迁移中断可识别阶段并 `resume`（先核对远端状态）或 `rollback`；
  功能验证失败自动回退；清理入口只在成功观察阶段可用，且复核任务 ID、备份路径、
  目标路径与链接关系。
- [x] 新空间分配：保存 `dataRoot`/`migrationRoot`，创建 `0750` 目录并建立 home 下符号链接，
  以该用户身份验证可写；解除链接只删除链接，不删除数据。
- [x] 前端新增「账户」页签：两层密码说明、每处提权操作标注「需要 sudo」、sudo 密码录入卡片、
  脱敏示例与规则检查、策略 diff 确认、批次结果与一次性密码、迁移阶段与回退/清理入口。
- [ ] SSH 公钥管理与跨主机批量变更（仍不在首版范围）。

证据：`uv run pytest`（352 passed，1 skipped）覆盖密码生成与下限合并、PAM/pwquality 解析与
最小化改写、策略备份/验证/恢复、批量建用户（含“密码不进入 argv”）、迁移 9 阶段与
中断/恢复/回退/清理、空间分配与错误码；前端 `npm test`（51 passed）覆盖账户页签的两层密码、
脱敏示例、策略 diff 确认、批次密码一次性展示与预检阻断。

### 4.5 复验命令

```powershell
uv sync --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
cd frontend; npm ci; npm run typecheck; npm test; npm run build
uv run labopspanel   # http://127.0.0.1:8765
```

### 本次管理流程与布局改进

- 新空间分配页面直接展示已挂载设备、路径、文件系统、容量与可用空间，支持选择可写本地 ext4；无需先完成文件扫描。
- 账户生命周期 sudo 操作必须将完整脚本作为单个 shell 参数执行，确保多行脚本及内建命令均在提权上下文运行。
- 各子页面提供右侧页内索引，支持分区跳转、当前阅读位置和返回顶部；窄屏切换为顶部索引。异步加载及主机标签切换后同步更新目录，改善卡片标题、表单间距和表格可读性。
