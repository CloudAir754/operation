import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { SettingsPage } from "./SettingsPage";
import { installFetchMock, notFoundResponse, requestPath } from "../test/fetchMock";
import { makeHost, makeSettings } from "../test/fixtures";
import { renderRoute } from "../test/renderRoute";

function setupSettings(initialHasPassword: boolean) {
  const host = makeHost({ hasSudoPassword: initialHasPassword });
  const { requests } = installFetchMock(({ url, method }) => {
    const path = requestPath(url);
    if (method === "GET" && path === "/api/servers") {
      return { body: { servers: [host], count: 1 } };
    }
    if (method === "GET" && path === "/api/servers/1/secret") {
      return {
        body: {
          serverId: 1,
          hasPassword: initialHasPassword,
          updatedAt: initialHasPassword ? "2026-10-25T12:00:00Z" : null,
        },
      };
    }
    if (method === "GET" && path === "/api/settings") {
      return { body: makeSettings() };
    }
    if (method === "PUT" && path === "/api/servers/1/secret") {
      return {
        body: {
          serverId: 1,
          hasPassword: true,
          updatedAt: "2026-10-25T12:05:00Z",
        },
      };
    }
    if (method === "DELETE" && path === "/api/servers/1/secret") {
      return { status: 204 };
    }
    return notFoundResponse(method, path);
  });
  return { requests };
}

function renderSettingsPage() {
  renderRoute(<SettingsPage />, {
    route: "/hosts/1/settings",
    path: "/hosts/:id/settings",
  });
}

describe("SettingsPage", () => {
  it("saves a new sudo password with PUT and shows 已设置 without echoing it", async () => {
    const user = userEvent.setup();
    const { requests } = setupSettings(false);
    renderSettingsPage();

    const status = await screen.findByTestId("sudo-status");
    expect(within(status).getByText("未设置")).toBeInTheDocument();

    const input = screen.getByLabelText("新 sudo 密码");
    await user.type(input, "s3cret");
    await user.click(screen.getByRole("button", { name: "设置密码" }));

    await within(status).findByText("已设置");

    const putRequest = requests.find(
      (request) =>
        request.method === "PUT" && requestPath(request.url) === "/api/servers/1/secret",
    );
    expect(putRequest).toBeDefined();
    expect(putRequest?.body).toEqual({ password: "s3cret" });
    expect(input).toHaveValue("");
    expect(screen.queryByDisplayValue("s3cret")).not.toBeInTheDocument();
  });

  it("clears the sudo password through DELETE after confirmation", async () => {
    const user = userEvent.setup();
    const { requests } = setupSettings(true);
    renderSettingsPage();

    const status = await screen.findByTestId("sudo-status");
    expect(within(status).getByText("已设置")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "清除密码" }));

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("清除 sudo 密码")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "确认清除" }));

    await within(status).findByText("未设置");

    const deleteRequest = requests.find(
      (request) =>
        request.method === "DELETE" && requestPath(request.url) === "/api/servers/1/secret",
    );
    expect(deleteRequest).toBeDefined();
    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    });
  });

  it("renders the local security settings read-only", async () => {
    setupSettings(false);
    renderSettingsPage();

    expect(await screen.findByText("监听地址")).toBeInTheDocument();
    expect(screen.getByText("127.0.0.1:8765")).toBeInTheDocument();
    expect(screen.getByText("已关闭")).toBeInTheDocument();
    expect(screen.getByText(/256 KiB（262144 字节）/)).toBeInTheDocument();
    expect(screen.getByText("20 秒")).toBeInTheDocument();
    expect(
      screen.getByText(/任意命令入口保持关闭；所有远程变更仅通过结构化动作白名单执行。/),
    ).toBeInTheDocument();
  });
});
