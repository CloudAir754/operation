import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { installHostDetailMock, renderHostDetail } from "../test/hostDetailMock";
import { makeGpuDevice, makeGpuMonitor } from "../test/observabilityFixtures";

describe("GPU tab", () => {
  it("renders device cards with PCI, driver, clocks and MIG instances", async () => {
    installHostDetailMock({
      gpus: () =>
        makeGpuMonitor({
          devices: [
            makeGpuDevice({
              migMode: "Enabled",
              migDevices: ["MIG 1g.5gb Device 0: (UUID: MIG-00000000-0000-0000-0000-000000000000)"],
            }),
          ],
        }),
    });
    renderHostDetail("/hosts/1?tab=gpu");

    const card = await screen.findByTestId("gpu-device-0");
    expect(card).toHaveTextContent("NVIDIA GeForce RTX 4090");
    expect(card).toHaveTextContent("0000:01:00.0");
    expect(card).toHaveTextContent("550.107.02");
    expect(card).toHaveTextContent("52 °C");
    expect(card).toHaveTextContent("210.5 W / 450 W");
    expect(card).toHaveTextContent("2520 MHz");
    expect(card).toHaveTextContent("MIG Enabled");
    expect(card).toHaveTextContent("MIG 1g.5gb Device 0");

    const memoryBar = within(card).getByRole("progressbar", { name: "GPU 0 显存使用率" });
    expect(memoryBar).toHaveAttribute("aria-valuenow", "33");
    expect(screen.getByTestId("chart-gpu-utilization")).toBeInTheDocument();
    expect(screen.getByTestId("chart-gpu-memory")).toBeInTheDocument();
  });

  it("filters processes by user and minimum memory", async () => {
    const user = userEvent.setup();
    installHostDetailMock();
    renderHostDetail("/hosts/1?tab=gpu");

    expect(await screen.findByTestId("gpu-process-4242")).toBeInTheDocument();
    expect(screen.getByTestId("gpu-process-5151")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("用户"), "alice");
    expect(screen.queryByTestId("gpu-process-4242")).not.toBeInTheDocument();
    expect(screen.getByTestId("gpu-process-5151")).toBeInTheDocument();

    await user.selectOptions(screen.getByLabelText("用户"), "all");
    const minMemory = screen.getByLabelText("最小显存 (MiB)");
    await user.clear(minMemory);
    await user.type(minMemory, "2000");
    expect(screen.queryByTestId("gpu-process-5151")).not.toBeInTheDocument();
    expect(screen.getByTestId("gpu-process-4242")).toBeInTheDocument();
    expect(screen.getByTestId("gpu-process-6161")).toBeInTheDocument();
  });

  it("sorts by memory or PID and toggles the direction", async () => {
    const user = userEvent.setup();
    installHostDetailMock();
    renderHostDetail("/hosts/1?tab=gpu");

    await screen.findByTestId("gpu-process-4242");
    const rowOrder = () =>
      screen
        .getAllByTestId(/^gpu-process-/)
        .map((row) => row.getAttribute("data-testid"));

    expect(rowOrder()).toEqual([
      "gpu-process-4242",
      "gpu-process-6161",
      "gpu-process-5151",
    ]);

    await user.click(screen.getByRole("button", { name: "切换为升序" }));
    expect(rowOrder()).toEqual([
      "gpu-process-5151",
      "gpu-process-6161",
      "gpu-process-4242",
    ]);

    await user.selectOptions(screen.getByLabelText("排序"), "pid");
    expect(rowOrder()).toEqual(["gpu-process-4242", "gpu-process-5151", "gpu-process-6161"]);
    await user.click(screen.getByRole("button", { name: "切换为降序" }));
    expect(rowOrder()).toEqual(["gpu-process-6161", "gpu-process-5151", "gpu-process-4242"]);
  });

  it("marks partial process visibility and stale processes, and renders N/A for unknown utilization", async () => {
    installHostDetailMock({
      gpus: () =>
        makeGpuMonitor({
          processesStatus: "partial",
          processesError: "hidepid=2 限制其他用户进程可见",
        }),
    });
    renderHostDetail("/hosts/1?tab=gpu");

    expect(await screen.findByText(/部分 GPU 进程不可见/)).toBeInTheDocument();
    expect(screen.getByText(/hidepid=2/)).toBeInTheDocument();

    const staleRow = screen.getByTestId("gpu-process-6161");
    expect(within(staleRow).getByText("已退出")).toBeInTheDocument();
    expect(within(staleRow).queryByText("运行中")).not.toBeInTheDocument();

    const unknownRow = screen.getByTestId("gpu-process-5151");
    expect(within(unknownRow).getByText("N/A")).toBeInTheDocument();
  });

  it("shows the unavailable state when the driver or process list is missing", async () => {
    installHostDetailMock({
      gpus: () =>
        makeGpuMonitor({
          status: "unavailable",
          error: "nvidia-smi not found",
          devices: [],
          processes: [],
          processesStatus: "unavailable",
          processesError: "nvidia-smi not found",
          capabilities: { nvml: false, processes: "unavailable", mig: false, perProcessUtilization: false },
        }),
    });
    renderHostDetail("/hosts/1?tab=gpu");

    expect(await screen.findByText("未检测到 GPU 设备")).toBeInTheDocument();
    expect(screen.getByTestId("gpu-monitor-notice")).toHaveTextContent("nvidia-smi not found");
    expect(screen.getByTestId("gpu-processes-notice")).toHaveTextContent("nvidia-smi not found");
    await waitFor(() => {
      expect(screen.queryByTestId("chart-gpu-utilization")).toBeInTheDocument();
    });
  });
});
