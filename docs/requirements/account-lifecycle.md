# 账户生命周期：新建用户、目录迁移与新空间分配

本文定义阶段四的三项功能及其安全边界：**批量新建用户（含随机密码与系统密码策略）**、
**迁移整个用户目录并保留逻辑路径**、**给某个用户分配新空间**。

三项功能都会改变远端账户或磁盘布局，因此全部遵循仓库既有约束：

- 只用结构化参数与固定命令，不接受任意 shell；参数经 Pydantic 精确校验并拒绝未知字段。
- 任何写操作都需要远端 `sudo`；控制台只保存 DPAPI 加密后的 sudo 密码，明文密码只存在于
  一次请求的内存与（必要时）受限临时文件中。
- 写操作先进后台任务，落库阶段与进度，可取消；应用重启后未完成的作业标记 `interrupted`，
  绝不自动重试高风险步骤。
- 审计只记录动作、目标、结果与错误摘要，不记录密码、令牌或完整命令输出。

## 1. 权限模型：两个层次的密码

界面上必须清晰区分两个层次，任何地方都不得混用同一个“密码”字样：

| 层次 | 名称 | 用途 | 存放位置 | 是否由本应用生成 |
| --- | --- | --- | --- | --- |
| 第一层 | **运维 sudo 密码** | 本应用登录远端后执行提权动作（创建用户、改 PAM、搬目录、建链接） | Windows DPAPI 加密后存 SQLite | 否，管理员手工录入 |
| 第二层 | **目标账户登录密码** | 新建出来的 Linux 账户将来登录用的密码 | 只读一次：生成后立即展示，不落库 | 是（随机生成）或管理员手工指定 |

界面要求：

- 每个需要提权的按钮旁标注 `需要 sudo`，并在「运维 sudo 密码」卡片里显示“已保存 / 未保存”。
- 未保存 sudo 密码时，写操作直接返回 `409 sudo_password_missing`，界面给出前往填写位置的链接。
- 生成的目标账户密码只通过一次性接口下发（`POST /api/account-batches/{id}/reveal`），
  读取后即从内存清除；页面明确提示“关闭后无法再次查看”。

## 2. 功能一：批量新建用户

### 2.1 随机密码生成规则

- 可设置长度、最少大写字母、最少小写字母、最少数字与最少符号数。
- 可配置允许的符号集合，可选择排除 `0`/`O`、`1`/`l`/`I` 等易混淆字符。
- 默认：长度 20，至少各 1 个大写/小写/数字/符号，排除易混淆字符。
- 一致性检查：各类最少字符数之和不得超过总长度；启用符号时符号集合不能为空；符号集合不得
  包含空白与控制字符；长度 8–128。
- 本机 PAM 更严格时，实际规则自动提高到 PAM 下限，并在界面上列出“因系统策略而提高”的条目。
- 使用操作系统密码学安全随机源：Python `secrets.SystemRandom`（`os.urandom`），
  不使用 `random` 的默认伪随机数发生器。
- 生成后按合并后的规则自检（长度、字符类别、`maxrepeat`、`maxsequence`、
  `maxclassrepeat`、用户名包含限制）；不通过则重新抽取，超过重试上限报错，不降级放行。
- 修改规则时实时生成**脱敏示例**（只显示前 2 位）并返回一致性检查结果。
- 首次使用时（尚无保存的生成规则）返回 `configured=false`，界面进入配置向导，确认后保存。

### 2.2 Ubuntu 系统密码策略

- 读取并展示**当前实际生效**的 PAM 配置，而不是应用内配置：
  - `/etc/security/pwquality.conf` 及其 `pwquality.conf.d/*.conf`（存在时）；
  - `/etc/pam.d/common-password` 中 `pam_pwquality.so`/`pam_cracklib.so` 的行内参数
    （PAM 参数优先级高于配置文件）；
  - `/etc/login.defs` 的 `PASS_MIN_LEN`、`ENCRYPT_METHOD`（仅展示）；
  - `/etc/pam.d/common-auth` 中 `pam_unix.so` 的 `retry=`（重试次数）。
- 可修改：最小长度、字符类别（`dcredit`/`ucredit`/`lcredit`/`ocredit`/`minclass`）、
  连续与重复限制（`maxrepeat`/`maxsequence`/`maxclassrepeat`）、用户名包含限制
  （`gecoscheck`）、重试次数（`retry`）。
- 写入口径：优先 `pwquality.conf`；仅当某个键已被写进 PAM 行内参数时才改那一行的参数，
  且只在该行是可识别的规范单行形态时允许修改，否则该键只读并说明原因。
- 能力检测：发行版/版本、`pwquality.conf` 是否存在、PAM 栈里是否包含 pwquality 模块、
  `pwscore`/`chage`/`useradd` 是否可用、drop-in 目录是否存在；任一关键能力缺失即整体只读，
  不提供“猜测式”写入。
- 保存前展示 **unified diff** 并二次确认；保存前把涉及的原文件复制到
  `/var/backups/labopspanel/`（0700，仅 root），同时在本地库保存原文，便于回退。
- 保存后验证：重新读取实际生效配置并与目标值比对；若 `pwscore` 可用，用一对正/负样例
  执行行为验证（应当通过 / 应当拒绝）。任一验证失败**立即**用备份恢复原文件并标记失败。
- 「恢复本次修改前配置」单独入口，按记录把两个文件写回原文，同样重新验证。
- 首版不设置首次登录强制改密，不管理密码最长/最短使用天数与周期性到期；
  发现泄露等风险时由管理员主动重置。

### 2.3 批量创建

- 一次最多 50 个账户，逐个执行并逐个记录结果；任一失败不回滚已成功的账户，但明确列出成功/失败。
- 用户名规则：`^[a-z_][a-z0-9_-]{0,31}$`；已存在的用户名在预检阶段拒绝。
- 命令固定为 `useradd`（`--create-home`、`--shell`、`--comment`、`--groups`、可选 `--uid`），
  密码通过 `chpasswd` 写入。
- 密码不进入 argv：先用普通（非 sudo）步骤把 `login:password` 写入远端 `umask 077` 的临时文件，
  再用 sudo 步骤 `chpasswd < 临时文件`，最后无条件删除临时文件（失败也删除）。
- 创建后校验：账户存在、UID 有效、home 目录存在且属主正确、密码状态为可用
  （`passwd -S` 输出 `P`）；密码设置失败则尝试 `userdel -r` 回滚该账户并标记失败。

## 3. 功能二：迁移某用户文件

目标：把 `/home/zhangsan` 迁移到 `/data2/users/zhangsan`，原路径变成指向新位置的符号链接。

### 3.1 阶段与状态机

| 阶段 | 内容 | 失败处理 |
| --- | --- | --- |
| `preflight` | 识别源 home、目标挂载点、文件系统、inode、只读、符号链接、子挂载、目标冲突 | 直接失败，不动任何东西 |
| `space_check` | 源逻辑大小/实际占用/文件数/inode 需求；目标剩余空间与余量比较 | 直接失败 |
| `freeze` | 记录账号原状态、锁定登录、终止会话/用户级 systemd 与进程（TERM→等待→KILL→复查） | 恢复账号状态后失败 |
| `rename` | 同父目录内原子 `mv -T` 为 `<home>.labops-backup-<ts>-<id>` | 恢复账号后失败 |
| `copy` | 从备份目录 `rsync` 到目标临时目录（保留 UID/GID、mode、时间戳、ACL、xattr、硬链接、稀疏） | 保留原目录，不建链接，恢复账号 |
| `verify` | 前后文件数/目录数/逻辑大小/实际占用/关键元数据；目标盘可用空间变化；所有者 | 同上 |
| `switch` | 临时目录改名为最终目录，原路径建符号链接，验证解析后的物理位置 | 进入可回退状态 |
| `restore` | 恢复账号原状态，以该用户身份验证登录/读/写/改/删与 SSH 公钥路径权限 | 提示人工回退 |
| `observe` | 保留备份目录，展示占用、保留期与回退/清理入口 | — |

- 任一阶段失败都停留在可识别状态：`status=failed` + `stage=<失败阶段>`，可直接回退。
- 应用重启：`queued/running` 迁移标记 `interrupted`，保留阶段与备份信息；`resume` 先做
  状态核对（源路径是否已是链接、备份是否存在）再从安全点继续；`rollback` 走反向流程。
- 「已复制」与「可删除备份」严格分离：只有 `stage=observe` 且 `status=succeeded` 才提供清理入口。

### 3.2 白名单

首版只允许**本地 ext4**：源 home 与其父目录所在文件系统、目标根目录所在文件系统都必须是
`ext4` 且设备为 `/dev/*`。XFS、Btrfs、ZFS、网络文件系统（nfs/cifs/sshfs）、加密目录
（eCryptfs/fscrypt 挂载）、tmpfs 及其他未知布局只读展示，`POST` 直接返回 `422 filesystem_not_supported`。

其余硬性拒绝条件：源路径是挂载点、源目录含子挂载、源/目标只读、目标目录已存在、
目标不在已配置的迁移根目录之下、统计遍历被截断或不完整。

### 3.3 空间与校验口径

- 同时给出逻辑大小（`st_size` 之和）、实际占用（`st_blocks*512`）、文件数、目录数与 inode 需求。
- 目标可用空间 ≥ 实际占用 × (1 + 安全余量) + 固定保留；inode 同理。
- 复制前后比较目标盘 `freeBytes` 变化与目标目录实际占用，差异超过 5% + 64 MiB 视为异常并停止。
- 可选全量内容校验（`rsync --checksum` 的等价口径：比较源/目标逐文件校验和），默认关闭。

### 3.4 回退与人工清理

- `POST /api/home-migrations/{id}/rollback`：重新冻结账号 → 删除/改名符号链接 → 把备份目录
  `mv -T` 回原 home → 恢复账号状态 → 校验。已建链接时的回退必须处理“链接里已有新写入”的情况：
  默认拒绝并提示，除非显式 `discardNewData=true`。
- `POST /api/home-migrations/{id}/cleanup`：只有管理员明确确认“新位置功能正常”后才可用。
  删除前再次核对任务 ID、备份路径、目标路径与当前链接关系，任一项不符即拒绝。

## 4. 功能三：给某用户分配新空间

- 每台主机保存两个默认路径：`dataRoot`（数据盘挂载点，如 `/data2`）与 `migrationRoot`
  （迁移目标父目录，如 `/data2/users`），通过 `PUT /api/servers/{id}/storage-settings` 保存。
- 分配流程：校验登录账户与 UID/GID → 校验 `dataRoot` 是本地 ext4 挂载点且非只读 →
  `install -d -o UID -g GID -m 0750 <dataRoot>/<login>` → 在 home 下建立符号链接
  `<home>/<linkName>`（默认 `data2_disk`）指向该目录 → 以该用户身份验证可写 → 记录。
- 目标目录已存在时：为空目录则复用，非空则拒绝（默认 `reuseExisting=false`）。
- 链接已存在时：指向同一目标视为幂等成功；指向别处则拒绝。
- 「解除链接」只删除符号链接，不删除数据目录；删除数据目录不在首版范围。

## 5. 接口一览

| 方法 | 路径 | 说明 | sudo |
| --- | --- | --- | --- |
| GET | `/api/accounts/generator` | 当前生成规则与脱敏示例、是否已配置 | 否 |
| PUT | `/api/accounts/generator` | 保存生成规则（含一致性检查） | 否 |
| POST | `/api/accounts/generator/preview` | 用未保存规则生成脱敏示例与一致性检查 | 否 |
| GET | `/api/servers/{id}/accounts/policy` | 实际生效的 PAM 密码策略与能力 | 否 |
| PUT | `/api/servers/{id}/accounts/policy` | 应用策略（diff + 备份 + 验证，失败自动恢复） | 是 |
| POST | `/api/servers/{id}/accounts/policy/{changeId}/restore` | 恢复某次修改前的配置 | 是 |
| GET | `/api/servers/{id}/accounts/policy/changes` | 策略修改历史 | 否 |
| POST | `/api/servers/{id}/account-batches` | 批量新建用户 | 是 |
| GET | `/api/servers/{id}/account-batches` | 批次列表 | 否 |
| GET | `/api/account-batches/{batchId}` | 批次详情（不含密码） | 否 |
| POST | `/api/account-batches/{batchId}/reveal` | 一次性取回生成的密码 | 是（无需 sudo） |
| POST | `/api/account-batches/{batchId}/cancel` | 取消尚未执行的账户 | 否 |
| POST | `/api/servers/{id}/home-migrations/preflight` | 只读预检 | 否 |
| POST | `/api/servers/{id}/home-migrations` | 创建迁移作业 | 是 |
| GET | `/api/home-migrations/{id}` | 详情与阶段进度 | 否 |
| POST | `/api/home-migrations/{id}/cancel` | 取消（仅在安全阶段生效） | 否 |
| POST | `/api/home-migrations/{id}/resume` | 从中断处继续 | 是 |
| POST | `/api/home-migrations/{id}/rollback` | 回退到原 home | 是 |
| POST | `/api/home-migrations/{id}/cleanup` | 人工清理备份 | 是 |
| GET | `/api/home-migrations/{id}/events` | SSE 阶段事件 | 否 |
| GET/PUT | `/api/servers/{id}/storage-settings` | 数据盘与迁移根目录 | 否 |
| POST | `/api/servers/{id}/datasets` | 分配新空间 | 是 |
| GET | `/api/servers/{id}/datasets` | 已分配清单 | 否 |
| POST | `/api/datasets/{id}/unlink` | 解除符号链接（保留数据） | 是 |

## 6. 验收标准

- [ ] 生成器只用 `secrets`，规则冲突时返回字段级错误，PAM 更严格时自动提高并说明。
- [ ] 策略读取以实际生效值为准（PAM 参数覆盖配置文件），能力不足时整体只读。
- [ ] 策略写入有 diff、备份、验证与自动恢复；`restore` 能恢复原文。
- [ ] 批量建用户逐个记录结果，密码一次性下发且不落库，失败账户可回滚。
- [ ] 迁移覆盖 9 个阶段，非 ext4/网络文件系统只读展示并拒绝写入。
- [ ] 迁移中断可识别阶段并恢复或回退；清理入口只在成功后可用且四要素复核。
- [ ] 空间分配幂等，链接指向同目标可重复执行，解除链接不删除数据。
- [ ] 前端在每处提权操作旁标注“需要 sudo”，并提供 sudo 密码录入入口。

## 7. 测试约束

所有用例使用 `FakeTransport` 与临时目录，绝不操作真实账户、`/etc/pam.d`、真实磁盘或生产主机；
远端命令只做字符串断言，不在本机执行。
