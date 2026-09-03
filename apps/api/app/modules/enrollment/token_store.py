"""一次性 enrollment token（DB 事务化消费，杜绝 host/container 文件同步）。

安全属性：
- issue：生成随机 token，只存 SHA-256 哈希 + credential_json（不存明文 token）。
- consume：`SELECT ... FOR UPDATE` + `consumed_at IS NULL` 条件，事务化保证并发下仅一次成功。
- 重放 / 过期 / 跨设备 / 错误 token → None。
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select

from app.db.models import EnrollmentToken

DEFAULT_TTL_SECONDS = 3600


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def issue_token(
    db, device_id: str, credential: dict, ttl_seconds: int = DEFAULT_TTL_SECONDS
) -> str:
    """签发一次性 token，返回明文 token（只存哈希）；重复签发覆盖旧 token。"""
    token = secrets.token_hex(32)
    now = datetime.now(UTC)
    db.execute(delete(EnrollmentToken).where(EnrollmentToken.device_id == device_id))
    db.add(
        EnrollmentToken(
            device_id=device_id,
            token_hash=_hash(token),
            credential_json=credential,
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
        )
    )
    db.flush()
    return token


def consume_token(db, device_id: str, token: str) -> dict | None:
    """事务化消费：并发下仅一次成功（SELECT FOR UPDATE + consumed_at IS NULL）。"""
    row = db.execute(
        select(EnrollmentToken)
        .where(
            EnrollmentToken.device_id == device_id,
            EnrollmentToken.consumed_at.is_(None),
            EnrollmentToken.expires_at > datetime.now(UTC),
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        return None
    if not secrets.compare_digest(row.token_hash, _hash(token)):
        return None
    row.consumed_at = datetime.now(UTC)
    db.flush()
    return dict(row.credential_json or {})
