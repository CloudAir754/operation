"""只读 HTTP API。"""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from lab_ops_panel.application.credentials import (
    delete_initial_credentials,
    initial_credentials_path,
)
from lab_ops_panel.application.user_imports import preview_csv, preview_text, preview_xlsx
from lab_ops_panel.domain.passwords import DEFAULT_SYMBOLS, PasswordPolicy, password_summary
from lab_ops_panel.domain.ssh_keys import preview_public_keys
from lab_ops_panel.domain.user_imports import RawImportRow, validate_import_rows
from lab_ops_panel.infrastructure.gpu import gpu_snapshot
from lab_ops_panel.infrastructure.pam import pam_policy_snapshot
from lab_ops_panel.infrastructure.privileged import helper_capability
from lab_ops_panel.infrastructure.resources import resource_snapshot
from lab_ops_panel.infrastructure.scans import TERMINAL_STATES, home_scan_manager
from lab_ops_panel.infrastructure.storage import block_device_snapshot
from lab_ops_panel.infrastructure.system_info import (
    local_users,
    mount_points,
    system_snapshot,
)

router = APIRouter(prefix="/api/v1")


class HomeScanRequest(BaseModel):
    """启动受限 home 扫描所需的结构化参数。"""

    username: str = Field(pattern=r"^[a-z_][a-z0-9_.-]*[$]?$", max_length=32)
    idempotency_key: str = Field(min_length=16, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    max_depth: int = Field(default=5, ge=1, le=8)
    max_nodes: int = Field(default=20_000, ge=100, le=50_000)


class TextImportRequest(BaseModel):
    """文本粘贴预览参数。"""

    text: str = Field(max_length=1_000_000)


class PasswordPolicyRequest(BaseModel):
    """随机密码规则预览参数。"""

    length: int = 20
    min_uppercase: int = 1
    min_lowercase: int = 1
    min_digits: int = 1
    min_symbols: int = 1
    symbols: str = Field(default=DEFAULT_SYMBOLS, max_length=100)
    exclude_ambiguous: bool = True


class SshKeyPreviewRequest(BaseModel):
    """SSH 公钥粘贴预览参数。"""

    keys: str = Field(max_length=100_000)


class UserCreationRow(BaseModel):
    """实际创建任务只接受预览阶段定义的两项账号字段。"""

    username: str = Field(min_length=1, max_length=32)
    display_name: str = Field(min_length=1, max_length=100)

    @field_validator("display_name")
    @classmethod
    def reject_display_name_whitespace(cls, value: str) -> str:
        """保持与批量导入领域规则一致，拒绝姓名中的空白字符。"""

        if any(character.isspace() for character in value):
            raise ValueError("中文姓名不能包含空白字符")
        return value


class UserCreationRequest(PasswordPolicyRequest):
    """启动幂等批量用户创建任务的完整结构化参数。"""

    rows: list[UserCreationRow] = Field(min_length=1, max_length=500)
    idempotency_key: str = Field(min_length=16, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


@router.get("/health")
def health() -> dict[str, str]:
    """返回进程存活状态。"""

    return {"status": "ok"}


@router.get("/overview")
def overview() -> dict[str, Any]:
    """返回总览页的一次性只读快照。"""

    return {
        "system": system_snapshot(),
        "resources": resource_snapshot(),
        "gpu": gpu_snapshot(),
        "mounts": mount_points(),
    }


@router.get("/resources")
def resources() -> dict[str, Any]:
    """返回供页面短周期刷新的 CPU 与内存快照。"""

    return resource_snapshot()


@router.get("/storage")
def storage() -> dict[str, Any]:
    """返回块设备拓扑和可获得的稳定身份。"""

    return block_device_snapshot()


@router.get("/gpu")
def gpu() -> dict[str, Any]:
    """返回可短周期刷新的 GPU 与当前进程快照。"""

    return gpu_snapshot()


@router.get("/users")
def users() -> list[dict[str, Any]]:
    """列出允许交互式登录的本机账号及可识别的 sudo 组来源。"""

    return local_users()


def _existing_usernames() -> set[str]:
    """每次预览均以当前操作系统账号为事实来源。"""

    return {str(user["username"]) for user in local_users()}


def _preview_result(factory, *args):  # type: ignore[no-untyped-def]
    """将导入领域错误转换为不泄露文件内容的 HTTP 错误。"""

    try:
        return factory(*args, _existing_usernames()).to_dict()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/user-imports/preview/text")
def preview_text_import(request: TextImportRequest) -> dict[str, Any]:
    """预览空白分隔的文本账号列表。"""

    return _preview_result(preview_text, request.text)


async def _read_upload(upload: UploadFile, maximum_bytes: int = 5 * 1024 * 1024) -> bytes:
    """限量读取上传内容，防止单次预览占用无界内存。"""

    value = await upload.read(maximum_bytes + 1)
    if len(value) > maximum_bytes:
        raise HTTPException(status_code=413, detail="上传文件不能超过 5 MiB")
    return value


@router.post("/user-imports/preview/csv")
async def preview_csv_import(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    """预览 UTF-8 CSV 账号列表。"""

    return _preview_result(preview_csv, await _read_upload(file))


@router.post("/user-imports/preview/xlsx")
async def preview_xlsx_import(file: Annotated[UploadFile, File()]) -> dict[str, Any]:
    """预览 XLSX 第一工作表账号列表。"""

    return _preview_result(preview_xlsx, await _read_upload(file))


@router.get("/password-policy/default")
def default_password_policy() -> dict[str, Any]:
    """返回首版默认随机密码规则的脱敏摘要。"""

    return password_summary(PasswordPolicy())


@router.post("/password-policy/preview")
def preview_password_policy(request: PasswordPolicyRequest) -> dict[str, Any]:
    """校验随机密码规则并返回脱敏示例，不生成可用凭据。"""

    try:
        return password_summary(PasswordPolicy(**request.model_dump()))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/pam-policy")
def actual_pam_policy() -> dict[str, Any]:
    """返回当前实际可见的 PAM 密码堆栈和 pwquality 能力。"""

    return pam_policy_snapshot()


@router.post("/ssh-keys/preview")
def ssh_key_preview(request: SshKeyPreviewRequest) -> dict[str, Any]:
    """校验公钥类型与指纹，不回传完整密钥内容。"""

    return preview_public_keys(request.keys)


@router.get("/privileged-capability")
def privileged_capability(request: Request) -> dict[str, Any]:
    """报告可信特权助手是否已由管理员安全安装。"""

    return helper_capability(request.app.state.privileged.helper_path)


@router.post("/users", status_code=202)
def create_users(request: UserCreationRequest, http_request: Request) -> dict[str, Any]:
    """重新校验预览结果后，启动幂等的单线程用户创建任务。"""

    capability = helper_capability(http_request.app.state.privileged.helper_path)
    if not capability["available"]:
        raise HTTPException(status_code=503, detail=capability["reason"])
    raw_rows = [
        RawImportRow(index, row.username, row.display_name)
        for index, row in enumerate(request.rows, 1)
    ]
    validated = validate_import_rows(raw_rows, _existing_usernames())
    rejected = [row for row in validated if row.status != "will_create"]
    if rejected:
        raise HTTPException(
            status_code=409,
            detail="账号事实状态已变化或提交内容无效，请重新预览后再创建",
        )
    policy_fields = PasswordPolicyRequest.model_fields
    policy_values = {name: getattr(request, name) for name in policy_fields}
    try:
        policy = PasswordPolicy(**policy_values)
        policy.validate()
        return http_request.app.state.user_creation.start(
            [row.model_dump() for row in request.rows],
            policy,
            request.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _credential_task(task_id: str, request: Request) -> dict[str, Any]:
    """确认凭据访问关联的是实际用户创建任务。"""

    try:
        task = request.app.state.tasks.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc
    if task["kind"] != "create_users":
        raise HTTPException(status_code=404, detail="该任务没有初始凭据")
    return task


@router.get("/tasks/{task_id}/credentials")
def download_credentials(task_id: str, request: Request) -> FileResponse:
    """下载仅启动用户可读的一次性初始凭据 CSV。"""

    _credential_task(task_id, request)
    try:
        path = initial_credentials_path(request.app.state.settings.data_dir, task_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return FileResponse(
        path,
        media_type="text/csv; charset=utf-8",
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )


@router.delete("/tasks/{task_id}/credentials", status_code=204)
def delete_credentials(task_id: str, request: Request) -> Response:
    """由管理员明确删除一次性初始凭据文件。"""

    _credential_task(task_id, request)
    try:
        delete_initial_credentials(request.app.state.settings.data_dir, task_id)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    request.app.state.tasks.log("删除初始凭据", task_id, "succeeded", task_id)
    return Response(status_code=204)


@router.get("/tasks")
def recent_tasks(request: Request) -> list[dict[str, Any]]:
    """返回当前数据库中最近的持久化任务。"""

    return request.app.state.tasks.list_recent()


@router.get("/tasks/{task_id}")
def task_detail(task_id: str, request: Request) -> dict[str, Any]:
    """返回单个持久化任务及步骤。"""

    try:
        return request.app.state.tasks.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="任务不存在") from exc


@router.post("/home-scans", status_code=202)
def start_home_scan(request: HomeScanRequest) -> dict[str, Any]:
    """创建可取消的 home 后台扫描，相同幂等键复用原任务。"""

    try:
        return home_scan_manager.start(
            request.username,
            request.idempotency_key,
            request.max_depth,
            request.max_nodes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/home-scans/{task_id}")
def get_home_scan(task_id: str) -> dict[str, Any]:
    """返回 home 扫描任务和完成后的 treemap 数据。"""

    try:
        return home_scan_manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="扫描任务不存在") from exc


@router.post("/home-scans/{task_id}/cancel")
def cancel_home_scan(task_id: str) -> dict[str, Any]:
    """幂等请求取消 home 扫描。"""

    try:
        return home_scan_manager.cancel(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="扫描任务不存在") from exc


@router.get("/home-scans/{task_id}/events")
def home_scan_events(task_id: str) -> StreamingResponse:
    """以 SSE 推送扫描进度，仍由统一启动令牌中间件保护。"""

    try:
        home_scan_manager.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="扫描任务不存在") from exc

    async def events() -> AsyncIterator[str]:
        previous = ""
        while True:
            snapshot = home_scan_manager.get(task_id)
            payload = json.dumps(snapshot, ensure_ascii=False)
            if payload != previous:
                yield f"event: progress\ndata: {payload}\n\n"
                previous = payload
            if snapshot["state"] in TERMINAL_STATES:
                break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
