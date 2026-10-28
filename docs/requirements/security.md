# 安全设计与残余风险

本文记录首版的安全边界、实现方式与已知残余风险。任何新增远程写操作都必须先补充本文件与
`acceptance.md` 的验收条目。

## 1. 威胁模型

LabOpsPanel 运行在管理员自己的 Windows 工作站上，信任本机当前登录用户；不信任网络对端，
也不假设远端主机配置正确。主要风险来自：

- 误操作导致远端服务/网络中断，或失去 SSH 访问能力。
- 凭据泄漏（sudo 密码、私钥、会话令牌）进入数据库、日志或前端。
- 远端输出过大、命令挂起或交互式提示导致本机资源耗尽。
- 通过 SSH 配置解析差异绕过既有限制（例如错误地解释 `ProxyJump`/`ProxyCommand`）。

## 2. 网络暴露面

- 默认只绑定 `127.0.0.1:8765`；`Settings` 在启动时校验，非回环地址会被拒绝，除非显式设置
  `LABOPS_ALLOW_REMOTE_BIND=true`。
- 首版无登录、无多用户 RBAC：任何能访问该端口的本机进程都等同于管理员。不要将端口映射到局域网。
- 静态资源仅从 `frontend/dist` 提供；SPA 回退对路径做 `relative_to(dist)` 校验，拒绝路径穿越；
  `/api/*` 不参与回退。

## 3. 凭据

- sudo 密码使用 Windows DPAPI（当前用户作用域）加密后写入 `server_secrets.ciphertext`；
  数据库中没有明文，API 从不返回明文，前端也从不回显。
- 解密只发生在执行需要提权的步骤前，密码通过 **stdin** 传给远端 `sudo -S -p ''`，
  不进入 argv、进程列表快照、任务参数或审计日志。
- `sudo` 的时间戳缓存可能让同一会话内的后续命令免密；这是远端 sudo 的既有行为，应用不额外延长缓存。
- DPAPI 密文与 Windows 账户绑定：数据库文件复制到其他账户下无法解密，会返回明确的解密失败。
- `LABOPS_SECRET_BACKEND=disabled` 只用于排障，会禁用凭据后端并让需要 sudo 的动作返回 503。

## 4. SSH 行为

- 导入自 OpenSSH 配置的主机只传别名，并带 `-F <config>`，让 OpenSSH 完整应用 `Include`、
  `ProxyJump`、`ProxyCommand`、`IdentityFile`，避免自行拼接导致行为偏差。
- 固定 `BatchMode=yes` 与 `NumberOfPasswordPrompts=0`：不进行交互式认证，不会卡在密码提示上。
- `StrictHostKeyChecking=accept-new`：首次连接接受新主机密钥，之后密钥变化会失败并给出诊断。
  这是可用性与安全性的折中；高安全环境应预置 `known_hosts`，并把该选项改为 `yes`。
- 连接超时、命令超时、取消均会终止本机 ssh 子进程；远端进程由 SSH 关闭连接时回收。

## 5. 远程写操作

- 只有固定动作白名单可以执行；参数模型 `extra="forbid"`，未知字段直接 422。
- 每次提交写操作都需要 `confirm=true` 与 `idempotencyKey`，重复键返回 409，避免重复执行。
- 服务名经过正则白名单校验并规范化；受保护服务（`ssh*`、`systemd-*`、`dbus*`、`network*`、
  `netplan*`、`polkit*`、`auditd`、`ufw`、`firewalld` 等）禁止 `stop`/`disable`/`mask`。
- 服务动作固定为“预检 → 执行 → 校验”三步，执行前读取 `LoadState`，执行后校验 `is-active`/`is-enabled`。
- 任意命令入口默认关闭；启用后仍需 Bash `-n` 语法检查、高风险确认、超时、输出截断与幂等键。
- 输出按流截断（默认 256 KiB），任务尾部最多保留 4000 字符；被截断的探针输出视为采集失败。

## 6. 数据与日志

- SQLite 位于 `%LOCALAPPDATA%\LabOpsPanel`，不进源码树；WAL、外键与 `busy_timeout` 已开启。
- 快照包含远端系统信息（主机名、磁盘、服务列表），属于敏感基础设施数据：只保存在本机用户目录，
  每台主机最多保留 50 份。
- 审计记录动作、目标、时间、结果与错误摘要；不记录密码、完整输出或私钥内容。
- 任务参数会落库（如服务名、日志行数），因此严禁把密码类参数放入动作参数。

## 7. 依赖与构建

- Python 依赖由 `uv` 锁定（`uv.lock`），前端由 `package-lock.json` 锁定。
- 前端生产构建产物由后端同源提供，不引入 CDN、外部脚本或遥测。
- 生产模式关闭 FastAPI `/api/docs` 之外的额外端点；文档端点仅本机可访问。

## 8. 残余风险

| 风险 | 说明 | 缓解 |
| --- | --- | --- |
| 本机进程可调用 API | 回环端口无认证 | 仅个人工作站使用；不开放端口转发 |
| 首次主机密钥被接受 | `accept-new` 无法验证首次身份 | 预置 `known_hosts`，或改为 `yes` |
| sudo 密码可在内存中被读取 | 解密后短暂存在于进程内存 | 仅在本机受信环境运行；不启用崩溃转储 |
| 远端 sudo 规则过宽 | 应用只按动作调用 sudo，无法审计远端 sudoers | 远端最小权限 sudoers，只允许 systemctl/journalctl |
| 交互式服务停止导致失联 | 受保护列表是静态白名单 | 保护 `ssh*`/`network*`；其他服务需人工确认 |
| 输出被截断导致信息不全 | 上限保护本机资源 | 提示 `truncated`，需要完整输出时用远端工具 |
