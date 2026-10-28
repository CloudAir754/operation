import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { TasksPage } from "./TasksPage";
import { installFetchMock, notFoundResponse, requestPath } from "../test/fetchMock";
import { makeHost, makeTask } from "../test/fixtures";
import { installEventSourceMock, MockEventSource } from "../test/mockEventSource";
import { renderRoute } from "../test/renderRoute";

function setupTasks() {
  const running = makeTask();
  const succeeded = makeTask({
    id: "task-done",
    action: "collect_snapshot",
    actionLabel: "采集快照",
    status: "succeeded",
    progress: 100,
    exitCode: 0,
    error: null,
    outputTail: "snapshot collected",
    finishedAt: "2026-10-25T12:05:00Z",
    steps: [
      {
        seq: 1,
        name: "采集",
        status: "ok",
        exitCode: 0,
        outputTail: "",
        startedAt: "2026-10-25T12:04:59Z",
        finishedAt: "2026-10-25T12:05:00Z",
      },
    ],
  });
  const cancelled = makeTask({
    status: "cancelled",
    finishedAt: "2026-10-25T12:03:00Z",
  });

  const { requests } = installFetchMock(({ url, method }) => {
    const path = requestPath(url);
    if (method === "GET" && path === "/api/servers") {
      return { body: { servers: [makeHost()], count: 1 } };
    }
    if (method === "GET" && path.startsWith("/api/tasks?")) {
      return { body: { tasks: [running, succeeded], count: 2 } };
    }
    if (method === "POST" && path === "/api/tasks/task-running/cancel") {
      return { status: 202, body: { task: cancelled } };
    }
    return notFoundResponse(method, path);
  });

  return { requests, running, succeeded, cancelled };
}

function renderTasksPage() {
  renderRoute(<TasksPage />, { route: "/tasks", path: "/tasks" });
}

describe("TasksPage", () => {
  it("renders task statuses, progress and the selected task detail", async () => {
    installEventSourceMock();
    setupTasks();
    renderTasksPage();

    const table = await screen.findByRole("table", { name: "维护任务列表" });
    expect(within(table).getByText("执行中")).toBeInTheDocument();
    expect(within(table).getByText("成功")).toBeInTheDocument();
    expect(within(table).getByText("40%")).toBeInTheDocument();
    expect(within(table).getByText("100%")).toBeInTheDocument();
    expect(within(table).getByText("重启服务")).toBeInTheDocument();
    expect(within(table).getByText("采集快照")).toBeInTheDocument();

    const detailStatus = await screen.findByTestId("task-detail-status");
    expect(within(detailStatus).getByText("执行中")).toBeInTheDocument();
    expect(screen.getByTestId("task-output")).toHaveTextContent("restarting nginx.service...");
    expect(screen.getByText("预检")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "取消任务" })).toBeEnabled();
  });

  it("cancels a running task through the API and reflects the new status", async () => {
    const user = userEvent.setup();
    installEventSourceMock();
    const { requests } = setupTasks();
    renderTasksPage();

    const detailStatus = await screen.findByTestId("task-detail-status");
    expect(within(detailStatus).getByText("执行中")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "取消任务" }));

    await within(detailStatus).findByText("已取消");

    const cancelRequest = requests.find(
      (request) =>
        request.method === "POST" &&
        requestPath(request.url) === "/api/tasks/task-running/cancel",
    );
    expect(cancelRequest).toBeDefined();
    expect(screen.queryByRole("button", { name: "取消任务" })).not.toBeInTheDocument();
  });

  it("applies a done SSE event to the selected task and closes the stream", async () => {
    installEventSourceMock();
    setupTasks();
    renderTasksPage();

    const detailStatus = await screen.findByTestId("task-detail-status");
    expect(within(detailStatus).getByText("执行中")).toBeInTheDocument();

    await waitFor(() => {
      expect(MockEventSource.instances.length).toBeGreaterThan(0);
    });
    const source = MockEventSource.latest();
    expect(source.url).toBe("/api/tasks/task-running/events");

    act(() => {
      source.emit("done", {
        type: "done",
        taskId: "task-running",
        status: "succeeded",
        progress: 100,
        exitCode: 0,
        error: null,
      });
    });

    await within(detailStatus).findByText("成功");
    expect(screen.getByTestId("task-detail-progress")).toHaveTextContent("100.0%");
    expect(source.closed).toBe(true);
    expect(screen.queryByRole("button", { name: "取消任务" })).not.toBeInTheDocument();
  });
});
