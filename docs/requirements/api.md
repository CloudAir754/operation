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
| `/hosts/:id` | 主机详情 | snapshot、services、logs、`POST /api/tasks`；`?tab=accounts` 打开账户生命周期 |
| `/tasks` | 维护任务 | `GET /api/tasks`、SSE、取消 |
| `/hosts/:id/settings` | 主机设置 | `PATCH /api/servers/{id}`、secret、connection-test |

## 8. 阶段三接口（资源观测）

统一约定：

- 分区对象形如 `{"status": "ok|permission_denied|unavailable|error", "error": null, "requiresSudo": false, "collectedAt": "..."}`；
  分区不可用时 `data` 必须为 `null`，未知值不得写成 0。
- 所有请求模型拒绝未知字段；路径参数先经过挂载点/home allowlist 校验。
- 采集与扫描沿用输出上限、超时、取消与审计约定。

### 8.1 `GET /api/servers/{serverId}/metrics?window=1m|5m|15m|1h`

每次请求执行一次远端采样；服务端保留最近 1 小时环形数据（默认 2 秒采样），
`points` 按窗口裁剪。`reset=true` 表示计数器回绕/间隔过短，该点速率为 `null`。

```json
{
  "serverId": 1,
  "collectedAt": "2026-10-30T12:00:00Z",
  "windowSeconds": 300,
  "retentionSeconds": 3600,
  "sampleIntervalMs": 2000,
  "stale": false,
  "status": "ok",
  "error": null,
  "requiresSudo": false,
  "current": {
    "cpu": {
      "status": "ok", "error": null, "requiresSudo": false, "collectedAt": "2026-10-30T12:00:00Z",
      "data": {
        "totalPercent": 12.5, "userPercent": 8.0, "systemPercent": 3.0,
        "iowaitPercent": 1.0, "stealPercent": 0.0, "idlePercent": 87.5,
        "logicalCpus": 32, "load1": 0.42, "load5": 0.55, "load15": 0.61
      }
    },
    "memory": {
      "status": "ok", "error": null, "requiresSudo": false, "collectedAt": "2026-10-30T12:00:00Z",
      "data": {
        "totalBytes": 68719476736, "availableBytes": 34359738368, "usedBytes": 34359738368,
        "usedPercent": 50.0, "buffersBytes": 1073741824, "cachedBytes": 8589934592,
        "commitBytes": 12884901888, "swapTotalBytes": 8589934592, "swapUsedBytes": 429496729,
        "swapFreeBytes": 8160437862, "swapInPerSecond": null, "swapOutPerSecond": 0.0
      }
    },
    "disk": {
      "status": "ok", "error": null, "requiresSudo": false, "collectedAt": "...",
      "data": {
        "devices": [
          {
            "name": "nvme0n1", "kind": "disk", "parent": null, "model": "Samsung SSD 990 PRO",
            "sizeBytes": 2000398934016, "hidden": false,
            "readBytesPerSecond": 1048576.0, "writeBytesPerSecond": 2097152.0,
            "readIops": 12.5, "writeIops": 8.0, "awaitMs": 0.42, "utilPercent": 12.3, "reset": false
          }
        ],
        "mounts": [
          {"mount": "/", "device": "/dev/nvme0n1p2", "filesystem": "ext4",
           "deviceName": "nvme0n1p2", "readBytesPerSecond": null, "writeBytesPerSecond": null}
        ]
      }
    },
    "network": {
      "status": "ok", "error": null, "requiresSudo": false, "collectedAt": "...",
      "data": {
        "defaultInterface": "eth0",
        "interfaces": [
          {"name": "eth0", "kind": "physical", "state": "up", "speedMbps": 10000,
           "addresses": ["10.0.0.11/24"], "rxBytesPerSecond": 1024.0, "txBytesPerSecond": 2048.0,
           "rxPacketsPerSecond": 12.0, "txPacketsPerSecond": 8.0,
           "rxErrors": 0, "txErrors": 0, "rxDrops": 0, "txDrops": 0, "reset": false}
        ]
      }
    },
    "gpu": {
      "status": "ok", "error": null, "requiresSudo": false, "collectedAt": "...",
      "data": {"devices": [{"index": 0, "uuid": "GPU-…", "name": "NVIDIA GeForce RTX 4090",
        "utilizationPercent": 37.0, "memoryUsedMb": 8192, "memoryTotalMb": 24564,
        "encoderPercent": null, "decoderPercent": null, "temperatureC": 52.0, "powerDrawW": 210.5}]}
    }
  },
  "points": [
    {
      "collectedAt": "2026-10-30T12:00:00Z", "receivedAt": "2026-10-30T12:00:00Z",
      "sampleIntervalMs": 2000, "reset": false, "resetReason": null,
      "cpuPercent": 12.5, "userPercent": 8.0, "systemPercent": 3.0, "iowaitPercent": 1.0,
      "memoryUsedPercent": 50.0, "swapUsedPercent": 5.0,
      "disk": {"nvme0n1": {"readBytesPerSecond": 1048576.0, "writeBytesPerSecond": 2097152.0,
                            "readIops": 12.5, "writeIops": 8.0, "reset": false}},
      "network": {"eth0": {"rxBytesPerSecond": 1024.0, "txBytesPerSecond": 2048.0,
                            "rxPacketsPerSecond": 12.0, "txPacketsPerSecond": 8.0, "reset": false}},
      "gpu": [{"index": 0, "utilizationPercent": 37.0, "memoryUsedMb": 8192,
               "memoryTotalMb": 24564, "temperatureC": 52.0, "powerDrawW": 210.5}]
    }
  ],
  "raw": {"cpu": {}, "memory": {}, "vmstat": {}, "disk": {}, "network": {}, "mounts": [], "gpu": {}}
}
```

`collectedAt` 为远端采样时间，`receivedAt` 为本机接收时间；两者都返回便于识别时钟偏差。
`stale=true` 表示最后一个成功采样已超过 3 个采样间隔（或最近一次采集失败）。

### 8.2 `GET /api/servers/{serverId}/gpus?refresh=false`

```json
{
  "serverId": 1,
  "collectedAt": "2026-10-30T12:00:00Z",
  "status": "ok",
  "error": null,
  "requiresSudo": false,
  "capabilities": {"nvml": true, "processes": "ok", "mig": true, "perProcessUtilization": false},
  "devices": [
    {
      "index": 0, "uuid": "GPU-…", "pciBusId": "0000:01:00.0", "name": "NVIDIA GeForce RTX 4090",
      "driverVersion": "550.107.02", "memoryTotalMb": 24564, "memoryUsedMb": 8192,
      "utilizationGpuPercent": 37.0, "utilizationMemoryPercent": 33.3,
      "encoderPercent": null, "decoderPercent": null, "temperatureC": 52.0,
      "powerDrawW": 210.5, "powerLimitW": 450.0, "clockGraphicsMhz": 2520,
      "clockMemoryMhz": 10501, "migMode": "Disabled", "computeMode": "Default",
      "migDevices": ["MIG 1g.5gb Device 0: (UUID: MIG-…)"]
    }
  ],
  "processes": [
    {"pid": 4242, "user": "labuser", "name": "python", "command": "python train.py --config a.yaml",
     "memoryUsedMb": 8192, "utilizationPercent": null, "gpuUuid": "GPU-…", "gpuIndex": 0,
     "programPath": "/usr/bin/python3.12", "cwd": "/home/labuser/work",
     "startedAt": "2026-10-30T08:00:00Z", "stale": false, "error": null}
  ],
  "processesStatus": "ok",
  "processesError": null,
  "notes": []
}
```

`processesStatus` 为 `ok` / `partial`（部分进程不可见或采样后退出）/ `unavailable`；
`capabilities.perProcessUtilization=false` 时 `utilizationPercent` 必须为 `null`。
不返回 `nvidia-smi` 原始文本。

### 8.3 `GET /api/servers/{serverId}/users?includeSystem=false`

```json
{
  "serverId": 1,
  "collectedAt": "2026-10-30T12:00:00Z",
  "includeSystem": false,
  "status": "ok",
  "error": null,
  "requiresSudo": false,
  "users": [
    {
      "login": "labuser", "uid": 1001, "gid": 1001, "primaryGroup": "labuser",
      "supplementaryGroups": ["sudo", "docker"], "home": "/home/labuser",
      "shell": "/bin/bash", "system": false, "recentLogin": null,
      "sudo": {"groupMember": true, "policyPresent": true, "effective": "false",
                "checkedAt": "2026-10-30T12:00:00Z", "error": null},
      "homeStorage": {"allocatedBytes": 10737418240, "apparentBytes": 12884901888,
                       "fileCount": 12000, "directoryCount": 900, "depth": 2,
                       "truncated": false, "incomplete": false, "error": null,
                       "collectedAt": "2026-10-30T12:00:00Z"}
    }
  ],
  "notes": ["sudo 有效权限仅对当前 SSH 用户执行非交互验证，其他用户为 unknown"]
}
```

`sudo.effective` 取值为 `true` / `false` / `unknown`，未知不得折算为 `false`；
不读取 `/etc/shadow`，不回传原始 sudoers 内容。

### 8.4 `GET /api/servers/{serverId}/mounts?refresh=false`

```json
{
  "serverId": 1,
  "collectedAt": "2026-10-30T12:00:00Z",
  "status": "ok",
  "error": null,
  "requiresSudo": false,
  "mounts": [
    {"mount": "/", "device": "/dev/nvme0n1p2", "filesystem": "ext4",
     "options": "rw,relatime", "uuid": "0f8fad5b-…", "majorMinor": "259:2",
     "physicalDevice": "nvme0n1p2", "readOnly": false}
  ]
}
```

### 8.5 空间扫描

`POST /api/servers/{serverId}/storage/scans`：

```json
{
  "mount": "/data",
  "oneFileSystem": true,
  "followSymlinks": false,
  "maxDepth": 3,
  "userFilter": null,
  "minFileSize": 0,
  "extension": ".log",
  "modifiedAfter": "2026-10-01T00:00:00Z",
  "includeDeletedOpen": true
}
```

- `mount` 必须与 `GET /mounts` 中的挂载点完全一致，否则 `422 mount_not_allowed`（fail closed）。
- 同一主机同时只允许 1 个排队/运行中的扫描，冲突返回 `409 scan_limit_reached`。
- 响应 `201`，返回扫描对象（`status=queued`）。

`POST /api/servers/{serverId}/users/storage-scan`：

```json
{"uid": 1001, "mount": "/", "maxDepth": 8, "minFileSize": 0, "includeDeletedOpen": true}
```

要求 UID 属于 `GET /users?includeSystem=true`，否则 `404 user_not_found`。

扫描对象：

```json
{
  "scanId": "6f1c…",
  "serverId": 1,
  "serverAlias": "lab-gpu-01",
  "mount": "/data",
  "mountInfo": {"device": "/dev/nvme1n1", "mount": "/data", "filesystem": "ext4"},
  "status": "succeeded",
  "stage": "done",
  "progress": 100,
  "oneFileSystem": true,
  "followSymlinks": false,
  "maxDepth": 3,
  "userFilter": null,
  "minFileSize": 0,
  "extension": ".log",
  "nodeCount": 812,
  "truncated": false,
  "incomplete": false,
  "crossDeviceCount": 0,
  "deletedOpenBytes": 0,
  "deletedOpenCount": 0,
  "nodes": [
    {
      "id": "5f2a…", "path": "/data", "parentPath": null, "name": "data", "kind": "dir",
      "allocatedBytes": 10737418240, "apparentBytes": 12884901888,
      "fileCount": 1200, "directoryCount": 80, "uid": 0, "gid": 0, "inode": 256,
      "hardlinkDuplicateOf": null, "deletedOpen": false,
      "unscanned": false, "incomplete": false, "error": null,
      "modifiedAt": "2026-10-30T08:00:00Z"
    }
  ],
  "largestFiles": [],
  "summaryByUid": [
    {"uid": 1001, "allocatedBytes": 10737418240, "apparentBytes": 12884901888,
     "fileCount": 1200, "directoryCount": 80, "incomplete": false}
  ],
  "error": null,
  "createdAt": "...", "startedAt": "...", "finishedAt": "..."
}
```

`status` 取值 `queued` / `running` / `succeeded` / `failed` / `cancelled` / `interrupted`。
应用重启时未完成的扫描标记为 `interrupted`。
`allocatedBytes` 为 `st_blocks*512`，`apparentBytes` 为 `st_size`；硬链接按 inode 去重并标记
`hardlinkDuplicateOf`；目录聚合值包含全部已扫描后代，`unscanned=true` 或权限错误会使
`incomplete=true`。

查询与操作：

- `GET /api/storage/scans/{scanId}`：返回扫描对象（含已完成的结果）。
- `GET /api/storage/scans/{scanId}/children?path=/data/logs`：从已完成结果中取直接子节点，
  路径必须在扫描挂载点内（否则 `422 path_not_allowed`），不存在返回 `404 scan_path_not_found`。
- `POST /api/storage/scans/{scanId}/cancel`：排队中直接取消；运行中终止远端扫描。
  已结束返回 `409 scan_finished`。
- `GET /api/storage/scans/{scanId}/events`：SSE。

SSE 事件：

```
event: progress
data: {"type":"progress","scanId":"…","status":"running","stage":"scanning","progress":35,"at":"…"}

event: nodes
data: {"type":"nodes","scanId":"…","offset":0,"chunk":[{"id":"…","path":"/data",…}]}

event: done
data: {"type":"done","scanId":"…","status":"succeeded","progress":100,"error":null,
       "nodeCount":812,"truncated":false,"incomplete":false,"summary":[…],"at":"…"}
```

节点分块大小 200；扫描结果最多保留 3000 个节点（超出时 `truncated=true`）。

### 8.6 阶段三错误码

| HTTP | code | 说明 |
| --- | --- | --- |
| 400 | `server_disabled` | 主机已停用，不能创建扫描 |
| 404 | `storage_scan_not_found` | 扫描不存在 |
| 404 | `scan_path_not_found` | 扫描结果中不存在该路径 |
| 404 | `user_not_found` | 可登录用户不存在或不可见 |
| 409 | `scan_limit_reached` | 该主机已有进行中的扫描 |
| 409 | `scan_finished` | 扫描已结束，无法取消 |
| 409 | `scan_not_finished` | 扫描未完成，无法浏览子目录 |
| 422 | `mount_not_allowed` | 挂载点不在 allowlist 内 |
| 422 | `path_not_allowed` | 路径越界或含非法路径段 |
| 502 | `mounts_unavailable` | 无法读取挂载点清单（fail closed） |
| 502 | `users_unavailable` | 无法读取用户清单 |
| 503 | `observability_unavailable` | 服务关闭中 |

## 9. 阶段四接口（账户生命周期）

统一约定：

- 所有写操作需要远端 sudo；未保存运维 sudo 密码时返回 `409 sudo_password_missing`，
  前端据此提示前往「运维 sudo 密码」卡片填写。
- 请求模型拒绝未知字段；生成的**目标账户密码永不落库**，只能通过一次性 `reveal` 接口取回。
- 迁移、批次与空间分配都写入审计，审计只记录动作、目标、结果与错误摘要。

### 9.1 密码生成规则

`GET /api/accounts/generator` 返回当前规则、是否已配置、脱敏示例、一致性检查与系统下限：

```json
{
  "configured": true,
  "rules": {
    "length": 20, "minUpper": 1, "minLower": 1, "minDigits": 1, "minSymbols": 1,
    "symbols": "!#%+-_=.:?@^~*", "excludeAmbiguous": true, "excludeChars": ""
  },
  "effectiveLength": 20,
  "adjustments": [
    {"field": "length", "label": "长度", "requested": 10, "applied": 14, "reason": "系统 PAM 策略更严格"}
  ],
  "example": "Xy••••••••••••••••••",
  "checks": [
    {"code": "length_sum", "label": "各类最少字符数之和不超过总长度", "ok": true, "message": "4 / 20", "blocking": true}
  ],
  "floor": {"minLength": 14, "minUpper": 1, "minDigits": 1, "minClasses": 0,
            "maxRepeat": 0, "maxSequence": 0, "maxClassRepeat": 0,
            "rejectUsername": false, "source": "conf", "notes": []},
  "serverId": 1,
  "updatedAt": "2026-10-30T12:00:00Z"
}
```

- `PUT /api/accounts/generator`：保存全局默认规则（不影响已创建账户）。
- `POST /api/accounts/generator/preview`：`{"rules": {...}, "serverId": 1}`，
  用未保存规则生成脱敏示例与检查结果；规则冲突返回 `422 validation_failed`。

### 9.2 系统密码策略

`GET /api/servers/{serverId}/accounts/policy?refresh=false`

```json
{
  "serverId": 1,
  "collectedAt": "2026-10-30T12:00:00Z",
  "status": "ok",
  "error": null,
  "os": {"id": "ubuntu", "versionId": "24.04", "prettyName": "Ubuntu 24.04 LTS"},
  "fields": [
    {"key": "minlen", "label": "最小长度", "value": 12, "origin": "conf",
     "writable": true, "min": 8, "max": 128, "reason": null},
    {"key": "retry", "label": "认证重试次数", "value": 3, "origin": "pam",
     "writable": true, "min": 1, "max": 10, "reason": null}
  ],
  "capabilities": {
    "pwqualityConf": true, "pwqualityModule": true, "pwqualityModuleEditable": true,
    "pamRetryEditable": true, "pwscore": true, "chage": true, "useradd": true,
    "rsync": true, "dropInDir": false,
    "writableFields": ["minlen", "retry"], "readOnly": false, "reasons": []
  },
  "managedLines": ["password requisite pam_pwquality.so retry=3"],
  "unknownKeys": ["enforcing = 1"],
  "loginDefs": {"PASS_MIN_LEN": "8", "ENCRYPT_METHOD": "yescrypt"},
  "notes": ["minlen 由 PAM 行内参数决定，修改会改 common-password"],
  "floor": {"...": "同 9.1"},
  "generator": {"...": "同 9.1"}
}
```

- `origin` 取值 `pam`/`conf`/`default`：PAM 行内参数优先级高于 `pwquality.conf`。
- `POST /api/servers/{serverId}/accounts/policy/preview`：`{"targets": {"minlen": 14}}`
  返回 `fields`（差异）、`diff`（unified diff）、`blockers`、`warnings`、`applicable`；
  只读字段进入 `blockers`，越界值返回 `422 validation_failed`。
- `PUT /api/servers/{serverId}/accounts/policy`：`{"targets": {...}, "confirm": true}`；
  备份 → 写入 → 重新读取比对 → 行为验证（`pwscore`），失败自动恢复并返回
  `500 policy_validation_failed`。未确认返回 `422 confirmation_required`。
- `GET /api/servers/{serverId}/accounts/policy/changes`：最近的修改记录。
- `POST /api/servers/{serverId}/accounts/policy/changes/{changeId}/restore`：恢复该次修改前的文件。

### 9.3 批量新建用户

`POST /api/servers/{serverId}/account-batches`

```json
{
  "users": [
    {"login": "alice", "comment": "Alice", "shell": "/bin/bash", "groups": [], "uid": null, "password": null},
    {"login": "bob", "password": "Str0ng!Password#2026"}
  ],
  "rules": null,
  "confirm": true
}
```

响应 `201` 为批次对象：

```json
{
  "id": "3f2c…", "serverId": 1, "serverAlias": "lab-01",
  "status": "queued", "stage": "queued", "progress": 0,
  "results": [], "error": null, "hasGeneratedPasswords": true,
  "createdAt": "…", "startedAt": null, "finishedAt": null
}
```

- `results[]` 逐账户给出 `{login, status, error, uid, home, passwordGenerated}`。
- `POST /api/account-batches/{batchId}/reveal`：一次性返回 `passwords`（登录名 → 明文），
  读取后置空；再次调用返回 `409 passwords_unavailable`。
- `GET /api/account-batches/{batchId}`、`GET /api/servers/{id}/account-batches`（列表）、
  `POST /api/account-batches/{batchId}/cancel`（取消未执行账户）、
  `GET /api/account-batches/{batchId}/events`（SSE）。
- 手工密码不满足合并后的规则返回 `422 password_policy_violation`。

### 9.4 用户目录迁移

`POST /api/servers/{serverId}/home-migrations/preflight`

```json
{"login": "zhangsan", "targetRoot": "/data2/users"}
```

返回 `paths`（`source`/`targetRoot`/`target`/`staging`/`backup`）、`counts`、`space`、
`checks[]`（`code`/`label`/`ok`/`message`/`blocking`）与 `allowed`。只读，不需要 sudo。

`POST /api/servers/{serverId}/home-migrations`：`{login, targetRoot, safetyMarginPercent,
fullChecksum, retentionDays, confirm}`，预检未通过返回 `422 preflight_blocked`。

迁移对象：

```json
{
  "id": "ab12…", "serverId": 1, "login": "zhangsan", "uid": 1001, "gid": 1001,
  "status": "running", "stage": "copy", "stageIndex": 4,
  "stages": ["preflight","space_check","freeze","rename","copy","verify","switch","restore","observe"],
  "progress": 47,
  "paths": {"source": "/home/zhangsan", "targetRoot": "/data2/users",
            "target": "/data2/users/zhangsan",
            "staging": "/data2/users/zhangsan.labops-staging-abcd1234",
            "backup": "/home/zhangsan.labops-backup-20261030-120000-abcd1234"},
  "counts": {"files": 1200, "dirs": 80, "apparentBytes": 0, "allocatedBytes": 0, "inodes": 0},
  "space": {"requiredBytes": 0, "freeBytes": 0, "ok": true},
  "checks": [], "result": {}, "error": null,
  "accountStatusBefore": "P", "backupRetentionDays": 7,
  "rollbackAvailable": true, "cleanupAvailable": false, "cancelRequested": false,
  "createdAt": "…", "startedAt": "…", "finishedAt": null, "cleanedAt": null
}
```

- `GET /api/home-migrations/{id}`、`GET /api/servers/{id}/home-migrations`（列表）。
- `POST /api/home-migrations/{id}/cancel`：在当前阶段结束后停止；已结束返回 `409 migration_finished`。
- `POST /api/home-migrations/{id}/resume`：先核对远端状态再决定继续阶段；
  状态无法安全判定时返回 `409 migration_state_ambiguous`。
- `POST /api/home-migrations/{id}/rollback`：`{"discardNewData": false}`；
  已建立链接且新位置有数据时必须显式传 `true`。
- `POST /api/home-migrations/{id}/cleanup`：`{"confirmPath": "<备份路径>"}`；
  路径不一致返回 `422 cleanup_confirmation_mismatch`；只在 `succeeded` + `observe` 阶段可用。
- `GET /api/home-migrations/{id}/events`：SSE（`progress` / `done`）。

### 9.5 新空间分配

- `GET|PUT /api/servers/{serverId}/storage-settings`：
  `{serverId, dataRoot, migrationRoot, defaultDataRoot: "/data2", defaultMigrationRoot: "/data2/users"}`。
- `POST /api/servers/{serverId}/datasets`：
  `{"login": "zhangsan", "linkName": "data2_disk", "dataRoot": "/data2", "reuseExisting": false, "confirm": true}`
  → `{id, serverId, login, uid, gid, dataRoot, dataPath, linkPath, status, writable, error, createdAt}`。
  数据盘不是本地 ext4 返回 `422 filesystem_not_supported`；系统账户返回 `422 system_account`。
- `GET /api/servers/{serverId}/datasets`：已分配清单。
- `POST /api/datasets/{datasetId}/unlink`：只删除符号链接，数据目录保留。

### 9.6 阶段四错误码

| HTTP | code | 说明 |
| --- | --- | --- |
| 409 | `sudo_password_missing` | 未保存运维 sudo 密码 |
| 422 | `confirmation_required` | 写操作缺少确认 |
| 422 | `validation_failed` | 规则或策略字段不合法 |
| 422 | `password_policy_violation` | 手工密码低于规则下限 |
| 422 | `preflight_blocked` | 迁移预检未通过 |
| 422 | `filesystem_not_supported` | 非本地 ext4 |
| 422 | `cleanup_confirmation_mismatch` | 清理确认路径与备份路径不一致 |
| 409 | `passwords_unavailable` | 生成的密码已读取或已过期 |
| 409 | `processes_remaining` | 冻结后仍有该 UID 的进程 |
| 409 | `verify_failed` / `checksum_failed` / `space_delta_anomaly` | 迁移校验失败 |
| 409 | `functional_test_failed` | 迁移后功能验证失败（已自动回退） |
| 409 | `migration_state_ambiguous` | 远端状态无法安全判定 |
| 409 | `migration_finished` / `batch_finished` | 作业已结束 |
| 500 | `policy_validation_failed` | 策略写入后验证失败（已恢复备份） |
