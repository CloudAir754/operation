# API 契约（v1）

后端监听 `127.0.0.1`，所有接口位于 `/api` 下，请求与响应均为 JSON（SSE 除外）。
字段使用 camelCase；时间使用带时区的 ISO 8601 字符串（UTC，`Z` 结尾）。

## 1. 通用约定

### 1.1 错误响应

所有错误统一为：

```json
{"detail": {"code": "server_not_found", "message": "主机不存在", "fields": [{"field": "unit", "message": "不能为空"}]}}
```

- `code`：稳定的机器可读标识，前端据此分支处理。
- `message`：中文可读信息，可直接展示。
- `fields`：可选的字段级错误列表，仅 422 使用。

常用状态码：`400` 参数/动作非法，`404` 资源不存在，`409` 冲突（服务保护、幂等键重复、任务已结束），`422` 字段校验失败，`502` 远端执行失败，`503` 本机缺少 SSH 客户端或凭据后端不可用。

### 1.2 主机对象

```json
{
  "id": 1,
  "alias": "lab-gpu-01",
  "hostname": "10.0.0.11",
  "user": "labadmin",
  "port": 22,
  "identityFile": "~/.ssh/id_ed25519",
  "proxyJump": "bastion",
  "proxyCommand": null,
  "connectTimeout": 10,
  "source": "ssh_config",
  "enabled": true,
  "notes": "",
  "hasSudoPassword": true,
  "lastConnect": {
    "status": "ok",
    "error": null,
    "at": "2026-10-25T12:00:00Z"
  }
}
```

`lastConnect.status` 取值 `ok` / `failed` / `unknown`。`source` 取值 `ssh_config` / `manual`。

## 2. 系统

### `GET /api/health`

```json
{"status": "ok", "version": "0.1.0", "time": "2026-10-25T12:00:00Z"}
```

### `GET /api/settings`

```json
{
  "allowArbitraryCommands": false,
  "snapshotTtlSeconds": 20,
  "maxOutputBytes": 262144,
  "sshConfigPath": "C:\\Users\\me\\.ssh\\config",
  "appDataDir": "C:\\Users\\me\\AppData\\Local\\LabOpsPanel",
  "bindHost": "127.0.0.1",
  "bindPort": 8765,
  "transport": "openssh"
}
```

### `GET /api/actions`

动作注册表，供前端渲染参数表单与风险提示。

```json
{
  "actions": [
    {
      "id": "service_restart",
      "label": "重启服务",
      "description": "systemctl restart <unit>",
      "risk": "medium",
      "requiresConfirmation": true,
      "requiresIdempotencyKey": true,
      "fields": [{"name": "unit", "required": true, "type": "string"}]
    }
  ]
}
```

`risk` 取值 `low` / `medium` / `high`。

## 3. SSH 配置与主机清单

### `GET /api/ssh-config`

```json
{
  "path": "C:\\Users\\me\\.ssh\\config",
  "exists": true,
  "hosts": [
    {
      "alias": "lab-gpu-01",
      "hostname": "10.0.0.11",
      "user": "labadmin",
      "port": 22,
      "identityFile": "~/.ssh/id_ed25519",
      "proxyJump": null,
      "proxyCommand": null,
      "connectTimeout": 10,
      "imported": true,
      "lineNumber": 12
    }
  ],
  "includes": [{"pattern": "~/.ssh/conf.d/*", "resolved": ["C:\\Users\\me\\.ssh\\conf.d\\a"], "error": null}],
  "errors": [{"path": "C:\\Users\\me\\.ssh\\config", "line": 7, "message": "缺少 Host 关键字"}]
}
```

只返回显式 `Host`（不含 `*`、`?`、`!` 通配与取反），解析失败不静默降级，必须出现在 `errors`。
通配 `Host` 块按 OpenSSH“首次取值获胜”的语义作为默认值来源；`Match` 块不参与求值（解析器直接跳过，文档中已注明）。
缺少 `HostName` 时按 OpenSSH 语义回退为别名本身，而不是报错。
`Port` 越界或 `ConnectTimeout` 非法的条目会被跳过并写入 `errors`。

### `POST /api/servers/import`

请求：

```json
{"aliases": ["lab-gpu-01"], "pruneMissing": false}
```

`aliases` 省略或为 `null` 时导入全部显式 Host。响应：

```json
{
  "imported": ["lab-gpu-01"],
  "updated": [],
  "skipped": [{"alias": "lab-old", "reason": "SSH 配置中不存在该 Host"}],
  "errors": [{"path": "...", "line": 7, "message": "..."}]
}
```

重复导入必须幂等：已存在的主机进入 `updated`，不产生重复行。
`pruneMissing=true` 时，配置中已不存在的 `ssh_config` 来源主机只被停用（保留历史任务），不删除。
`aliases` 中不存在于配置的别名进入 `skipped`；`skipped` 与 `errors` 都必须展示给用户。

### `GET /api/servers`

```json
{"servers": [Host], "count": 1}
```

### `PATCH /api/servers/{serverId}`

请求（字段均可选，未知字段拒绝，至少提供一个字段）：

```json
{"enabled": false, "notes": "维护中"}
```

响应：`Host`。

### `GET /api/servers/{serverId}`

返回单个 `Host`；不存在时 `404 server_not_found`。前端可据此直接打开主机详情/设置页。

### `POST /api/servers/{serverId}/connection-test`

同步执行，最长 20 秒。

```json
{
  "ok": true,
  "status": "ok",
  "error": null,
  "durationMs": 842,
  "exitCode": 0,
  "testedAt": "2026-10-25T12:00:00Z"
}
```

失败时 `ok=false`，`error` 为可读原因（认证失败、主机不可达、超时等），HTTP 仍为 200 并持久化结果。

## 4. 只读状态采集

### `GET /api/servers/{serverId}/snapshot?refresh=false`

缓存 20 秒内复用，过期时响应体带 `stale=true` 并仍然返回旧数据。

```json
{
  "serverId": 1,
  "collectedAt": "2026-10-25T12:00:00Z",
  "ageSeconds": 4,
  "stale": false,
  "status": "ok",
  "error": null,
  "data": {
    "system": {
      "status": "ok",
      "error": null,
      "data": {
        "hostname": "lab-gpu-01",
        "os": "Ubuntu 24.04.1 LTS",
        "kernel": "6.8.0-45-generic",
        "arch": "x86_64",
        "uptimeSeconds": 864000,
        "bootTime": "2026-10-15T12:00:00Z"
      }
    },
    "cpu": {
      "status": "ok",
      "error": null,
      "data": {"model": "AMD EPYC 7543", "cores": 32, "usagePercent": 12.5, "load1": 0.4, "load5": 0.5, "load15": 0.6}
    },
    "memory": {
      "status": "ok",
      "error": null,
      "data": {"totalBytes": 68719476736, "availableBytes": 34359738368, "usedPercent": 50.0, "swapTotalBytes": 0, "swapUsedBytes": 0}
    },
    "disks": {
      "status": "ok",
      "error": null,
      "data": {
        "mounts": [
          {"mount": "/", "filesystem": "/dev/nvme0n1p2", "totalBytes": 1000000000000, "usedBytes": 400000000000, "freeBytes": 600000000000, "usedPercent": 40.0}
        ]
      }
    },
    "network": {
      "status": "ok",
      "error": null,
      "data": {
        "defaultInterface": "eth0",
        "interfaces": [
          {"name": "eth0", "state": "up", "speedMbps": 10000, "rxBytes": 123456, "txBytes": 654321, "rxErrors": 0, "txErrors": 0, "addresses": ["10.0.0.11/24"]}
        ],
        "gateway": "10.0.0.1"
      }
    },
    "gpu": {
      "status": "unavailable",
      "error": "nvidia-smi not found",
      "data": null
    },
    "services": {
      "status": "ok",
      "error": null,
      "data": {
        "runningCount": 42,
        "failedCount": 1,
        "failedUnits": ["broken.service"],
        "units": [{"name": "ssh.service", "load": "loaded", "active": "active", "sub": "running", "description": "OpenBSD Secure Shell server", "protected": true}]
      }
    }
  }
}
```

分区（section）状态取值 `ok` / `permission_denied` / `unavailable` / `error`；
不支持或缺少权限时必须返回状态与 `error`，禁止伪造为零。

### `GET /api/servers/{serverId}/services?refresh=false`

```json
{
  "serverId": 1,
  "collectedAt": "2026-10-25T12:00:00Z",
  "status": "ok",
  "error": null,
  "units": [{"name": "nginx.service", "load": "loaded", "active": "active", "sub": "running", "description": "A high performance web server", "protected": false}]
}
```

### `GET /api/servers/{serverId}/logs?unit=nginx.service&lines=200`

`lines` 取值 1–2000，默认 200。`unit` 为空时查询全部日志。

```json
{
  "serverId": 1,
  "unit": "nginx.service",
  "collectedAt": "2026-10-25T12:00:00Z",
  "lines": ["Oct 25 12:00:00 host nginx[1]: started"],
  "exitCode": 0,
  "truncated": false,
  "error": null,
  "command": "journalctl -u nginx.service -n 200 --no-pager"
}
```

### `GET /api/servers/{serverId}/secret`

只返回状态，绝不返回明文：

```json
{"serverId": 1, "hasPassword": true, "updatedAt": "2026-10-25T12:00:00Z"}
```

### `PUT /api/servers/{serverId}/secret`

```json
{"password": "s3cret"}
```

响应：`{"serverId": 1, "hasPassword": true, "updatedAt": "2026-10-25T12:00:00Z"}`。
`DELETE /api/servers/{serverId}/secret` → `204`。

## 5. 任务

### 任务对象

```json
{
  "id": "0f7f1a52-4b0c-4a51-9f0c-5b9e2f5f0a11",
  "serverId": 1,
  "serverAlias": "lab-gpu-01",
  "action": "service_restart",
  "actionLabel": "重启服务",
  "risk": "medium",
  "params": {"unit": "nginx.service"},
  "status": "running",
  "progress": 40,
  "exitCode": null,
  "error": null,
  "outputTail": "restarting nginx.service...",
  "idempotencyKey": "6c1d...",
  "createdAt": "2026-10-25T12:00:00Z",
  "startedAt": "2026-10-25T12:00:01Z",
  "finishedAt": null,
  "steps": [
    {"seq": 1, "name": "预检", "status": "ok", "exitCode": 0, "outputTail": "", "startedAt": "2026-10-25T12:00:01Z", "finishedAt": "2026-10-25T12:00:02Z"}
  ]
}
```

`status`：`queued` / `running` / `succeeded` / `failed` / `cancelled` / `interrupted`。
步骤 `status`：`pending` / `running` / `ok` / `failed` / `skipped`。

### `POST /api/tasks`

```json
{
  "serverId": 1,
  "action": "service_restart",
  "params": {"unit": "nginx.service"},
  "confirm": true,
  "idempotencyKey": "6c1d..."
}
```

- `confirm` 必须为 `true`，否则 422（`confirmation_required`）。
- 写操作（`service_*`、`run_command`）缺少 `idempotencyKey` → 422（`idempotency_key_required`）；只读任务
  （`host_check`、`collect_snapshot`、`query_logs`）可省略，且不要求 `confirm`。
- 相同 `idempotencyKey` 重复提交 → 409（`duplicate_idempotency_key`），响应 `detail.existingTaskId`。
- 受保护服务且动作危险 → 409（`protected_service`）。
- 需要 sudo 但未保存密码 → 409（`sudo_password_missing`）；凭据后端不可用 → 503（`sudo_unavailable`）。
- 未知动作 → 400（`unknown_action`）；任意命令入口关闭时 `run_command` → 400（`action_disabled`）。
- 主机停用 → 400（`server_disabled`）；主机不存在 → 404（`server_not_found`）。
- 成功 → `201`，响应任务对象（`queued`）。

### 任务列表与详情

- `GET /api/tasks?serverId=1&limit=50&status=running` → `{"tasks": [Task], "count": 1}`。
- `GET /api/tasks/{taskId}` → `Task`。
- `POST /api/tasks/{taskId}/cancel` → `202 {"task": Task}`；已结束任务返回 409（`task_finished`）。
- `GET /api/tasks/{taskId}/events` → SSE。

### SSE 事件

`text/event-stream`，事件名与 `data`（单行 JSON）：

```
event: status
data: {"type":"status","taskId":"...","status":"running","progress":25,"at":"..."}

event: step
data: {"type":"step","taskId":"...","seq":1,"name":"预检","status":"running"}

event: log
data: {"type":"log","taskId":"...","channel":"stdout","chunk":"...","at":"..."}

event: done
data: {"type":"done","taskId":"...","status":"succeeded","progress":100,"exitCode":0,"error":null}
```

连接后先推送当前快照（`status` + 已有步骤），随后推送增量；`done` 后服务端关闭连接。
每 15 秒发送注释行 `: keepalive`。

## 6. 审计

`GET /api/audit?limit=50`：

```json
{
  "entries": [
    {"id": 1, "action": "service_restart", "target": "lab-gpu-01", "outcome": "succeeded", "detail": "nginx.service", "createdAt": "2026-10-25T12:00:00Z"}
  ]
}
```

审计只记录动作、目标、时间、状态与错误摘要，不记录密码或完整输出。

## 7. 前端路由

| 路径 | 页面 | 数据来源 |
| --- | --- | --- |
| `/` | 主机总览 | `GET /api/servers`、`GET /api/ssh-config`、`POST /api/servers/import` |
| `/hosts/:id` | 主机详情 | snapshot、services、logs、`POST /api/tasks` |
| `/tasks` | 维护任务 | `GET /api/tasks`、SSE、取消 |
| `/hosts/:id/settings` | 主机设置 | `PATCH /api/servers/{id}`、secret、connection-test |
