import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { OverviewPage } from "./OverviewPage";
import type { ConnectionTestResult, ImportResult } from "../api/types";
import { installFetchMock, notFoundResponse, requestPath } from "../test/fetchMock";
import { makeHost, makeSshConfig } from "../test/fixtures";
import { renderRoute } from "../test/renderRoute";

interface OverviewOverrides {
  testResult?: ConnectionTestResult;
  importResult?: ImportResult;
}

function setupOverview(overrides: OverviewOverrides = {}) {
  const host = makeHost();
  const { requests } = installFetchMock(({ url, method }) => {
    const path = requestPath(url);
    if (method === "GET" && path === "/api/servers") {
      return { body: { servers: [host], count: 1 } };
    }
    if (method === "GET" && path === "/api/ssh-config") {
      return { body: makeSshConfig() };
    }
    if (method === "POST" && path === "/api/servers/import") {
      return {
        body:
          overrides.importResult ?? {
            imported: ["lab-cpu-02"],
            updated: [],
            skipped: [{ alias: "lab-old", reason: "配置中缺少 HostName" }],
            errors: [],
          },
      };
    }
    if (method === "POST" && path === "/api/servers/1/connection-test") {
      return {
        body:
          overrides.testResult ?? {
            ok: true,
            status: "ok",
            error: null,
            durationMs: 842,
            exitCode: 0,
            testedAt: "2026-10-25T12:00:00Z",
          },
      };
    }
    return notFoundResponse(method, path);
  });
  return { requests };
}

describe("OverviewPage", () => {
  it("renders host cards and the summary strip from the API", async () => {
    setupOverview();
    renderRoute(<OverviewPage />, { route: "/", path: "*" });

    expect(await screen.findByText("lab-gpu-01")).toBeInTheDocument();
    expect(screen.getByText("labadmin@10.0.0.11:22")).toBeInTheDocument();
    expect(screen.getByText("已启用")).toBeInTheDocument();
    expect(screen.getByText("已设置")).toBeInTheDocument();
    expect(screen.getByText(/GPU 训练节点/)).toBeInTheDocument();
    expect(screen.getByText("设备总数")).toBeInTheDocument();
    expect(screen.getByText("在线")).toBeInTheDocument();
    expect(screen.getByText("异常")).toBeInTheDocument();
    expect(screen.getByText("最近采集")).toBeInTheDocument();

    const detailsLink = screen.getByRole("link", { name: "查看详情" });
    expect(detailsLink).toHaveAttribute("href", "/hosts/1");
    expect(screen.getByRole("button", { name: "测试连接" })).toBeEnabled();
  });

  it("imports the selected SSH host and reports imported/skipped counts", async () => {
    const user = userEvent.setup();
    const { requests } = setupOverview();
    renderRoute(<OverviewPage />, { route: "/", path: "*" });

    await screen.findByText("lab-gpu-01");
    await user.click(screen.getByRole("button", { name: "导入 SSH 主机" }));

    expect(await screen.findByText("从 SSH 配置导入")).toBeInTheDocument();
    const importButton = await screen.findByRole("button", { name: "导入选中 (1)" });
    await user.click(importButton);

    const summary = await screen.findByText(/导入完成：新增 1 台，更新 0 台，跳过 1 台。/);
    expect(summary).toBeInTheDocument();
    expect(screen.getByText(/配置中缺少 HostName/)).toBeInTheDocument();

    const importRequest = requests.find(
      (request) =>
        request.method === "POST" && requestPath(request.url) === "/api/servers/import",
    );
    expect(importRequest).toBeDefined();
    expect(importRequest?.body).toEqual({
      aliases: ["lab-cpu-02"],
      pruneMissing: false,
    });
  });

  it("shows the failure reason when a connection test fails", async () => {
    const user = userEvent.setup();
    setupOverview({
      testResult: {
        ok: false,
        status: "failed",
        error: "认证失败：Permission denied (publickey)",
        durationMs: 842,
        exitCode: 255,
        testedAt: "2026-10-25T12:00:00Z",
      },
    });
    renderRoute(<OverviewPage />, { route: "/", path: "*" });

    await screen.findByText("lab-gpu-01");
    await user.click(screen.getByRole("button", { name: "测试连接" }));

    const failure = await screen.findByText(
      "连接失败：认证失败：Permission denied (publickey)",
    );
    expect(failure).toBeInTheDocument();
    expect(screen.queryByText("连接正常 · 842 ms")).not.toBeInTheDocument();
  });
});
