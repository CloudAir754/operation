import type { FieldError } from "./types";

/** Stable error codes emitted by the API contract plus client-side fallbacks. */
export type ClientErrorCode =
  | "network_error"
  | "http_error"
  | "aborted"
  | "unknown_error"
  | string;

export interface ApiErrorInit {
  code: ClientErrorCode;
  /** HTTP status, or 0 when the request never reached the server. */
  status: number;
  fields?: FieldError[];
  /** Present on 409 `duplicate_idempotency_key`. */
  existingTaskId?: string | null;
}

/**
 * Error raised for every failed API call. `message` is always safe to render
 * in the UI (the backend sends Chinese messages directly).
 */
export class ApiError extends Error {
  readonly code: ClientErrorCode;
  readonly status: number;
  readonly fields: FieldError[];
  readonly existingTaskId: string | null;

  constructor(message: string, init: ApiErrorInit) {
    super(message);
    this.name = "ApiError";
    this.code = init.code;
    this.status = init.status;
    this.fields = init.fields ?? [];
    this.existingTaskId = init.existingTaskId ?? null;
  }

  get isNetworkError(): boolean {
    return this.code === "network_error";
  }
}

export function isApiError(error: unknown): error is ApiError {
  return error instanceof ApiError;
}

export function isAbortError(error: unknown): boolean {
  if (typeof error !== "object" || error === null) return false;
  const name = (error as { name?: unknown }).name;
  return name === "AbortError";
}

/** Normalise anything thrown by a request into an `ApiError`. */
export function normalizeApiError(error: unknown): ApiError {
  if (isApiError(error)) return error;
  if (isAbortError(error)) {
    return new ApiError("请求已取消", { code: "aborted", status: 0 });
  }
  if (error instanceof Error && error.message) {
    return new ApiError(error.message, { code: "unknown_error", status: 0 });
  }
  return new ApiError("发生未知错误，请重试。", { code: "unknown_error", status: 0 });
}

/** Human readable one-liner for banners. */
export function describeError(error: unknown): string {
  return normalizeApiError(error).message;
}
