# LabOpsPanel 前端

LabOpsPanel 的浏览器客户端：Windows 本机 Web 控制台，通过后端 SSH 能力管理 Linux 主机。
技术栈为 React 18、TypeScript（strict）、Vite 6、React Router v6 和纯 CSS；组件与依赖保持最小化。

## 环境要求

- Node.js >= 20（本仓库使用 Node 24 验证）
- npm 11（随 Node 安装）
- 后端运行在 `http://127.0.0.1:8765`

## 启动

```powershell
# 1. 在仓库根目录启动后端（另开一个终端）
uv sync --extra dev
uv run uvicorn lab_ops_panel.app:app --reload --host 127.0.0.1 --port 8765

# 2. 安装前端依赖并启动开发服务器
cd frontend
npm ci          # 首次或依赖变更后；也可用 npm install
npm run dev
```

打开终端输出的地址（默认 `http://localhost:55173`）。Vite 会把 `/api` 代理到
`http://127.0.0.1:8765`，不启用 WebSocket；SSE 通过同一个 HTTP 代理工作。
如果后端端口不同，修改 [vite.config.ts](vite.config.ts) 中的 `apiTarget`。

## 常用命令

| 命令 | 说明 |
| --- | --- |
| `npm run dev` | 开发服务器（`/api` 代理到本机后端） |
| `npm run build` | 类型检查并生成生产构建到 `dist/` |
| `npm run preview` | 预览生产构建（同样代理 `/api`） |
| `npm run typecheck` | `tsc --noEmit`，零错误为通过 |
| `npm test` | Vitest + jsdom 单次运行全部测试 |
| `npm run test:watch` | 监听模式运行测试 |

构建产物入口为 `dist/index.html`，`base` 为 `/`。若由后端或其他静态服务器托管，
需要把未知路径回退到 `index.html`（前端使用 BrowserRouter）。

## 页面

| 路径 | 页面 | 主要接口 |
| --- | --- | --- |
| `/` | 主机总览 | `GET /api/servers`、`GET /api/ssh-config`、`POST /api/servers/import`、连接测试 |
| `/hosts/:id` | 主机详情 | `GET /api/servers/{id}/snapshot`、`GET /api/servers/{id}/logs`、`POST /api/tasks` |
| `/tasks` | 维护任务 | `GET /api/tasks`、`GET /api/tasks/{id}/events`（SSE）、取消任务 |
| `/hosts/:id/settings` | 主机设置 | `PATCH /api/servers/{id}`、`PUT/DELETE /api/servers/{id}/secret`、`GET /api/settings` |

接口契约见仓库根目录 `docs/requirements/api.md`。所有时间按 UTC ISO 8601 接收，
界面按本地时区显示；缺少权限或驱动的指标显示明确状态，GPU 不可用时显示 `N/A`，
不会伪造为 `0`。快照 `stale` 为真时显示“非实时缓存”标记。

## 目录结构

```text
src/
  api/          typed client、ApiError 映射、契约类型
  components/   布局、对话框、日志面板与通用 UI
  hooks/        usePolling / useTasks / useTaskEvents / usePageTitle
  lib/          格式化、中文标签、幂等键
  pages/        四个页面
  test/         Vitest setup、fetch/EventSource mock、fixtures
  styles.css    深色主题样式（唯一样式入口）
```

## 测试

```powershell
npm run typecheck
npm test
```

测试完全离线：`global.fetch` 与 `EventSource` 均被替换为确定性 mock，不访问真实后端，
不操作真实账户、SSH 配置或主机。覆盖 API 错误映射、主机卡片与导入结果、连接测试失败、
分区降级与 GPU `N/A`、非实时缓存、服务操作确认与幂等键、任务取消/SSE `done`、
sudo 密码设置与清除等关键路径。

## 安全说明

- 前端不持久化 sudo 密码；输入框提交后立即清空，页面只显示“已设置/未设置”。
- 写操作在确认对话框中展示目标主机、服务单元、风险等级与 sudo 要求，并携带
  `crypto.randomUUID()` 生成的幂等键。
- 面向本机回环地址使用，不包含登录或远程鉴权；请勿将 Vite 或后端暴露到局域网。
