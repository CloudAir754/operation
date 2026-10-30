import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { requestPath } from "../test/fetchMock";
import { installHostDetailMock, renderHostDetail } from "../test/hostDetailMock";
import { installEventSourceMock, MockEventSource } from "../test/mockEventSource";
import { makeStorageNodes } from "../test/observabilityFixtures";

function startScanButton(): HTMLElement {
  return screen.getByRole("button", { name: "开始扫描" });
}

async function mountReady(): Promise<HTMLElement> {
  return screen.findByLabelText("挂载点");
}

async function startScan(user: ReturnType<typeof userEvent.setup>): Promise<MockEventSource> {
  await mountReady();
  await user.click(startScanButton());
  await waitFor(() => expect(MockEventSource.instances.length).toBeGreaterThan(0));
  return MockEventSource.latest();
}

describe("磁盘空间 tab", () => {
  beforeEach(() => {
    installEventSourceMock();
  });

  it("loads mounts into the selector with filesystem and known size", async () => {
    installHostDetailMock();
    renderHostDetail("/hosts/1?tab=storage");

    const select = await mountReady();
    expect(within(select).getByRole("option", { name: /^\/ · ext4/ })).toBeInTheDocument();
    expect(within(select).getByRole("option", { name: /^\/data · ext4/ })).toBeInTheDocument();
    expect(select).toHaveValue("/");
    expect(screen.getByRole("checkbox", { name: /单文件系统/ })).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /跟随符号链接/ })).not.toBeChecked();
    expect(screen.getByRole("checkbox", { name: /已删除但仍打开/ })).toBeChecked();
  });

  it("posts the documented request body when starting a scan", async () => {
    const user = userEvent.setup();
    const { requests } = installHostDetailMock();
    renderHostDetail("/hosts/1?tab=storage");

    await mountReady();
    await user.selectOptions(screen.getByLabelText("挂载点"), "/data");
    await user.selectOptions(screen.getByLabelText("最大深度"), "5");
    await user.clear(screen.getByLabelText("最小文件大小"));
    await user.type(screen.getByLabelText("最小文件大小"), "2");
    await user.selectOptions(screen.getByLabelText("文件大小单位"), "MiB");
    await user.type(screen.getByLabelText("扩展名过滤"), "log");
    await user.click(screen.getByRole("checkbox", { name: /跟随符号链接/ }));
    expect(screen.getByText(/跟随符号链接可能进入其他文件系统/)).toBeInTheDocument();
    await user.click(startScanButton());

    await waitFor(() => {
      const post = requests.find(
        (request) =>
          request.method === "POST" &&
          requestPath(request.url) === "/api/servers/1/storage/scans",
      );
      expect(post).toBeDefined();
      expect(post?.body).toMatchObject({
        mount: "/data",
        oneFileSystem: true,
        followSymlinks: true,
        maxDepth: 5,
        minFileSize: 2 * 1024 * 1024,
        extension: "log",
        modifiedAfter: null,
        includeDeletedOpen: true,
      });
    });
  });

  it("streams SSE progress into the progress bar and label", async () => {
    const user = userEvent.setup();
    installHostDetailMock();
    renderHostDetail("/hosts/1?tab=storage");

    const source = await startScan(user);
    await waitFor(() =>
      expect(source.url).toContain("/api/storage/scans/scan-new/events"),
    );

    act(() => {
      source.emitOpen();
      source.emit("progress", {
        type: "progress",
        scanId: "scan-new",
        status: "running",
        stage: "scanning",
        progress: 42,
      });
    });

    expect(await screen.findByTestId("storage-progress-value")).toHaveTextContent("42%");
    expect(screen.getByRole("progressbar", { name: "扫描进度" })).toHaveAttribute(
      "aria-valuenow",
      "42",
    );
    expect(screen.getByTestId("storage-progress-status")).toHaveTextContent("扫描目录");
    expect(screen.getByTestId("storage-progress-status")).toHaveTextContent("实时连接已建立");
  });

  it("renders streamed nodes as treemap rectangles with labels", async () => {
    const user = userEvent.setup();
    installHostDetailMock();
    renderHostDetail("/hosts/1?tab=storage");

    const source = await startScan(user);
    act(() => {
      source.emit("nodes", {
        type: "nodes",
        scanId: "scan-new",
        offset: 0,
        chunk: makeStorageNodes(),
      });
    });

    const treemap = await screen.findByTestId("treemap");
    expect(within(treemap).getAllByTestId("treemap-cell")).toHaveLength(4);
    expect(within(treemap).getByText("data")).toBeInTheDocument();
    expect(within(treemap).getByText("home")).toBeInTheDocument();
    expect(within(treemap).getByText("tmp")).toBeInTheDocument();
    expect(treemap.querySelectorAll("rect").length).toBeGreaterThanOrEqual(4);
  });

  it("refetches the scan on done and renders summary, largest files and UID summary", async () => {
    const user = userEvent.setup();
    const { requests } = installHostDetailMock();
    renderHostDetail("/hosts/1?tab=storage");

    const source = await startScan(user);
    act(() => {
      source.emit("nodes", {
        type: "nodes",
        scanId: "scan-new",
        offset: 0,
        chunk: makeStorageNodes(),
      });
      source.emit("done", {
        type: "done",
        scanId: "scan-new",
        status: "succeeded",
        progress: 100,
        error: null,
        nodeCount: 7,
        truncated: true,
        incomplete: true,
        summary: [
          {
            uid: 1001,
            allocatedBytes: 900_000_000,
            apparentBytes: 950_000_000,
            fileCount: 900,
            directoryCount: 40,
            incomplete: false,
          },
        ],
      });
    });

    await waitFor(() => {
      expect(
        requests.some(
          (request) =>
            request.method === "GET" &&
            requestPath(request.url) === "/api/storage/scans/scan-new",
        ),
      ).toBe(true);
    });

    const result = await screen.findByTestId("storage-result");
    expect(within(result).getByTestId("storage-summary")).toHaveTextContent("跨设备");
    expect(within(result).getByTestId("storage-incomplete")).toHaveTextContent("结果不完整");
    expect(within(result).getByTestId("storage-truncated")).toHaveTextContent("结果已截断");
    expect(within(result).getByTestId("storage-largest-files")).toHaveTextContent("app.log");
    expect(within(result).getByTestId("storage-largest-files")).toHaveTextContent("core.dump");
    expect(within(result).getByTestId("storage-uid-summary")).toHaveTextContent("1001");
    expect(within(result).getByTestId("treemap")).toBeInTheDocument();
  });

  it("cancels a running scan and shows the reason", async () => {
    const user = userEvent.setup();
    const { requests } = installHostDetailMock();
    renderHostDetail("/hosts/1?tab=storage");

    await startScan(user);
    await user.click(screen.getByRole("button", { name: "取消扫描" }));

    await waitFor(() => {
      expect(
        requests.some(
          (request) =>
            request.method === "POST" &&
            requestPath(request.url) === "/api/storage/scans/scan-new/cancel",
        ),
      ).toBe(true);
    });
    expect(await screen.findByText(/扫描已取消/)).toBeInTheDocument();
    expect(screen.getByText(/已取消（尚未开始）/)).toBeInTheDocument();
  });

  it("drills into a directory, expands children lazily and navigates back", async () => {
    const user = userEvent.setup();
    const { requests } = installHostDetailMock();
    renderHostDetail("/hosts/1?tab=storage");

    const source = await startScan(user);
    act(() => {
      source.emit("nodes", {
        type: "nodes",
        scanId: "scan-new",
        offset: 0,
        chunk: makeStorageNodes(),
      });
      source.emit("done", {
        type: "done",
        scanId: "scan-new",
        status: "succeeded",
        progress: 100,
        error: null,
        nodeCount: 7,
        truncated: false,
        incomplete: false,
      });
    });

    const result = await screen.findByTestId("storage-result");
    const treemap = within(result).getByTestId("treemap");
    await user.click(within(treemap).getByText("data"));

    await waitFor(() => {
      expect(within(treemap).getByText("logs")).toBeInTheDocument();
    });
    expect(within(treemap).getByText("app.log")).toBeInTheDocument();

    const nav = screen.getByRole("navigation", { name: "空间树路径" });
    expect(within(nav).getByRole("button", { name: "data" })).toHaveAttribute(
      "aria-current",
      "location",
    );

    // Expandable row fetches children through the documented endpoint.
    await user.click(screen.getByRole("button", { name: "展开 logs" }));
    await waitFor(() => {
      expect(
        requests.some(
          (request) =>
            requestPath(request.url) ===
            "/api/storage/scans/scan-new/children?path=%2Fdata%2Flogs",
        ),
      ).toBe(true);
    });

    await user.click(within(nav).getByRole("button", { name: "/" }));
    await waitFor(() => {
      expect(within(treemap).getByText("home")).toBeInTheDocument();
    });
  });

  it("keeps the previous result visible while a new scan runs", async () => {
    const user = userEvent.setup();
    installHostDetailMock();
    renderHostDetail("/hosts/1?tab=storage");

    const source = await startScan(user);
    act(() => {
      source.emit("done", {
        type: "done",
        scanId: "scan-new",
        status: "succeeded",
        progress: 100,
        error: null,
        nodeCount: 7,
        truncated: false,
        incomplete: false,
      });
    });
    const result = await screen.findByTestId("storage-result");
    expect(result).toBeInTheDocument();

    await user.click(startScanButton());
    const previous = await screen.findByTestId("storage-previous-result");
    expect(previous).toHaveTextContent("上次扫描结果");
    expect(within(previous).getByTestId("storage-largest-files")).toHaveTextContent("app.log");
    expect(screen.getByTestId("storage-progress")).toBeInTheDocument();
  });
});
