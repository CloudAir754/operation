"""只读 HTTP API。"""

import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from lab_ops_panel.application.credentials import (
    delete_initial_credentials,
    initial_credentials_path,
)
from lab_ops_panel.application.user_imports import preview_csv, preview_text, preview_xlsx
from lab_ops_panel.domain.passwords import DEFAULT_SYMBOLS, PasswordPolicy, password_summary
from lab_ops_panel.domain.ssh_keys import parse_public_key, preview_public_keys
from lab_ops_panel.domain.user_imports import RawImportRow, validate_import_rows
from lab_ops_panel.infrastructure.gpu import gpu_snapshot
from lab_ops_panel.infrastructure.pam import pam_policy_snapshot
from lab_ops_panel.infrastructure.pam_rules import validate_policy_changes
from lab_ops_panel.infrastructure.privileged import helper_capability
from lab_ops_panel.infrastructure.processes import (
    minimum_regular_uid,
    port_usage,
    process_identity,
)
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


class AccountActionRequest(BaseModel):
    """系统写任务共用的幂等参数。"""

    idempotency_key: str = Field(min_length=16, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")


class PamPolicyChanges(BaseModel):
    """首版允许修改的 pwquality 字段及保守取值范围。"""

    model_config = ConfigDict(extra="forbid")

    minlen: int | None = Field(default=None, ge=15, le=128)
    dcredit: int | None = Field(default=None, ge=-20, le=0)
    ucredit: int | None = Field(default=None, ge=-20, le=0)
    lcredit: int | None = Field(default=None, ge=-20, le=0)
    ocredit: int | None = Field(default=None, ge=-20, le=0)
    minclass: int | None = Field(default=None, ge=0, le=4)
    maxrepeat: int | None = Field(default=None, ge=0, le=128)
    maxsequence: int | None = Field(default=None, ge=0, le=128)
    maxclassrepeat: int | None = Field(default=None, ge=0, le=128)
    usercheck: bool | None = None
    retry: int | None = Field(default=None, ge=1, le=10)
    difok: int | None = Field(default=None, ge=0, le=128)

    def validated(self) -> dict[str, int | bool]:
        """去除未提交字段并复用基础设施层白名单校验。"""

        return validate_policy_changes(self.model_dump(exclude_none=True))


class PamPolicyPreviewRequest(BaseModel):
    """PAM 策略修改预览参数。"""

    changes: PamPolicyChanges


class PamPolicyApplyRequest(PamPolicyPreviewRequest, AccountActionRequest):
    """经固定确认文本授权的 PAM 修改任务。"""

    confirmation: str = Field(max_length=50)


class PamPolicyRestoreRequest(AccountActionRequest):
    """指定可验证备份的 PAM 恢复任务。"""

    backup_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirmation: str = Field(max_length=80)


class ProcessTerminateRequest(AccountActionRequest):
    """绑定稳定进程身份的单进程终止参数。"""

    expected_uid: int = Field(gt=0)
    expected_create_time: float = Field(gt=0)
    grace_seconds: int = Field(default=5, ge=1, le=30)
    confirmation: str = Field(max_length=80)


class UidProcessesTerminateRequest(AccountActionRequest):
    """按用户结束全部进程的确认参数。"""

    grace_seconds: int = Field(default=5, ge=1, le=30)
    confirmation: str = Field(max_length=100)


class UserDeletePreflightRequest(AccountActionRequest):
    """启动用户删除只读预检的参数。"""


class UserDeleteRequest(AccountActionRequest):
    """引用近期成功预检的不可逆用户删除参数。"""

    preflight_task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    delete_home: bool = False
    confirmation: str = Field(min_length=1, max_length=32)


class FileTransferPreflightRequest(AccountActionRequest):
    """部分文件迁移的源路径与目标目录。"""

    source: str = Field(min_length=2, max_length=1000)
    target_directory: str = Field(min_length=2, max_length=1000)


class FileTransferCopyRequest(AccountActionRequest):
    """引用近期成功预检的只复制任务。"""

    preflight_task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirmation: str = Field(max_length=50)


class FileTransferCleanupRequest(AccountActionRequest):
    """引用成功复制任务的独立源清理确认。"""

    copy_task_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    confirmation: str = Field(min_length=2, max_length=1000)


class SshKeyPreviewRequest(BaseModel):
    """SSH 公钥粘贴预览参数。"""

    keys: str = Field(max_length=100_000)


class SshKeyImportRequest(AccountActionRequest):
    """SSH 公钥实际导入参数。"""

    keys: str = Field(min_length=1, max_length=100_000)


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


@router.get("/ports/{port}")
def lookup_port(
    port: int,
    request: Request,
    protocol: str = Query(default="any", pattern=r"^(tcp|udp|any)$"),
) -> dict[str, Any]:
    """查询本地端口；可信助手可用时返回完整 root 视图，否则明确降级。"""

    if not 1 <= port <= 65535:
        raise HTTPException(status_code=400, detail="端口必须在 1 到 65535 之间")
    capability = helper_capability(request.app.state.privileged.helper_path)
    if capability["available"]:
        try:
            return request.app.state.privileged.execute(
                "port-lookup", {"port": port, "protocol": protocol}
            )
        except RuntimeError:
            pass
    try:
        return port_usage(port, protocol)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


def _pam_change_preview(changes: dict[str, int | bool]) -> dict[str, Any]:
    """基于当前实际策略生成不写文件的修改判定。"""

    current = pam_policy_snapshot()
    conflicts = sorted(set(changes) & set(current.get("module_settings", {})))
    supported = bool(current["modification_supported"]) and not conflicts
    reason = current["modification_reason"]
    if conflicts:
        reason = f"PAM 模块参数覆盖了待修改字段：{', '.join(conflicts)}"
    effective_after = dict(current["settings"])
    if supported:
        effective_after.update(changes)
    return {
        "supported": supported,
        "reason": reason,
        "config_path": "/etc/security/pwquality.conf",
        "current": current["settings"],
        "requested": changes,
        "effective_after": effective_after,
        "module_conflicts": conflicts,
        "confirmation_text": "修改全机PAM策略",
    }


@router.post("/pam-policy/change-preview")
def preview_pam_change(request: PamPolicyPreviewRequest) -> dict[str, Any]:
    """预览 PAM 修改的当前值、目标值和能力冲突。"""

    return _pam_change_preview(request.changes.validated())


@router.post("/pam-policy/apply", status_code=202)
def apply_pam_change(request: PamPolicyApplyRequest, http_request: Request) -> dict[str, Any]:
    """明确确认后启动带备份和自动恢复的 PAM 修改任务。"""

    _require_privileged_helper(http_request)
    if request.confirmation != "修改全机PAM策略":
        raise HTTPException(status_code=400, detail="PAM 修改确认文本不匹配")
    changes = request.changes.validated()
    preview = _pam_change_preview(changes)
    if not preview["supported"]:
        raise HTTPException(status_code=409, detail=preview["reason"])
    try:
        return http_request.app.state.pam_policy.start_update(changes, request.idempotency_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/pam-policy/restore", status_code=202)
def restore_pam_change(request: PamPolicyRestoreRequest, http_request: Request) -> dict[str, Any]:
    """输入绑定备份 ID 的确认文本后启动恢复任务。"""

    _require_privileged_helper(http_request)
    if request.confirmation != f"恢复PAM备份:{request.backup_id}":
        raise HTTPException(status_code=400, detail="PAM 恢复确认文本不匹配")
    try:
        return http_request.app.state.pam_policy.start_restore(
            request.backup_id, request.idempotency_key
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/ssh-keys/preview")
def ssh_key_preview(request: SshKeyPreviewRequest) -> dict[str, Any]:
    """校验公钥类型与指纹，不回传完整密钥内容。"""

    return preview_public_keys(request.keys)


def _interactive_local_account(username: str) -> dict[str, Any]:
    """只允许当前仍可交互登录的本机账号参与写操作。"""

    account = next((user for user in local_users() if user["username"] == username), None)
    if (
        account is None
        or account["account_source"] != "local"
        or int(account["uid"]) < minimum_regular_uid()
    ):
        raise HTTPException(status_code=404, detail="未找到可管理的本机交互式账号")
    return account


@router.post("/processes/{pid}/terminate", status_code=202)
def terminate_single_process(
    pid: int, request: ProcessTerminateRequest, http_request: Request
) -> dict[str, Any]:
    """确认稳定进程身份后启动 TERM/KILL 任务。"""

    _require_privileged_helper(http_request)
    if request.confirmation != f"结束进程:{pid}":
        raise HTTPException(status_code=400, detail="进程终止确认文本不匹配")
    try:
        identity = process_identity(pid)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if identity["uid"] < minimum_regular_uid():
        raise HTTPException(status_code=403, detail="拒绝终止 root 或系统账号进程")
    if (
        identity["uid"] != request.expected_uid
        or abs(identity["create_time"] - request.expected_create_time) > 0.01
    ):
        raise HTTPException(status_code=409, detail="进程身份已变化，请重新查询端口")
    try:
        return http_request.app.state.process_control.start_process_termination(
            pid,
            request.expected_uid,
            request.expected_create_time,
            request.grace_seconds,
            request.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/users/{username}/processes/terminate", status_code=202)
def terminate_all_user_processes(
    username: str, request: UidProcessesTerminateRequest, http_request: Request
) -> dict[str, Any]:
    """输入用户名绑定确认文本后，按稳定 UID 终止普通用户全部进程。"""

    _require_privileged_helper(http_request)
    account = _interactive_local_account(username)
    uid = int(account["uid"])
    if uid == os.getuid():
        raise HTTPException(status_code=403, detail="拒绝结束启动 LabOpsPanel 用户的全部进程")
    if request.confirmation != f"结束全部进程:{username}":
        raise HTTPException(status_code=400, detail="用户进程终止确认文本不匹配")
    try:
        return http_request.app.state.process_control.start_uid_termination(
            username, uid, request.grace_seconds, request.idempotency_key
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/users/{username}/deletion-preflight", status_code=202)
def start_user_deletion_preflight(
    username: str, request: UserDeletePreflightRequest, http_request: Request
) -> dict[str, Any]:
    """启动包含磁盘统计的用户删除后台预检。"""

    _require_privileged_helper(http_request)
    account = _interactive_local_account(username)
    uid = int(account["uid"])
    if uid == os.getuid():
        raise HTTPException(status_code=403, detail="拒绝删除启动 LabOpsPanel 的用户")
    try:
        return http_request.app.state.user_deletion.start_preflight(
            username, uid, str(account["home"]), request.idempotency_key
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _validated_delete_preflight(
    request: Request,
    task_id: str,
    username: str,
    uid: int,
    home: str,
    delete_home: bool,
) -> dict[str, Any]:
    """要求十分钟内成功且身份参数完全匹配的删除预检。"""

    try:
        task = request.app.state.tasks.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="删除预检任务不存在") from exc
    if (
        task["kind"] != "user_delete_preflight"
        or task["state"] != "succeeded"
        or task["parameters"] != {"username": username, "uid": uid, "home": home}
    ):
        raise HTTPException(status_code=409, detail="删除预检未成功或账号身份已变化")
    created_at = datetime.fromisoformat(task["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - created_at > timedelta(minutes=10):
        raise HTTPException(status_code=409, detail="删除预检已超过十分钟，请重新执行")
    detail = task["steps"][2]["detail"] or {}
    if detail.get("blockers"):
        raise HTTPException(status_code=409, detail="删除预检仍有阻断项")
    if delete_home and not detail.get("delete_home_supported"):
        raise HTTPException(status_code=409, detail="当前 home 不能安全自动删除")
    return task


@router.post("/users/{username}/delete", status_code=202)
def delete_user(username: str, request: UserDeleteRequest, http_request: Request) -> dict[str, Any]:
    """输入用户名二次确认后启动仅删账号或账号加 home 的任务。"""

    _require_privileged_helper(http_request)
    if request.confirmation != username:
        raise HTTPException(status_code=400, detail="删除确认必须完整输入用户名")
    account = _interactive_local_account(username)
    uid = int(account["uid"])
    home = str(account["home"])
    if uid == os.getuid():
        raise HTTPException(status_code=403, detail="拒绝删除启动 LabOpsPanel 的用户")
    _validated_delete_preflight(
        http_request,
        request.preflight_task_id,
        username,
        uid,
        home,
        request.delete_home,
    )
    try:
        return http_request.app.state.user_deletion.start_delete(
            username,
            uid,
            home,
            request.delete_home,
            request.preflight_task_id,
            request.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _successful_task(request: Request, task_id: str, kind: str) -> dict[str, Any]:
    """读取指定类型的成功任务，供后续高风险阶段绑定。"""

    try:
        task = request.app.state.tasks.get(task_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="关联任务不存在") from exc
    if task["kind"] != kind or task["state"] != "succeeded":
        raise HTTPException(status_code=409, detail="关联任务类型不匹配或尚未成功")
    return task


@router.post("/file-transfers/preflight", status_code=202)
def start_file_transfer_preflight(
    request: FileTransferPreflightRequest, http_request: Request
) -> dict[str, Any]:
    """启动不写磁盘的路径、容量和 inode 预检。"""

    _require_privileged_helper(http_request)
    try:
        return http_request.app.state.file_transfer.start_preflight(
            request.source, request.target_directory, request.idempotency_key
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/file-transfers/copy", status_code=202)
def start_file_transfer_copy(
    request: FileTransferCopyRequest, http_request: Request
) -> dict[str, Any]:
    """近期预检成功且明确确认后，复制、校验并发布目标路径。"""

    _require_privileged_helper(http_request)
    if request.confirmation != "复制并保留源文件":
        raise HTTPException(status_code=400, detail="文件复制确认文本不匹配")
    preflight = _successful_task(http_request, request.preflight_task_id, "file_transfer_preflight")
    created_at = datetime.fromisoformat(preflight["created_at"])
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    if datetime.now(UTC) - created_at > timedelta(minutes=10):
        raise HTTPException(status_code=409, detail="文件迁移预检已超过十分钟，请重新执行")
    parameters = preflight["parameters"]
    detail = preflight["steps"][1]["detail"] or {}
    summary = detail.get("source_summary") or {}
    try:
        return http_request.app.state.file_transfer.start_copy(
            str(parameters["source"]),
            str(parameters["target_directory"]),
            int(summary["device"]),
            int(summary["inode"]),
            preflight["id"],
            request.idempotency_key,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="文件迁移预检结果不完整") from exc


@router.post("/file-transfers/cleanup", status_code=202)
def cleanup_file_transfer_source_api(
    request: FileTransferCleanupRequest, http_request: Request
) -> dict[str, Any]:
    """完整输入源路径后，启动独立的源数据清理任务。"""

    _require_privileged_helper(http_request)
    copied = _successful_task(http_request, request.copy_task_id, "file_transfer_copy")
    result = copied["steps"][-1]["detail"] or {}
    source = str(result.get("source", ""))
    if request.confirmation != source:
        raise HTTPException(status_code=400, detail="源清理确认必须完整输入源路径")
    try:
        return http_request.app.state.file_transfer.start_cleanup(
            source,
            str(result["destination"]),
            int(result["source_device"]),
            int(result["source_inode"]),
            copied["id"],
            request.idempotency_key,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=409, detail="文件复制结果不完整") from exc


def _require_privileged_helper(request: Request) -> None:
    """统一阻断未安装可信助手时的所有系统写操作。"""

    capability = helper_capability(request.app.state.privileged.helper_path)
    if not capability["available"]:
        raise HTTPException(status_code=503, detail=capability["reason"])


@router.post("/users/{username}/ssh-keys", status_code=202)
def import_ssh_keys(
    username: str, request: SshKeyImportRequest, http_request: Request
) -> dict[str, Any]:
    """校验全部公钥后启动原子、去重且幂等的导入任务。"""

    _require_privileged_helper(http_request)
    account = _interactive_local_account(username)
    preview = preview_public_keys(request.keys)
    if preview["counts"]["invalid"] or not preview["rows"]:
        raise HTTPException(status_code=400, detail="公钥输入为空或包含无效行")
    keys_by_fingerprint = {}
    for line in request.keys.splitlines():
        if line.strip():
            key = parse_public_key(line)
            keys_by_fingerprint.setdefault(key.fingerprint, key)
    try:
        return http_request.app.state.account_access.start_ssh_import(
            username,
            int(account["uid"]),
            list(keys_by_fingerprint.values()),
            request.idempotency_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/users/{username}/sudo/grant", status_code=202)
def grant_sudo(
    username: str, request: AccountActionRequest, http_request: Request
) -> dict[str, Any]:
    """启动独立 sudoers 规则授权任务。"""

    _require_privileged_helper(http_request)
    account = _interactive_local_account(username)
    try:
        return http_request.app.state.account_access.start_sudo_change(
            username, int(account["uid"]), True, request.idempotency_key
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/users/{username}/sudo/revoke", status_code=202)
def revoke_sudo(
    username: str, request: AccountActionRequest, http_request: Request
) -> dict[str, Any]:
    """启动仅撤销本程序托管规则的 sudo 任务。"""

    _require_privileged_helper(http_request)
    account = _interactive_local_account(username)
    try:
        return http_request.app.state.account_access.start_sudo_change(
            username, int(account["uid"]), False, request.idempotency_key
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/privileged-capability")
def privileged_capability(request: Request) -> dict[str, Any]:
    """报告可信特权助手是否已由管理员安全安装。"""

    return helper_capability(request.app.state.privileged.helper_path)


@router.post("/users", status_code=202)
def create_users(request: UserCreationRequest, http_request: Request) -> dict[str, Any]:
    """重新校验预览结果后，启动幂等的单线程用户创建任务。"""

    _require_privileged_helper(http_request)
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
