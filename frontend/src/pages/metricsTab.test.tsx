import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { requestPath } from "../test/fetchMock";
import {
  installHostDetailMock,
  renderHostDetail,
  requestsTo,
} from "../test/hostDetailMock";
import {
  isoAt,
  makeMetricPoint,
  makeMetrics,
  makeMetricsCurrent,
} from "../test/observabilityFixtures";

function setVisibility(state: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
  document.dispatchEvent(new Event("visibilitychange"));
}

afterEach(() => {
  Reflect.deleteProperty(document, "visibilityState");
  vi.useRealTimers();
});

describe("资源 tab", () => {
  it("renders the four charts with axes, legends and N/A for unknown values", async () => {
    installHostDetailMock();
    renderHostDetail("/hosts/1?tab=metrics");

    expect(await screen.findByTestId("chart-cpu")).toBeInTheDocument();
    expect(screen.getByTestId("metrics-sample-interval")).toHaveTextContent("2000 ms");
    expect(screen.getByTestId("metrics-point-count")).toHaveTextContent("4");

    const cpuChart = screen.getByTestId("chart-cpu");
    expect(
      within(cpuChart).getByRole("img", { name: /CPU 使用率趋势图/ }),
    ).toBeInTheDocument();
    expect(within(cpuChart).getByText("总利用率")).toBeInTheDocument();
    expect(within(cpuChart).getByText("用户态")).toBeInTheDocument();
    expect(within(cpuChart).getByText("系统态")).toBeInTheDocument();
    expect(within(cpuChart).getByText("IO 等待")).toBeInTheDocument();
    expect(within(cpuChart).getAllByText("0%").length).toBeGreaterThan(0);
    expect(within(cpuChart).getAllByText("100%").length).toBeGreaterThan(0);

    const memoryChart = screen.getByTestId("chart-memory");
    expect(within(memoryChart).getByText("内存使用率")).toBeInTheDocument();
    expect(within(memoryChart).getByText("Swap 使用率")).toBeInTheDocument();

    const diskChart = screen.getByTestId("chart-disk");
    expect(within(diskChart).getByText("nvme0n1 读")).toBeInTheDocument();
    expect(within(diskChart).getByText("nvme0n1 写")).toBeInTheDocument();
    expect(within(diskChart).getAllByText(/MiB\/s/).length).toBeGreaterThan(0);

    const networkChart = screen.getByTestId("chart-network");
    expect(within(networkChart).getByText("eth0 RX")).toBeInTheDocument();
    expect(within(networkChart).getByText("eth0 TX")).toBeInTheDocument();

    const devices = screen.getByTestId("metrics-devices");
    expect(within(devices).getByText("loop0")).toBeInTheDocument();
    // nvme0n1p2 has null rates in the fixture: never rendered as 0.
    expect(within(devices).getAllByText("N/A").length).toBeGreaterThan(0);

    expect(within(screen.getByTestId("metrics-interfaces")).getByText("docker0")).toBeInTheDocument();
    expect(within(screen.getByTestId("metrics-cpu-detail")).getByText("逻辑核数")).toBeInTheDocument();
    expect(within(screen.getByTestId("metrics-cpu-detail")).getByText("32")).toBeInTheDocument();
  });

  it("shows the 非实时 badge and explains reset points instead of drawing zero", async () => {
    installHostDetailMock({
      metrics: () =>
        makeMetrics({
          stale: true,
          points: [
            makeMetricPoint({ collectedAt: isoAt(0), receivedAt: isoAt(0), cpuPercent: 10 }),
            makeMetricPoint({
              collectedAt: isoAt(2),
              receivedAt: isoAt(2),
              cpuPercent: null,
              reset: true,
              resetReason: "计数器回绕",
            }),
            makeMetricPoint({ collectedAt: isoAt(4), receivedAt: isoAt(4), cpuPercent: 30 }),
          ],
        }),
    });
    renderHostDetail("/hosts/1?tab=metrics");

    expect(await screen.findByText("非实时")).toBeInTheDocument();
    const reasons = await screen.findByTestId("metrics-reset-reasons");
    expect(reasons).toHaveTextContent("计数器回绕");
    expect(reasons).toHaveTextContent("不会绘制为 0");

    const markers = document.querySelectorAll(".chart__reset-marker");
    expect(markers.length).toBeGreaterThan(0);
    expect(markers[0].textContent).toContain("计数器回绕");
  });

  it("re-requests metrics immediately when the window changes", async () => {
    const user = userEvent.setup();
    const { requests } = installHostDetailMock();
    renderHostDetail("/hosts/1?tab=metrics");

    await screen.findByTestId("chart-cpu");
    expect(
      requests.some(
        (request) => requestPath(request.url) === "/api/servers/1/metrics?window=5m",
      ),
    ).toBe(true);

    await user.click(screen.getByRole("button", { name: "15m" }));
    await waitFor(() => {
      expect(
        requests.some(
          (request) => requestPath(request.url) === "/api/servers/1/metrics?window=15m",
        ),
      ).toBe(true);
    });
    expect(screen.getByRole("button", { name: "15m" })).toHaveAttribute("aria-pressed", "true");
  });

  it("polls every 2s while visible and stops polling while the document is hidden", async () => {
    vi.useFakeTimers({ toFake: ["setInterval", "clearInterval"] });
    const { requests } = installHostDetailMock();
    renderHostDetail("/hosts/1?tab=metrics");

    await screen.findByTestId("chart-cpu");
    const count = (): number => requestsTo(requests, "/api/servers/1/metrics").length;
    const initial = count();

    await act(async () => {
      vi.advanceTimersByTime(2100);
    });
    await waitFor(() => expect(count()).toBeGreaterThan(initial));

    const whileVisible = count();
    act(() => setVisibility("hidden"));
    await act(async () => {
      vi.advanceTimersByTime(8000);
    });
    expect(count()).toBe(whileVisible);

    act(() => setVisibility("visible"));
    await waitFor(() => expect(count()).toBeGreaterThan(whileVisible));
  });

  it("renders a per-section status notice when a probe section is not ok", async () => {
    installHostDetailMock({
      metrics: () =>
        makeMetrics({
          current: makeMetricsCurrent({
            cpu: {
              status: "permission_denied",
              error: "需要 sudo 权限读取 /proc/stat",
              requiresSudo: true,
              collectedAt: isoAt(0),
              data: null,
            },
          }),
        }),
    });
    renderHostDetail("/hosts/1?tab=metrics");

    const notice = await screen.findByTestId("metrics-cpu-notice");
    expect(notice).toHaveTextContent("权限不足");
    expect(notice).toHaveTextContent("需要 sudo 权限读取 /proc/stat");
    expect(screen.getByTestId("metrics-cpu-detail")).toHaveTextContent("CPU 明细不可用");
    expect(screen.getByTestId("chart-cpu")).toBeInTheDocument();
  });

  it("shows the baseline empty state when no sample points exist yet", async () => {
    installHostDetailMock({ metrics: () => makeMetrics({ points: [] }) });
    renderHostDetail("/hosts/1?tab=metrics");

    expect(await screen.findByTestId("chart-cpu")).toHaveTextContent("正在建立基线");
    expect(screen.getAllByText("正在建立基线").length).toBeGreaterThanOrEqual(4);
  });
});
