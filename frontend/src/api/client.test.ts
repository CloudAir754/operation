import { describe, expect, it, vi } from "vitest";
import { ApiError, createTask, getServers } from "./client";
import { installFetchMock, notFoundResponse, requestPath } from "../test/fetchMock";
import { makeHost } from "../test/fixtures";

describe("API client", () => {
  it("parses a successful JSON response", async () => {
    const host = makeHost();
    const { requests } = installFetchMock(({ url, method }) => {
      const path = requestPath(url);
      if (method === "GET" && path === "/api/servers") {
        return { body: { servers: [host], count: 1 } };
      }
      return notFoundResponse(method, path);
    });

    const result = await getServers();

    expect(result.count).toBe(1);
    expect(result.servers[0].alias).toBe("lab-gpu-01");
    expect(result.servers[0].lastConnect?.status).toBe("ok");
    expect(requests).toHaveLength(1);
    expect(requests[0].url).toBe("/api/servers");
    expect(requests[0].method).toBe("GET");
    expect(requests[0].body).toBeUndefined();
  });

  it("maps the contract error shape into ApiError", async () => {
    installFetchMock(({ url, method }) => {
      const path = requestPath(url);
      if (method === "POST" && path === "/api/tasks") {
        return {
          status: 409,
          body: {
            detail: {
              code: "duplicate_idempotency_key",
              message: "重复的幂等键",
              existingTaskId: "existing-task-1",
            },
          },
        };
      }
      return notFoundResponse(method, path);
    });

    const failure = await createTask({
      serverId: 1,
      action: "service_restart",
      params: { unit: "nginx.service" },
      confirm: true,
      idempotencyKey: "key-1",
    }).catch((reason: unknown) => reason);

    expect(failure).toBeInstanceOf(ApiError);
    const apiError = failure as ApiError;
    expect(apiError.code).toBe("duplicate_idempotency_key");
    expect(apiError.message).toBe("重复的幂等键");
    expect(apiError.status).toBe(409);
    expect(apiError.existingTaskId).toBe("existing-task-1");
    expect(apiError.fields).toEqual([]);
  });

  it("keeps field-level validation errors from 422 responses", async () => {
    installFetchMock(() => ({
      status: 422,
      body: {
        detail: {
          code: "validation_error",
          message: "请求字段不合法",
          fields: [{ field: "unit", message: "不能为空" }],
        },
      },
    }));

    const failure = await createTask({
      serverId: 1,
      action: "service_restart",
      params: {},
      confirm: true,
    }).catch((reason: unknown) => reason);

    expect(failure).toBeInstanceOf(ApiError);
    const apiError = failure as ApiError;
    expect(apiError.status).toBe(422);
    expect(apiError.fields).toEqual([{ field: "unit", message: "不能为空" }]);
  });

  it("maps fetch failures to a friendly network error", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("Failed to fetch");
      }),
    );

    const failure = await getServers().catch((reason: unknown) => reason);

    expect(failure).toBeInstanceOf(ApiError);
    const apiError = failure as ApiError;
    expect(apiError.code).toBe("network_error");
    expect(apiError.status).toBe(0);
    expect(apiError.message).toContain("无法连接到本机服务");
  });
});
