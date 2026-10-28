import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { HostDetailPage } from "./HostDetailPage";
import type { LogsResponse, Snapshot, Task } from "../api/types";
import { installFetchMock, notFoundResponse, requestPath } from "../test/fetchMock";
import { makeHost, makeSnapshot, makeTask } from "../test/fixtures";
import { renderRoute } from "../test/renderRoute";

interface HostDetailSetup {
  snapshot?: Snapshot;
  task?: Task;
  logs?: LogsResponse;
}

function setupHostDetail({ snapshot = makeSnapshot(), task, logs }: HostDetailSetup = {}) {
  const logResponse: LogsResponse = logs ?? {
    serverId: 1,
    unit: "nginx.service",
    collectedAt: "2026-10-25T12:00:00Z",
    lines: ["Oct 25 12:00:00 host nginx[1]: started"],
    exitCode: 0,
    truncated: false,
    error: null,
    command: "journalctl -u nginx.service -n 200 --no-pager",
  };

  const { requests } = installFetchMock(({ url, method }) => {
    const path = requestPath(url);
    if (method === "GET" && path === "/api/servers") {
      return { body: { servers: [makeHost()], count: 1 } };
    }
    if (
      method === "GET" &&
      (path === "/api/servers/1/snapshot" || path === "/api/servers/1/snapshot?refresh=true")
    ) {
      return { body: snapshot };
    }
    if (method === "POST" && path === "/api/tasks") {
      return {
        status: 201,
        body:
          task ??
          makeTask({
            action: "service_stop",
            actionLabel: "停止服务",
            status: "queued",
            progress: 0,
          }),
      };
    }
    if (method === "GET" && path.startsWith("/api/servers/1/logs")) {
      return { body: logResponse };
    }
    return notFoundResponse(method, path);
  });

  return { requests };
}

function renderHostDetail() {
  renderRoute(<HostDetailPage />, { route: "/hosts/1", path: "/hosts/:id" });
}

describe("HostDetailPage", () => {
  it("renders section status notices, GPU N/A and the stale cache badge", async () => {
    setupHostDetail({
      snapshot: makeSnapshot({
        stale: true,
        data: {
          cpu: {
            status: "permission_denied",
            error: "需要 sudo 权限读取 CPU 信息",
            data: null,
          },
          gpu: { status: "unavailable", error: "nvidia-smi not found", data: null },
        },
      }),
    });
    renderHostDetail();

    const cpuSection = await screen.findByTestId("cpu-section");
    const cpuNotice = within(cpuSection).getByTestId("cpu-section-notice");
    expect(within(cpuNotice).getByText("权限不足")).toBeInTheDocument();
    expect(within(cpuNotice).getByText(/需要 sudo 权限读取 CPU 信息/)).toBeInTheDocument();

    expect(screen.getByTestId("gpu-value")).toHaveTextContent("N/A");
    expect(screen.getByTestId("gpu-value")).not.toHaveTextContent("0");
    expect(
      within(screen.getByTestId("gpu-section")).getByText(/nvidia-smi not found/),
    ).toBeInTheDocument();

    expect(screen.getByText("非实时缓存")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "lab-gpu-01" })).toBeInTheDocument();

    const diskBar = within(screen.getByTestId("disks-section")).getByRole("progressbar", {
      name: "/ 使用率",
    });
    expect(diskBar).toHaveAttribute("aria-valuenow", "40");
    expect(screen.getByRole("progressbar", { name: "内存使用率" })).toBeInTheDocument();
  });

  it("opens a confirmation dialog and posts confirm:true with an idempotency key", async () => {
    const user = userEvent.setup();
    const task = makeTask({
      action: "service_stop",
      actionLabel: "停止服务",
      status: "queued",
      progress: 0,
      outputTail: "",
    });
    const { requests } = setupHostDetail({ task });
    renderHostDetail();

    const stopButton = await screen.findByRole("button", { name: "停止 nginx.service" });
    await user.click(stopButton);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("目标主机")).toBeInTheDocument();
    expect(within(dialog).getByText("lab-gpu-01")).toBeInTheDocument();
    expect(within(dialog).getByText("服务单元")).toBeInTheDocument();
    expect(within(dialog).getByText("nginx.service")).toBeInTheDocument();
    expect(within(dialog).getByText("中风险")).toBeInTheDocument();
    expect(within(dialog).getByText(/需要 sudo/)).toBeInTheDocument();
    expect(within(dialog).getByText(/已配置 sudo 密码/)).toBeInTheDocument();

    await user.click(within(dialog).getByRole("button", { name: "确认执行" }));

    expect(await screen.findByText(/已创建任务：停止服务/)).toBeInTheDocument();

    const createRequest = requests.find(
      (request) => request.method === "POST" && requestPath(request.url) === "/api/tasks",
    );
    expect(createRequest).toBeDefined();
    expect(createRequest?.body).toMatchObject({
      serverId: 1,
      action: "service_stop",
      params: { unit: "nginx.service" },
      confirm: true,
    });
    const body = createRequest?.body as { idempotencyKey?: string };
    expect(body.idempotencyKey).toMatch(
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i,
    );
  });

  it("loads 200 log lines for a service when the log panel opens", async () => {
    const user = userEvent.setup();
    const { requests } = setupHostDetail();
    renderHostDetail();

    const logButton = await screen.findByRole("button", { name: "查看日志 nginx.service" });
    await user.click(logButton);

    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    expect(await screen.findByText(/Oct 25 12:00:00 host nginx\[1\]: started/)).toBeInTheDocument();

    await waitFor(() => {
      const logRequest = requests.find((request) =>
        requestPath(request.url).startsWith("/api/servers/1/logs"),
      );
      expect(logRequest).toBeDefined();
      expect(requestPath(logRequest?.url ?? "")).toBe(
        "/api/servers/1/logs?unit=nginx.service&lines=200",
      );
    });
  });
});
