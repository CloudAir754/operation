"""审计日志查询。"""

from __future__ import annotations

from fastapi import APIRouter, Query
from sqlalchemy import select

from lab_ops_panel.api.deps import ContainerDep
from lab_ops_panel.models import AuditLog
from lab_ops_panel.schemas import AuditEntryOut, AuditListOut

router = APIRouter(tags=["audit"])


@router.get("/audit", response_model=AuditListOut)
def list_audit(
    container: ContainerDep,
    limit: int = Query(default=50, ge=1, le=500),
) -> AuditListOut:
    with container.session_factory() as session:
        entries = session.scalars(
            select(AuditLog).order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
        ).all()
        return AuditListOut(
            entries=[
                AuditEntryOut(
                    id=entry.id,
                    action=entry.action,
                    target=entry.target,
                    outcome=entry.outcome,
                    detail=entry.detail,
                    created_at=entry.created_at,
                )
                for entry in entries
            ]
        )
