"""审计日志写入。

只记录动作、目标、时间、结果与摘要；绝不记录密码或完整命令输出。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from lab_ops_panel.models import AuditLog

if TYPE_CHECKING:
    from sqlalchemy.orm import Session, sessionmaker

logger = logging.getLogger(__name__)

MAX_DETAIL_LENGTH = 400


def record_audit(
    session_factory: sessionmaker[Session],
    *,
    action: str,
    target: str,
    outcome: str,
    detail: str = "",
    operator: str = "local-admin",
) -> None:
    entry = AuditLog(
        action=action[:64],
        target=target[:255],
        outcome=outcome[:32],
        detail=(detail or "")[:MAX_DETAIL_LENGTH],
        operator=operator[:32],
        created_at=datetime.now(UTC),
    )
    session = session_factory()
    try:
        session.add(entry)
        session.commit()
    except Exception:  # pragma: no cover - 审计失败不应中断主流程
        session.rollback()
        logger.warning("写入审计日志失败", exc_info=True)
    finally:
        session.close()
