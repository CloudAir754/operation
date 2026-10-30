import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";
import { requestPath } from "../test/fetchMock";
import { installHostDetailMock, renderHostDetail } from "../test/hostDetailMock";
import { installEventSourceMock } from "../test/mockEventSource";
import { makeStorageScan } from "../test/observabilityFixtures";

describe("用户 tab", () => {
  beforeEach(() => {
    installEventSourceMock();
  });

  it("renders the three-state sudo badges without collapsing unknown to false", async () => {
    installHostDetailMock();
    renderHostDetail("/hosts/1?tab=users");

    expect(await screen.findByTestId("user-row-labuser")).toBeInTheDocument();

    expect(screen.getByTestId("sudo-effective-labuser")).toHaveTextContent("可用");
    expect(screen.getByTestId("sudo-effective-alice")).toHaveTextContent("不可用");
    const unknown = screen.getByTestId("sudo-effective-bob");
    expect(unknown).toHaveTextContent("未知");
    expect(unknown).not.toHaveTextContent("不可用");

    expect(screen.getByTestId("sudo-group-labuser")).toHaveTextContent("组成员");
    expect(screen.getByTestId("sudo-group-bob")).toHaveTextContent("未知");
    expect(screen.getByTestId("sudo-policy-alice")).toHaveTextContent("策略存在");
    expect(screen.getByTestId("sudo-policy-bob")).toHaveTextContent("未知");

    expect(screen.getByTestId("users-notes")).toHaveTextContent(
      "sudo 有效权限仅对当前 SSH 用户执行非交互验证",
    );
  });

  it("refetches with includeSystem=true when the toggle is enabled", async () => {
    const user = userEvent.setup();
    const { requests } = installHostDetailMock();
    renderHostDetail("/hosts/1?tab=users");

    expect(await screen.findByTestId("user-row-labuser")).toBeInTheDocument();
    expect(screen.queryByTestId("user-row-svc")).not.toBeInTheDocument();

    await user.click(screen.getByRole("checkbox", { name: "包含系统账号" }));

    await waitFor(() => {
      expect(
        requests.some(
          (request) =>
            requestPath(request.url) === "/api/servers/1/users?includeSystem=true",
        ),
      ).toBe(true);
    });
    expect(await screen.findByTestId("user-row-svc")).toBeInTheDocument();
    expect(screen.getByText("包含系统账号", { selector: ".badge" })).toBeInTheDocument();
  });

  it("renders N/A for users whose home storage could not be collected", async () => {
    installHostDetailMock();
    renderHostDetail("/hosts/1?tab=users");

    const alice = await screen.findByTestId("user-row-alice");
    expect(within(alice).getByTestId("home-storage")).toHaveTextContent("N/A");

    const bob = screen.getByTestId("user-row-bob");
    expect(within(bob).getByTestId("home-storage")).toHaveTextContent("已截断");
    expect(within(bob).getByTestId("home-storage")).toHaveTextContent("不完整");

    const labuser = screen.getByTestId("user-row-labuser");
    expect(within(labuser).getByTestId("home-storage")).toHaveTextContent("12000 文件");
    expect(within(labuser).getByText("sudo")).toBeInTheDocument();
    expect(within(labuser).getByText("docker")).toBeInTheDocument();
  });

  it("creates a per-UID scan, switches to 磁盘空间 and adopts the new scan", async () => {
    const user = userEvent.setup();
    const { requests } = installHostDetailMock({
      getScan: (scanId) =>
        makeStorageScan({
          scanId,
          status: "running",
          stage: "scanning",
          progress: 10,
          nodes: [],
          largestFiles: [],
          summaryByUid: [],
          nodeCount: 0,
          finishedAt: null,
        }),
    });
    renderHostDetail("/hosts/1?tab=users");

    await screen.findByTestId("user-row-labuser");
    await user.click(within(screen.getByTestId("user-row-labuser")).getByRole("button", { name: "扫描空间" }));

    await waitFor(() => {
      const post = requests.find(
        (request) =>
          request.method === "POST" &&
          requestPath(request.url) === "/api/servers/1/users/storage-scan",
      );
      expect(post?.body).toMatchObject({
        uid: 1001,
        mount: "/",
        maxDepth: 8,
        minFileSize: 0,
        includeDeletedOpen: true,
      });
    });

    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "磁盘空间" })).toHaveAttribute(
        "aria-selected",
        "true",
      );
    });
    expect(await screen.findByTestId("storage-tab")).toBeInTheDocument();
    await waitFor(() => {
      expect(
        requests.some(
          (request) =>
            request.method === "GET" &&
            requestPath(request.url) === "/api/storage/scans/scan-user",
        ),
      ).toBe(true);
    });
    expect(await screen.findByTestId("storage-progress")).toBeInTheDocument();
    expect(screen.getByTestId("storage-progress-status")).toHaveTextContent("扫描中");
  });
});
