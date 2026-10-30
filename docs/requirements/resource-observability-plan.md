# 资源观测与磁盘空间管理功能规划

## 1. 目标与边界

本规划扩展主机详情页，提供 GPU 进程定位、CPU/内存/Swap/磁盘 I/O/网络 I/O 短周期趋势、物理磁盘空间树和可登录用户空间归属。继续通过 SSH 一次性探针采集，不在远端安装常驻 Agent。曲线只在页面打开期间采集，默认保留最近 1 小时内存环形数据。

占用率必须注明口径：GPU 利用率是采样窗口平均值，显存是当前 used/total，磁盘和网络是由累计计数差分得到的速率。

## 2. 权限与降级矩阵

| 能力 | 普通 SSH 用户 | sudo/额外能力 | 降级结果 |
| --- | --- | --- | --- |
| GPU 总利用率、显存、温度、功耗 | 通常可用，取决于驱动设备节点 | 一般不需要 | 不可用时返回 unavailable |
| GPU 进程 PID、USER、显存、程序路径 | NVIDIA 通常可用；可能受 hidepid、容器和驱动策略影响 | 可能需要 sudo | 只显示可见进程并标记 partial |
| CPU、内存、Swap 总量和曲线 | 通常可用，读取 /proc | 不需要 | 字段级降级 |
| 其他用户的进程 CPU/内存/Swap | 可能需要 sudo 读取 /proc/PID | 推荐受限 sudo | 只显示可见进程 |
| 设备级磁盘 I/O、网卡 I/O | 通常可用，读取 /proc 和 sysfs | 不需要 | 保留设备/接口级数据 |
| 其他用户的进程磁盘 I/O | 可能需要 sudo 读取 /proc/PID/io | 推荐受限 sudo | 只显示可见进程 |
| 文件系统容量和挂载点 | 通常可用 | 不需要 | 显示挂载点，内容扫描可不完整 |
| / 全盘目录扫描、其他用户 home | 只能读取有权限路径 | 需要 sudo | 节点 permission_denied，汇总 incomplete |
| 可登录用户、组成员 | /etc/passwd、/etc/group 通常可读 | 完整 sudoers 验证可能需要 sudo | 无法验证时 sudoEffective=unknown |

sudo 仅用于固定的只读采集和扫描，不开放任意 root 命令。每个分区返回 status、error、requiresSudo、collectedAt；未知值禁止写成 0。

## 3. GPU 监控（接近 nvtop）

GPU 页签包括：

- 每张卡：型号、PCI 地址、驱动、温度、功耗、总/已用显存、显存使用率、GPU 利用率、编解码利用率、时钟和 MIG 实例。
- GPU 利用率与显存利用率双曲线，默认 5 分钟，可选 1 分钟、15 分钟、1 小时；默认 2 秒采样。
- 进程表：PID、USER、GPU/实例、进程类型、显存、GPU 利用率、命令名、完整程序位置、启动时间、工作目录（可取得时）。
- 按显存、利用率、用户、GPU 筛选和排序；采样竞争导致进程退出时显示 stale process。

首选 NVIDIA NVML 或 nvidia-smi 的结构化查询，不把 nvtop 安装为运行时依赖。后续通过适配器增加 AMD/Intel。验收覆盖无 GPU、驱动缺失、多 GPU、MIG、超时和不可见进程。

## 4. CPU、内存、Swap、磁盘 I/O、网络 I/O

指标如下：

- CPU：总利用率、user/system/iowait/steal、逻辑 CPU、load 1/5/15。
- 内存：total/available/used、buffers、cache、commit；Swap total/used/free、swap in/out。
- 磁盘：按物理块设备和分区显示 read/write bytes/s、IOPS、await、util%，区分设备和挂载点，隐藏重复的 loop/device-mapper 视图但可展开。
- 网络：按接口 RX/TX bytes/s、包速率、错误、丢包、链路状态和速率，标注物理、bond、bridge、VLAN、虚拟接口。

从 /proc/stat、/proc/meminfo、/proc/vmstat、/proc/diskstats、/proc/net/dev 和 sysfs 的两次快照计算差分。响应同时带 sampleIntervalMs、原始累计计数和派生速率。

前端提供 CPU/内存与 Swap、磁盘 I/O、网络 I/O、设备明细四组联动图表；异常点显示采集原因。首版不承诺进程级网络归属；该能力需要 eBPF 或 socket 观测，通常需要 root/CAP_BPF/CAP_NET_ADMIN。

## 5. 物理磁盘空间管理

SpaceSniffer 是 Windows 本地 GUI，不能直接用于远端 Linux。可评估 ncdu、gdu、dua 作为已安装时的可选加速器；首版以固定 Python 探针返回 JSON，避免依赖远端安装和工具输出差异。gdu 的 JSON 导出可作为后续导入树视图的参考。

扫描规则：

- 从 mountinfo 或 findmnt 建立挂载清单，以设备 UUID/major:minor、文件系统类型和挂载点标识物理文件系统。
- 默认 oneFileSystem=true，按 st_dev 不跨入其他物理文件系统；每个扫描任务绑定一个挂载点。
- 符号链接默认只计链接本身、不跟随目录链接；解析链接目标时检测循环和跨设备目标。
- 同时返回 allocatedBytes（st_blocks*512）和 apparentBytes；说明稀疏文件、压缩、reflink 和硬链接口径。
- 硬链接按 inode 去重，避免重复计数；删除但仍打开的文件作为独立类别，需要读取 /proc/PID/fd，跨用户通常需要 sudo。
- 不可读目录必须携带路径和权限错误，父节点标记 incomplete，不静默跳过。

交互采用 treemap + 可展开目录表，支持挂载点、深度、最小文件大小、用户、扩展名、最近修改时间过滤；扫描作为后台任务，分块推送结果和进度，可取消。路径只能来自挂载点清单或用户 home，禁止任意 root 路径。

## 6. 可登录用户与空间

默认筛选 shell 不在 nologin/false 黑名单且 UID 达到可配置阈值的用户，同时提供显示系统账号开关。展示 login、UID/GID、主目录、shell、主组/附加组、最近登录（可用时）、home 所在设备和空间占用。

sudo 拆成三态：sudoGroupMember（sudo/wheel 等组成员）、sudoPolicyPresent（sudoers/include 中存在匹配规则）、sudoEffective（非交互 sudo 验证结果）。组成员不能直接等同于有效 sudo；无法验证返回 unknown。不读取 /etc/shadow，不回传原始 sudoers 内容。

空间分为用户 home 扫描和按 UID 的全盘归属扫描。指标包括 allocatedBytes、apparentBytes、文件数、目录数、跨设备数量和 incomplete。共享硬链接显示共享归属；overlay、NFS、FUSE、quota 不支持时返回能力状态。未知路径不能计入任一用户。

## 7. 后端、API 和数据结构

建议新增 remote/observability.py、remote/storage_scan.py、remote/users.py。snapshots 保存最新汇总；曲线存内存环形缓冲，应用重启不承诺恢复。

- GET /api/servers/{id}/metrics?window=5m：资源当前点和曲线。
- GET /api/servers/{id}/gpus：GPU、MIG 和可见进程。
- POST /api/servers/{id}/storage/scans：创建扫描，参数包含 mount、oneFileSystem、followSymlinks、maxDepth、userFilter。
- GET /api/storage/scans/{taskId} 和 /events：扫描进度、分块树和错误。
- GET /api/servers/{id}/users?includeSystem=false：用户与 sudo 状态、home 摘要。
- POST /api/servers/{id}/users/storage-scan：创建按 UID 的全盘扫描。

新接口沿用 section status、stale、权限字段、输出上限、取消和审计；未知字段拒绝。

## 8. 分阶段交付

A：CPU/内存/Swap、设备 I/O、网络 I/O 曲线和权限降级。

B：NVIDIA GPU 汇总、显存/GPU 曲线、进程 USER/PID/命令/程序位置；随后增加 AMD/Intel 适配器。

C：挂载点发现、one-file-system、符号链接/硬链接/稀疏文件口径、分块扫描和 treemap；评估 gdu/ncdu/dua 可选加速。

D：可登录用户、home 空间、sudo 三态、按 UID 全盘归属、共享硬链接和 deleted-open 文件。

## 9. 测试与验收

必须覆盖 GPU 无驱动/MIG/多卡/超时/不可见进程，采样间隔变化和计数器回绕，设备热插拔和虚拟网卡，多挂载点、bind mount、符号链接循环、硬链接去重、稀疏文件、不可读目录、deleted-open 文件，passwd 筛选、sudo 三态、扫描取消/超时/输出分块和应用重启 interrupted。测试使用临时目录、loop 文件系统或 fake probe，不扫描真实根盘、不修改 sudoers、不安装远端软件。

## 10. 需求拆分、依赖与完成门槛

按 A→D 顺序交付；每个阶段都必须同时提交探针/适配器、API 契约、前端状态展示、自动化测试和本文件对应的验收证据。阶段之间共享统一的 `section status`、`stale`、`requiresSudo`、`collectedAt`、取消和输出上限约定，避免前端为每种指标实现不同的降级模型。

| 阶段 | 可评审产物 | 主要依赖 | 阶段完成门槛 |
| --- | --- | --- | --- |
| A | 资源快照探针、差分采样器、趋势 API 和四组图表 | 现有 SSH 探针、任务取消、主机详情页 | 计数器回绕/采样异常/权限不足均有可解释状态；前端能区分 stale 与 unavailable；测试覆盖 Linux fake 快照 |
| B | GPU 适配器、GPU 趋势和进程表 | A 的时间序列模型；NVIDIA 驱动测试夹具 | 无 GPU、驱动缺失、多卡、MIG、超时和不可见进程均能返回结构化结果；不把 `nvidia-smi` 原始文本直接暴露给前端 |
| C | 挂载发现、空间扫描任务、分块事件和 treemap | 任务队列、SSE、主机详情页 | 扫描只能从挂载点或 home 开始；one-file-system、硬链接去重、稀疏文件和权限错误有测试证据；取消后不再推送新块 |
| D | 用户目录、sudo 三态和 UID 归属扫描 | C 的扫描树；passwd/group 解析 | 组成员、策略存在、有效权限分别可见；unknown 不折算为 false 或 0；共享硬链接和 deleted-open 文件口径有说明 |

实现前需冻结以下接口决策：时间序列点使用远端采样时间还是本机接收时间（建议两者同时返回）；速率差分遇到计数器回绕时返回 `unavailable` 并重新建立基线；扫描块使用稳定节点 ID 以支持重试；用户空间归属在 overlay、NFS、FUSE 或 quota 不可判定时返回能力状态。

发布前的共同检查清单：

- API schema 拒绝未知字段，所有路径参数经过挂载点/home allowlist 校验。
- 远端命令仍是固定探针和只读扫描；sudo 规则只覆盖规划中的命令，不允许通过参数拼接任意命令。
- 采集和扫描输出有大小、时长、并发和事件速率上限；任务取消、超时和应用重启后的 `interrupted` 状态可复验。
- 文档、迁移（如有）、后端/前端测试和验收证据在同一变更中更新；未满足任一项不得将阶段标记为已完成。

