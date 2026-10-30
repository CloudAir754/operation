import type { RenderResult } from "@testing-library/react";
import type {
  GpuMonitor,
  Metrics,
  MountsResponse,
  Snapshot,
  StorageNode,
  StorageScan,
  UsersResponse,
} from "../api/types";
import { HostDetailPage } from "../pages/HostDetailPage";
import { installFetchMock, notFoundResponse, requestPath } from "./fetchMock";
import type { MockRequest, MockResponse } from "./fetchMock";
import { makeHost, makeSnapshot } from "./fixtures";
import {
  makeGpuMonitor,
  makeMetrics,
  makeMountsResponse,
  makeStorageScan,
  makeUsersResponse,
} from "./observabilityFixtures";
import { renderRoute } from "./renderRoute";

export interface HostDetailMockHandlers {
  snapshot?: () => Snapshot;
  metrics?: (window: string) => Metrics;
  gpus?: (refresh: boolean) => GpuMonitor;
  users?: (includeSystem: boolean) => UsersResponse;
  mounts?: () => MountsResponse;
  /** Return a custom response for POST /storage/scans. */
  createScan?: (body: unknown) => MockResponse;
  /** Return a custom response for POST /users/storage-scan. */
  createUserScan?: (body: unknown) => MockResponse;
  getScan?: (scanId: string) => StorageScan | undefined;
  cancelScan?: (scanId: string) => MockResponse;
  children?: (scanId: string, path: string) => StorageNode[] | undefined;
}

export interface HostDetailMockResult {
  requests: MockRequest[];
}

const SCAN_PATH = "/api/servers/1/storage/scans";

function defaultChildren(path: string): StorageNode[] {
  return makeStorageScan().nodes.filter((node) => node.parentPath === path);
}

function queryOf(request: MockRequest): URLSearchParams {
  return new URL(request.url, "http://localhost").searchParams;
}

/**
 * Installs a fetch double covering every endpoint the host detail console uses.
 * Individual callbacks override the sensible defaults from `observabilityFixtures`.
 */
export function installHostDetailMock(
  handlers: HostDetailMockHandlers = {},
): HostDetailMockResult {
  const { requests } = installFetchMock((request) => {
    const path = requestPath(request.url);
    const query = queryOf(request);
    const method = request.method;

    if (method === "GET" && path === "/api/servers") {
      return { body: { servers: [makeHost()], count: 1 } };
    }
    if (method === "GET" && path.startsWith("/api/servers/1/snapshot")) {
      return { body: (handlers.snapshot ?? (() => makeSnapshot()))() };
    }
    if (method === "GET" && path.startsWith("/api/servers/1/metrics")) {
      return {
        body: (handlers.metrics ?? (() => makeMetrics()))(query.get("window") ?? "5m"),
      };
    }
    if (method === "GET" && path.startsWith("/api/servers/1/gpus")) {
      return {
        body: (handlers.gpus ?? (() => makeGpuMonitor()))(query.get("refresh") === "true"),
      };
    }
    if (method === "GET" && path.startsWith("/api/servers/1/users")) {
      return {
        body: (handlers.users ?? ((includeSystem: boolean) =>
          makeUsersResponse({ includeSystem })))(query.get("includeSystem") === "true"),
      };
    }
    if (method === "GET" && path.startsWith("/api/servers/1/mounts")) {
      return { body: (handlers.mounts ?? (() => makeMountsResponse()))() };
    }
    if (method === "GET" && path === SCAN_PATH) {
      return { body: { scans: [], count: 0 } };
    }
    if (method === "POST" && path === SCAN_PATH) {
      return (
        handlers.createScan?.(request.body) ?? {
          status: 201,
          body: makeStorageScan({
            scanId: "scan-new",
            status: "queued",
            stage: "queued",
            progress: 0,
            nodes: [],
            largestFiles: [],
            summaryByUid: [],
            nodeCount: 0,
            finishedAt: null,
            startedAt: null,
          }),
        }
      );
    }
    if (method === "POST" && path === "/api/servers/1/users/storage-scan") {
      return (
        handlers.createUserScan?.(request.body) ?? {
          status: 201,
          body: makeStorageScan({
            scanId: "scan-user",
            status: "running",
            stage: "scanning",
            progress: 10,
            nodes: [],
            largestFiles: [],
            summaryByUid: [],
            nodeCount: 0,
            finishedAt: null,
          }),
        }
      );
    }
    if (/^\/api\/storage\/scans\/[^/]+\/children$/.test(path)) {
      const scanId = decodeURIComponent(path.split("/")[4] ?? "");
      const target = query.get("path") ?? "/";
      return { body: handlers.children?.(scanId, target) ?? defaultChildren(target) };
    }
    if (method === "POST" && /^\/api\/storage\/scans\/[^/]+\/cancel$/.test(path)) {
      const scanId = decodeURIComponent(path.split("/")[4] ?? "");
      return (
        handlers.cancelScan?.(scanId) ?? {
          body: makeStorageScan({
            scanId,
            status: "cancelled",
            stage: "cancelled",
            progress: 100,
            error: "已取消（尚未开始）",
          }),
        }
      );
    }
    if (method === "GET" && /^\/api\/storage\/scans\/[^/]+$/.test(path)) {
      const scanId = decodeURIComponent(path.split("/")[4] ?? "");
      return { body: handlers.getScan?.(scanId) ?? makeStorageScan({ scanId }) };
    }
    return notFoundResponse(method, path);
  });

  return { requests };
}

export function renderHostDetail(route = "/hosts/1"): RenderResult {
  return renderRoute(<HostDetailPage />, { route, path: "/hosts/:id" });
}

/** All requests whose path starts with `prefix`. */
export function requestsTo(
  requests: MockRequest[],
  prefix: string,
): MockRequest[] {
  return requests.filter((request) => requestPath(request.url).startsWith(prefix));
}
