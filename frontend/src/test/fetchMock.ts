import { vi } from "vitest";

export interface MockRequest {
  url: string;
  method: string;
  body: unknown;
  init: RequestInit;
}

export interface MockResponse {
  status?: number;
  body?: unknown;
  raw?: string;
}

export type MockHandler = (request: MockRequest) => MockResponse | Promise<MockResponse>;

export function requestPath(url: string): string {
  const parsed = new URL(url, "http://localhost");
  return `${parsed.pathname}${parsed.search}`;
}

function jsonResponse(spec: MockResponse): Response {
  const status = spec.status ?? 200;
  const text = spec.raw ?? (status === 204 ? "" : JSON.stringify(spec.body ?? null));
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "",
    text: async () => text,
    json: async () => JSON.parse(text) as unknown,
  } as unknown as Response;
}

function resolveUrl(input: RequestInfo | URL): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  return input.url;
}

function parseBody(init: RequestInit | undefined): unknown {
  if (!init || typeof init.body !== "string") return undefined;
  try {
    return JSON.parse(init.body) as unknown;
  } catch {
    return init.body;
  }
}

/**
 * Install a deterministic fetch double. Every request is recorded so tests can
 * assert methods/bodies; unhandled routes can fail loudly.
 */
export function installFetchMock(handler: MockHandler): {
  requests: MockRequest[];
  fetchMock: ReturnType<typeof vi.fn>;
} {
  const requests: MockRequest[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const request: MockRequest = {
      url: resolveUrl(input),
      method: (init?.method ?? "GET").toUpperCase(),
      body: parseBody(init),
      init: init ?? {},
    };
    requests.push(request);
    return jsonResponse(await handler(request));
  });
  vi.stubGlobal("fetch", fetchMock);
  return { requests, fetchMock };
}

export function notFoundResponse(method: string, path: string): MockResponse {
  return {
    status: 404,
    body: { detail: { code: "not_found", message: `测试未模拟请求：${method} ${path}` } },
  };
}
