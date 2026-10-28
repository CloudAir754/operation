"""统一的 API 错误类型。

所有对外错误都带稳定的 ``code``，前端据此分支处理；``message`` 为中文可读信息。
"""

from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse


class ApiError(Exception):
    """业务错误，直接映射为 ``{"detail": {...}}`` 响应。"""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        fields: list[dict[str, str]] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.fields = fields or []
        self.extra = extra or {}

    def to_payload(self) -> dict[str, Any]:
        detail: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.fields:
            detail["fields"] = self.fields
        detail.update(self.extra)
        return {"detail": detail}

    def to_response(self) -> JSONResponse:
        return JSONResponse(status_code=self.status_code, content=self.to_payload())


class NotFoundError(ApiError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(404, code, message)


class ConflictError(ApiError):
    def __init__(self, code: str, message: str, *, extra: dict[str, Any] | None = None) -> None:
        super().__init__(409, code, message, extra=extra)


class InvalidRequestError(ApiError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(400, code, message)


class ValidationFailedError(ApiError):
    def __init__(self, message: str, *, fields: list[dict[str, str]] | None = None) -> None:
        super().__init__(422, "validation_failed", message, fields=fields)


def api_error_handler(_request: object, exc: ApiError) -> JSONResponse:
    return exc.to_response()
