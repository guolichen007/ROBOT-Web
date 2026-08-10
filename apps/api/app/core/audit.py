from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import AuditLog


def write_audit(
    db: Session,
    *,
    action: str,
    resource_type: str,
    user_id: str | None = None,
    robot_id: str | None = None,
    resource_id: str | None = None,
    request_id: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    result: str = "SUCCESS",
    actor_type: str = "USER",
) -> AuditLog:
    row = AuditLog(
        actor_type=actor_type,
        user_id=user_id,
        robot_id=robot_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        request_id=request_id,
        ip=ip,
        user_agent=user_agent,
        before_json=before,
        after_json=after,
        result=result,
    )
    db.add(row)
    return row
