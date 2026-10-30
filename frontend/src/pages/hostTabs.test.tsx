import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { installHostDetailMock, renderHostDetail, requestsTo } from "../test/hostDetailMock";

describe("HostDetailPage tabs", () => {
  it("opens the GPU tab directly from ?tab=gpu and keeps other tabs unmounted", async () => {
    const { requests } = installHostDetailMock();
    renderHostDetail("/hosts/1?tab=gpu");

    const gpuTab = await screen.findByRole("tab", { name: "GPU" });
    expect(gpuTab).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "概览" })).toHaveAttribute("aria-selected", "false");

    const panel = screen.getByRole("tabpanel");
    expect(panel).toHaveAttribute("aria-labelledby", "host-tab-gpu");
    expect(await screen.findByTestId("gpu-tab")).toBeInTheDocument();

    await waitFor(() => {
      expect(requestsTo(requests, "/api/servers/1/gpus").length).toBeGreaterThan(0);
    });
    // The GPU tab reuses the metrics ring buffer for its trend lines.
    expect(requestsTo(requests, "/api/servers/1/metrics").length).toBeGreaterThan(0);
    // Inactive tabs must not fetch.
    expect(requestsTo(requests, "/api/servers/1/mounts")).toHaveLength(0);
    expect(requestsTo(requests, "/api/servers/1/users")).toHaveLength(0);
  });

  it("supports ArrowLeft/ArrowRight/Home/End navigation with roving focus", async () => {
    const user = userEvent.setup();
    installHostDetailMock();
    renderHostDetail();

    const overviewTab = await screen.findByRole("tab", { name: "概览" });
    expect(overviewTab).toHaveAttribute("aria-selected", "true");
    overviewTab.focus();

    await user.keyboard("{ArrowRight}");
    const metricsTab = screen.getByRole("tab", { name: "资源" });
    expect(metricsTab).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(metricsTab).toHaveFocus());

    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "账户" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(screen.getByRole("tab", { name: "账户" })).toHaveFocus());

    await user.keyboard("{Home}");
    expect(screen.getByRole("tab", { name: "概览" })).toHaveAttribute("aria-selected", "true");
    await waitFor(() => expect(screen.getByRole("tab", { name: "概览" })).toHaveFocus());

    await user.keyboard("{ArrowLeft}");
    expect(screen.getByRole("tab", { name: "账户" })).toHaveAttribute("aria-selected", "true");
  });

  it("fetches only the active tab and unmounts it when switching", async () => {
    const user = userEvent.setup();
    const { requests } = installHostDetailMock();
    renderHostDetail("/hosts/1?tab=overview");

    expect(await screen.findByTestId("system-section")).toBeInTheDocument();
    expect(requestsTo(requests, "/api/servers/1/metrics")).toHaveLength(0);
    expect(requestsTo(requests, "/api/servers/1/gpus")).toHaveLength(0);
    expect(requestsTo(requests, "/api/servers/1/mounts")).toHaveLength(0);
    expect(requestsTo(requests, "/api/servers/1/users")).toHaveLength(0);

    await user.click(screen.getByRole("tab", { name: "资源" }));
    expect(await screen.findByTestId("metrics-tab")).toBeInTheDocument();
    expect(screen.queryByTestId("system-section")).not.toBeInTheDocument();
    await waitFor(() => {
      expect(requestsTo(requests, "/api/servers/1/metrics").length).toBeGreaterThan(0);
    });
    expect(requestsTo(requests, "/api/servers/1/mounts")).toHaveLength(0);

    await user.click(screen.getByRole("tab", { name: "概览" }));
    expect(await screen.findByTestId("system-section")).toBeInTheDocument();
    expect(screen.queryByTestId("metrics-tab")).not.toBeInTheDocument();
  });
});
