"""WEB-R1-MIN 反证测试：账号权限、会话失效、站点隔离、PREVIEW_ONLY 零下行、火警修复、导出授权。

每个测试只反证一个根因；“零下行”统一用 Command / OutboxEvent / Task 行数前后不变来证明
（Command 与 Outbox 是 MQTT 下行的唯一入口：dispatcher 只发布 OutboxEvent）。
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from uuid import uuid4

import pytest
from app.core.config import get_settings
from app.core.events import get_redis
from app.core.security import hash_password, verify_password
from app.db.models import (
    Command,
    FireEvent,
    Map,
    MapVersion,
    OutboxEvent,
    ParkingSlot,
    PatrolPlan,
    PatrolSchedule,
    PatrolScheduleOccurrence,
    RefreshSession,
    Robot,
    Role,
    Site,
    StreamRegistry,
    Task,
    Trajectory,
    User,
    user_roles,
)
from app.db.session import SessionLocal
from app.main import app
from app.modules.media.router import encode_media_ticket
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from starlette.websockets import WebSocketDisconnect

ROOT = Path(__file__).resolve().parents[3]
PASSWORD = "Web-R1-Min-Pass-2026!"
NEW_PASSWORD = "Web-R1-Min-Next-2026!"
PREFIX = "r1min_"


def load_service(name: str, relative: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def baseline() -> None:
    get_redis().flushdb()
    with SessionLocal.begin() as db:
        admin = db.scalar(select(User).where(User.username == "admin"))
        assert admin
        password = get_settings().effective_admin_password
        if not verify_password(password, admin.password_hash):
            admin.password_hash = hash_password(password)
        admin.must_change_password = False
        admin.status = "ACTIVE"
        admin.failed_attempts = 0
        admin.locked_until = None
        ids = db.scalars(select(User.id).where(User.username.like(f"{PREFIX}%"))).all()
        if ids:
            db.execute(delete(RefreshSession).where(RefreshSession.user_id.in_(ids)))
            db.execute(delete(user_roles).where(user_roles.c.user_id.in_(ids)))
            # 保留审计历史引用：账号只改名，不物理删除（与产品逻辑停用一致）。
            for user in db.scalars(select(User).where(User.id.in_(ids))).all():
                user.username = f"x{uuid4().hex[:10]}"
                user.status = "DISABLED"
        db.execute(delete(Site).where(Site.code == "R1MIN_OTHER"))


@pytest.fixture(autouse=True)
def drop_test_alarms():
    yield
    # 本文件制造的火警（含 UNMATCHED）在用例后清理：CI 结束时会执行 alembic downgrade base，
    # 0010 降级对残留 UNMATCHED 火警是 fail-closed 的（防止生产静默丢事件）。
    with SessionLocal.begin() as db:
        db.execute(
            delete(FireEvent).where(
                FireEvent.source_event_id.like("r1min-%") | FireEvent.event_code.like("FE-R1MIN-%")
            )
        )


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as value:
        yield value


def login(client: TestClient, username: str = "admin", password: str | None = None):
    return client.post(
        "/api/v1/auth/login",
        json={
            "username": username,
            "password": password or get_settings().effective_admin_password,
        },
    )


def bearer(token: str, **extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", **extra}


def admin_token(client: TestClient) -> str:
    response = login(client)
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def make_user(client: TestClient, token: str, name: str, roles: list[str]) -> dict:
    response = client.post(
        "/api/v1/admin/users",
        json={
            "username": f"{PREFIX}{name}",
            "display_name": f"测试{name}",
            "password": PASSWORD,
            "role_codes": roles,
        },
        headers=bearer(token),
    )
    assert response.status_code == 201, response.text
    return response.json()


def activate(client: TestClient, username: str) -> str:
    """首次登录 → 必须改密 → 重新登录，返回可用访问令牌。"""
    first = login(client, username, PASSWORD)
    assert first.status_code == 200, first.text
    assert first.json()["user"]["must_change_password"] is True
    token = first.json()["access_token"]
    blocked = client.get("/api/v1/robots", headers=bearer(token))
    assert blocked.status_code == 428
    changed = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        headers=bearer(token),
    )
    assert changed.status_code == 200, changed.text
    # 改密后旧访问令牌失效
    assert client.get("/api/v1/auth/me", headers=bearer(token)).status_code == 401
    again = login(client, username, NEW_PASSWORD)
    assert again.status_code == 200
    return again.json()["access_token"]


def downlink_counts() -> tuple[int, int, int]:
    with SessionLocal() as db:
        return (
            int(db.scalar(select(func.count(Command.id))) or 0),
            int(db.scalar(select(func.count(OutboxEvent.id))) or 0),
            int(db.scalar(select(func.count(Task.id))) or 0),
        )


def route_for(vehicle_id: str = "R001") -> tuple[str, str]:
    with SessionLocal() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == vehicle_id))
        assert robot
        trajectory = db.scalar(
            select(Trajectory)
            .join(MapVersion, MapVersion.id == Trajectory.map_version_id)
            .where(MapVersion.status == "PUBLISHED", Trajectory.enabled.is_(True))
            .order_by(Trajectory.code)
        )
        assert trajectory
        return robot.id, trajectory.id


def plan_body(name: str, robot_id: str, trajectory_id: str, **extra) -> dict:
    return {
        "name": name,
        "robot_id": robot_id,
        "trajectory_id": trajectory_id,
        "schedule": {"frequency": "DAILY", "time": "09:30", "timezone": "Asia/Shanghai"},
        "target_lateral_min_m": 0.22,
        "target_lateral_max_m": 0.28,
        **extra,
    }


# ---------------------------------------------------------------- 第一阶段：账号权限


def test_user_listing_never_exposes_password_hash(client: TestClient) -> None:
    token = admin_token(client)
    make_user(client, token, "listing", ["viewer"])
    rows = client.get("/api/v1/admin/users", headers=bearer(token)).json()
    assert rows and all("password_hash" not in row and "token_version" not in row for row in rows)
    target = next(r for r in rows if r["username"] == f"{PREFIX}listing")
    assert target["roles"] == ["viewer"] and target["must_change_password"] is True


def test_user_lifecycle_profile_roles_disable_reset_and_audit(client: TestClient) -> None:
    token = admin_token(client)
    user = make_user(client, token, "op1", ["viewer"])
    op_token = activate(client, f"{PREFIX}op1")

    profile = client.patch(
        f"/api/v1/admin/users/{user['id']}",
        json={"display_name": "巡检员甲"},
        headers=bearer(token),
    )
    assert profile.status_code == 200 and profile.json()["display_name"] == "巡检员甲"
    # 资料修改不影响会话
    assert client.get("/api/v1/auth/me", headers=bearer(op_token)).status_code == 200

    roles = client.put(
        f"/api/v1/admin/users/{user['id']}/roles",
        json={"role_codes": ["operator"]},
        headers=bearer(token),
    )
    assert roles.status_code == 200 and roles.json()["sessions_revoked"] is True
    # 权限变化后旧访问令牌立即失效（不必等 15 分钟过期）
    assert client.get("/api/v1/robots", headers=bearer(op_token)).status_code == 401
    op_token = login(client, f"{PREFIX}op1", NEW_PASSWORD).json()["access_token"]
    me = client.get("/api/v1/auth/me", headers=bearer(op_token)).json()
    assert me["roles"] == ["operator"] and "patrol.create" in me["permissions"]

    disabled = client.put(
        f"/api/v1/admin/users/{user['id']}/status",
        json={"status": "DISABLED", "reason": "离职"},
        headers=bearer(token),
    )
    assert disabled.status_code == 200 and disabled.json()["status"] == "DISABLED"
    assert client.get("/api/v1/auth/me", headers=bearer(op_token)).status_code == 401
    relogin = login(client, f"{PREFIX}op1", NEW_PASSWORD)
    assert relogin.status_code == 403
    with SessionLocal() as db:
        # 逻辑停用：账号与角色关联保留，刷新会话全部撤销
        row = db.get(User, user["id"])
        assert row and row.deactivated_at and row.deactivated_by
        live = db.scalar(
            select(func.count(RefreshSession.id)).where(
                RefreshSession.user_id == user["id"], RefreshSession.revoked_at.is_(None)
            )
        )
        assert live == 0

    enabled = client.put(
        f"/api/v1/admin/users/{user['id']}/status",
        json={"status": "ACTIVE"},
        headers=bearer(token),
    )
    assert enabled.status_code == 200
    reset = client.post(
        f"/api/v1/admin/users/{user['id']}/reset-password",
        json={"new_password": PASSWORD},
        headers=bearer(token),
    )
    assert reset.status_code == 200 and reset.json()["must_change_password"] is True
    assert login(client, f"{PREFIX}op1", NEW_PASSWORD).status_code == 401
    assert login(client, f"{PREFIX}op1", PASSWORD).json()["user"]["must_change_password"] is True

    audit = client.get(
        "/api/v1/history/audit", params={"action": "USER_"}, headers=bearer(token)
    ).json()
    actions = {row["action"] for row in audit if row["resource_id"] == user["id"]}
    assert {
        "USER_CREATE",
        "USER_PROFILE_UPDATE",
        "USER_ROLES_UPDATE",
        "USER_DISABLE",
        "USER_ENABLE",
        "USER_PASSWORD_RESET",
    } <= actions


def test_weak_password_and_duplicate_username_rejected(client: TestClient) -> None:
    token = admin_token(client)
    weak = client.post(
        "/api/v1/admin/users",
        json={
            "username": f"{PREFIX}weak",
            "display_name": "弱",
            "password": "alllowercase12",
            "role_codes": ["viewer"],
        },
        headers=bearer(token),
    )
    assert weak.status_code == 422
    make_user(client, token, "dup", ["viewer"])
    again = client.post(
        "/api/v1/admin/users",
        json={
            "username": f"{PREFIX}DUP",
            "display_name": "重复",
            "password": PASSWORD,
            "role_codes": ["viewer"],
        },
        headers=bearer(token),
    )
    assert again.status_code == 409 and again.json()["error"]["code"] == "USERNAME_EXISTS"


def test_role_grant_limits_self_escalation_and_unknown_role(client: TestClient) -> None:
    token = admin_token(client)
    # administrator（非 super_admin）不能授予 super_admin
    admin2 = make_user(client, token, "admin2", ["administrator"])
    admin2_token = activate(client, f"{PREFIX}admin2")
    victim = make_user(client, token, "victim", ["viewer"])
    grant_super = client.put(
        f"/api/v1/admin/users/{victim['id']}/roles",
        json={"role_codes": ["super_admin"]},
        headers=bearer(admin2_token),
    )
    assert grant_super.status_code == 403
    assert grant_super.json()["error"]["code"] == "ROLE_GRANT_FORBIDDEN"
    # 自我提权：不能改自己的角色
    self_up = client.put(
        f"/api/v1/admin/users/{admin2['id']}/roles",
        json={"role_codes": ["super_admin"]},
        headers=bearer(admin2_token),
    )
    assert self_up.status_code == 403
    # 不能管理 super_admin 账号（停用 / 重置 bootstrap admin）
    with SessionLocal() as db:
        root = db.scalar(select(User).where(User.username == "admin"))
        assert root
        root_id = root.id
    assert (
        client.put(
            f"/api/v1/admin/users/{root_id}/status",
            json={"status": "DISABLED"},
            headers=bearer(admin2_token),
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/admin/users/{root_id}/reset-password",
            json={"new_password": PASSWORD},
            headers=bearer(admin2_token),
        ).status_code
        == 403
    )
    # 错误角色
    unknown = client.put(
        f"/api/v1/admin/users/{victim['id']}/roles",
        json={"role_codes": ["god_mode"]},
        headers=bearer(token),
    )
    assert unknown.status_code == 400 and unknown.json()["error"]["code"] == "ROLE_UNKNOWN"
    # 没有 user.manage 的操作员访问账号管理 = 403
    make_user(client, token, "plainop", ["operator"])
    op_token = activate(client, f"{PREFIX}plainop")
    assert client.get("/api/v1/admin/users", headers=bearer(op_token)).status_code == 403
    assert (
        client.post(
            "/api/v1/admin/users",
            json={
                "username": f"{PREFIX}evil",
                "display_name": "x",
                "password": PASSWORD,
                "role_codes": ["super_admin"],
            },
            headers=bearer(op_token),
        ).status_code
        == 403
    )


def test_admin_cannot_change_own_roles_or_status(client: TestClient) -> None:
    token = admin_token(client)
    with SessionLocal() as db:
        root = db.scalar(select(User).where(User.username == "admin"))
        assert root
        root_id = root.id
    for path, body in (
        (f"/api/v1/admin/users/{root_id}/roles", {"role_codes": ["viewer"]}),
        (f"/api/v1/admin/users/{root_id}/status", {"status": "DISABLED"}),
    ):
        response = client.put(path, json=body, headers=bearer(token))
        assert response.status_code == 403, path
    assert (
        client.post(
            f"/api/v1/admin/users/{root_id}/reset-password",
            json={"new_password": PASSWORD},
            headers=bearer(token),
        ).status_code
        == 403
    )


def test_last_admin_protected_under_concurrent_demotion() -> None:
    """并发反证：调用方在请求处理中途已不再是有效管理员（例如被另一管理员同时停用），
    此时唯一剩下的管理员不得被降级或停用。

    整个用例在外层连接事务 + SAVEPOINT 内执行，服务函数内部的 commit 只释放 savepoint，
    结束时回滚外层事务，绝不改动共享测试库中的其他账号。
    """
    from app.core.dependencies import AuthContext
    from app.core.errors import PlatformError
    from app.db.session import engine
    from app.modules.admin.router import UserStatusInput, _set_roles, set_user_status
    from sqlalchemy.orm import Session
    from starlette.requests import Request

    scope = {
        "type": "http",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "path": "/",
        "query_string": b"",
    }
    connection = engine.connect()
    outer = connection.begin()
    db = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        super_role = db.scalar(select(Role).where(Role.code == "super_admin"))
        assert super_role
        last = User(
            username=f"{PREFIX}last",
            display_name="唯一管理员",
            password_hash="x",
            must_change_password=False,
        )
        caller = User(
            username=f"{PREFIX}caller",
            display_name="已被并发停用的管理员",
            password_hash="x",
            must_change_password=False,
            status="DISABLED",
        )
        db.add_all([last, caller])
        db.flush()
        for row in (last, caller):
            db.execute(insert(user_roles).values(user_id=row.id, role_id=super_role.id))
        for other in db.scalars(
            select(User)
            .join(user_roles, user_roles.c.user_id == User.id)
            .join(Role, Role.id == user_roles.c.role_id)
            .where(Role.code.in_({"super_admin", "administrator"}), User.id != last.id)
        ).all():
            other.status = "DISABLED"
        db.flush()
        auth = AuthContext(user=caller, permissions={"user.manage", "role.manage"})
        with pytest.raises(PlatformError) as demote:
            _set_roles(last.id, ["viewer"], Request(scope), db, auth)
        assert demote.value.code == "LAST_ADMIN_PROTECTED"
        with pytest.raises(PlatformError) as disable:
            set_user_status(last.id, UserStatusInput(status="DISABLED"), Request(scope), db, auth)
        assert disable.value.code == "LAST_ADMIN_PROTECTED"
    finally:
        db.close()
        outer.rollback()
        connection.close()
    with SessionLocal() as check:
        assert check.scalar(select(User).where(User.username == f"{PREFIX}last")) is None


def test_websocket_and_media_ticket_revoked_after_disable(client: TestClient) -> None:
    token = admin_token(client)
    user = make_user(client, token, "wsop", ["operator"])
    op_token = activate(client, f"{PREFIX}wsop")
    ticket = client.post("/api/v1/auth/ws-ticket", headers=bearer(op_token)).json()["ticket"]
    with SessionLocal() as db:
        r001 = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert r001
        media = encode_media_ticket(
            user_id=user["id"],
            robot_id=r001.id,
            camera="roof_rgb",
            stream_id="R001-roof_rgb",
            token_version=1,  # 改密后 token_version=1
        )
    read = {"action": "read", "protocol": "webrtc", "path": "R001-roof_rgb"}
    assert client.post("/api/v1/media/authorize", json={**read, "token": media}).status_code == 200
    with client.websocket_connect(f"/ws/v1/monitor?ticket={ticket}") as websocket:
        first = websocket.receive_json()
        assert first["event_type"] in {"heartbeat", "resync_required"} or first.get("stream_id")
        client.put(
            f"/api/v1/admin/users/{user['id']}/status",
            json={"status": "DISABLED"},
            headers=bearer(token),
        )
        revoked = None
        for _ in range(10):
            try:
                event = websocket.receive_json()
            except WebSocketDisconnect:
                revoked = "closed"
                break
            if event.get("event_type") == "session_revoked":
                revoked = "event"
                break
        assert revoked is not None
    # 停用后媒体票据同样失效
    assert client.post("/api/v1/media/authorize", json={**read, "token": media}).status_code == 403
    # 停用后新 WS 票据无法获取
    assert client.post("/api/v1/auth/ws-ticket", headers=bearer(op_token)).status_code == 401


# ---------------------------------------------------------------- 站点隔离


def _other_site_robot() -> tuple[str, str]:
    with SessionLocal.begin() as db:
        site = Site(code="R1MIN_OTHER", name="其他站点", active=True)
        db.add(site)
        db.flush()
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        assert robot
        original = robot.site_id
        robot.site_id = site.id
        return robot.id, original


def test_cross_site_api_ws_media_and_export_blocked(client: TestClient) -> None:
    token = admin_token(client)
    make_user(client, token, "siteop", ["operator"])
    op_token = activate(client, f"{PREFIX}siteop")
    robot_id, original_site = _other_site_robot()
    try:
        with SessionLocal.begin() as db:
            db.add(
                FireEvent(
                    event_code=f"FE-R1MIN-{uuid4().hex[:6]}",
                    robot_id=robot_id,
                    parking_slot_id=None,
                    location_state="UNMATCHED",
                    detection_method="AUTO",
                    fire_type="smoke",
                    severity="HIGH",
                    fingerprint=uuid4().hex,
                    state="NEW",
                )
            )
        robots = client.get("/api/v1/robots", headers=bearer(op_token)).json()
        assert all(row["id"] != robot_id for row in robots)
        assert client.get("/api/v1/robots/R002", headers=bearer(op_token)).status_code == 404
        assert (
            client.get(
                "/api/v1/history/telemetry", params={"robot_id": "R002"}, headers=bearer(op_token)
            ).status_code
            == 404
        )
        snapshot = client.get("/api/v1/monitor/snapshot", headers=bearer(op_token)).json()
        assert all(row["id"] != robot_id for row in snapshot["robots"])
        assert all(row.get("robot_id") != robot_id for row in snapshot["alarms"])
        assert all(row["robot_id"] != robot_id for row in snapshot["streams"])
        alarms = client.get("/api/v1/alarms", headers=bearer(op_token)).json()
        assert all(row.get("robot_id") != robot_id for row in alarms)
        streams = client.get("/api/v1/media/streams", headers=bearer(op_token)).json()
        assert all(row["robot_id"] != robot_id for row in streams)
        with SessionLocal() as db:
            other_stream = db.scalar(
                select(StreamRegistry).where(StreamRegistry.robot_id == robot_id)
            )
        if other_stream:
            assert (
                client.post(
                    "/api/v1/media/tickets",
                    json={"stream_id": other_stream.stream_id},
                    headers=bearer(op_token),
                ).status_code
                == 404
            )
        # 管理员（settings.manage）不受限
        admin_robots = client.get("/api/v1/robots", headers=bearer(token)).json()
        assert any(row["id"] == robot_id for row in admin_robots)
        # WS：跨站点车辆事件不推送给普通账号
        from app.core.events import append_event

        ticket = client.post("/api/v1/auth/ws-ticket", headers=bearer(op_token)).json()["ticket"]
        with client.websocket_connect(f"/ws/v1/monitor?ticket={ticket}&after=0-0") as websocket:
            hidden = append_event("vehicle.location", {"vehicle_id": "R002", "x": 1, "y": 1})
            shown = append_event("vehicle.location", {"vehicle_id": "R001", "x": 2, "y": 2})
            seen = []
            for _ in range(30):
                event = websocket.receive_json()
                if event.get("stream_id"):
                    seen.append(event["stream_id"])
                if shown in seen:
                    break
            assert shown in seen and hidden not in seen
        # 导出同样受范围约束
        csv = client.get("/api/v1/history/export/alarms", headers=bearer(op_token))
        assert csv.status_code == 200 and "R002" not in csv.text
    finally:
        with SessionLocal.begin() as db:
            robot = db.get(Robot, robot_id)
            assert robot
            robot.site_id = original_site
            db.execute(delete(FireEvent).where(FireEvent.event_code.like("FE-R1MIN-%")))


# ---------------------------------------------------------------- 巡检计划 PREVIEW_ONLY


def test_plan_draft_preview_edit_pause_never_creates_command(client: TestClient) -> None:
    token = admin_token(client)
    make_user(client, token, "planner", ["operator"])
    op_token = activate(client, f"{PREFIX}planner")
    robot_id, trajectory_id = route_for()
    before = downlink_counts()

    preview = client.post(
        "/api/v1/plans/preview",
        json={
            "schedule": {
                "frequency": "WEEKLY",
                "time": "08:00",
                "weekdays": ["MON", "FRI"],
                "timezone": "Asia/Shanghai",
            },
            "count": 4,
        },
        headers=bearer(op_token),
    )
    assert preview.status_code == 200
    body = preview.json()
    assert body["cron_expression"] == "0 8 * * 1,5" and len(body["occurrences"]) == 4
    assert body["real_execution"] is False

    key = str(uuid4())
    payload = plan_body(f"右侧日常巡检-{uuid4().hex[:4]}", robot_id, trajectory_id)
    created = client.post(
        "/api/v1/plans", json=payload, headers=bearer(op_token, **{"Idempotency-Key": key})
    )
    assert created.status_code == 201, created.text
    plan = created.json()
    assert plan["execution_mode"] == "PREVIEW_ONLY" and plan["enabled"] is False
    assert plan["status"] == "DRAFT" and plan["work_side"] == "RIGHT"
    assert plan["lateral_verification"] == "NOT_VERIFIED" and plan["real_execution"] is False
    # 重复保存（同一幂等键）→ 同一计划，不重复创建
    again = client.post(
        "/api/v1/plans", json=payload, headers=bearer(op_token, **{"Idempotency-Key": key})
    )
    assert again.status_code == 201 and again.json()["id"] == plan["id"]
    # 重复保存（新幂等键、同名）→ 409
    dup = client.post(
        "/api/v1/plans",
        json=payload,
        headers=bearer(op_token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "PLAN_NAME_EXISTS"

    detail = client.get(f"/api/v1/plans/{plan['id']}", headers=bearer(op_token)).json()
    assert detail["right_side_mechanism"]["verification_state"] == "NOT_VERIFIED"
    assert detail["right_side_mechanism"]["control_enabled"] is False

    recorded = client.post(f"/api/v1/plans/{plan['id']}/preview-run", headers=bearer(op_token))
    assert recorded.status_code == 201 and recorded.json()["real_execution"] is False
    detail = client.get(f"/api/v1/plans/{plan['id']}", headers=bearer(op_token)).json()
    assert detail["previews"] and all(p["state"] == "PREVIEW" for p in detail["previews"])

    edit = client.put(
        f"/api/v1/plans/{plan['id']}",
        json={**payload, "target_lateral_min_m": 0.20, "revision": 1},
        headers=bearer(op_token),
    )
    assert edit.status_code == 200 and edit.json()["revision"] == 2
    stale = client.put(
        f"/api/v1/plans/{plan['id']}",
        json={**payload, "revision": 1},
        headers=bearer(op_token),
    )
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "PLAN_REVISION_CONFLICT"
    paused = client.post(f"/api/v1/plans/{plan['id']}/pause", headers=bearer(op_token))
    assert paused.status_code == 200 and paused.json()["status"] == "PAUSED"

    # 计划误启用：API 显式拒绝 + 审计
    enable = client.post(f"/api/v1/plans/{plan['id']}/enable", headers=bearer(op_token))
    assert enable.status_code == 409
    assert enable.json()["error"]["code"] == "PLAN_EXECUTION_FORBIDDEN"
    sneaky = client.post(
        "/api/v1/plans",
        json=plan_body("偷偷启用", robot_id, trajectory_id, enabled=True),
        headers=bearer(op_token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert sneaky.status_code == 409
    legacy_mode = client.post(
        "/api/v1/plans",
        json=plan_body("偷偷改模式", robot_id, trajectory_id, execution_mode="LEGACY_EXECUTABLE"),
        headers=bearer(op_token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert legacy_mode.status_code == 409
    legacy = client.post(
        "/api/v1/patrol-plans",
        json={
            "code": f"LEG{uuid4().hex[:6]}",
            "name": "旧接口启用",
            "robot_id": "R001",
            "map_version_id": detail["map_version_id"],
            "enabled": True,
        },
        headers=bearer(token),
    )
    assert legacy.status_code == 409
    sched = client.post(
        "/api/v1/patrol-schedules",
        json={"patrol_plan_id": plan["id"], "cron_expression": "* * * * *", "enabled": True},
        headers=bearer(token),
    )
    assert sched.status_code == 409
    # 直接用旧执行入口触发 PREVIEW_ONLY 计划 → 服务端硬拒绝
    run = client.post(
        "/api/v1/tasks/patrol-plan",
        json={"robot_id": "R001", "patrol_plan_id": plan["id"]},
        headers=bearer(token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert run.status_code == 409
    assert run.json()["error"]["code"] == "PATROL_PLAN_PREVIEW_ONLY"
    with SessionLocal.begin() as db:
        row = db.get(PatrolPlan, plan["id"])
        assert row
        row.enabled = True
        with pytest.raises(IntegrityError):  # DB CHECK 兜底：PREVIEW_ONLY 不得 enabled
            db.flush()
    assert downlink_counts() == before


def test_plan_requires_real_vehicle_and_route(client: TestClient) -> None:
    token = admin_token(client)
    robot_id, trajectory_id = route_for()
    no_vehicle = client.post(
        "/api/v1/plans",
        json=plan_body("无车辆", "NO-SUCH-VEHICLE", trajectory_id),
        headers=bearer(token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert no_vehicle.status_code == 422
    assert no_vehicle.json()["error"]["code"] == "PLAN_ROBOT_REQUIRED"
    no_route = client.post(
        "/api/v1/plans",
        json=plan_body("无路线", robot_id, "NO-SUCH-ROUTE"),
        headers=bearer(token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert no_route.status_code == 422
    assert no_route.json()["error"]["code"] == "PLAN_ROUTE_REQUIRED"
    missing = client.post(
        "/api/v1/plans",
        json={k: v for k, v in plan_body("缺路线", robot_id, "x").items() if k != "trajectory_id"},
        headers=bearer(token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert missing.status_code == 422
    out_of_range = client.post(
        "/api/v1/plans",
        json=plan_body("越界", robot_id, trajectory_id, target_lateral_max_m=0.35),
        headers=bearer(token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert out_of_range.status_code == 422
    left = client.post(
        "/api/v1/plans",
        json=plan_body("左侧", robot_id, trajectory_id, work_side="LEFT"),
        headers=bearer(token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert left.status_code == 422
    viewer = make_user(client, token, "viewonly", ["viewer"])
    assert viewer
    viewer_token = activate(client, f"{PREFIX}viewonly")
    forbidden = client.post(
        "/api/v1/plans",
        json=plan_body("越权", robot_id, trajectory_id),
        headers=bearer(viewer_token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert forbidden.status_code == 403
    options = client.get("/api/v1/plans/options", headers=bearer(token)).json()
    assert options["execution_mode"] == "PREVIEW_ONLY" and options["lateral_range_m"] == [0.2, 0.3]


def test_worker_never_dispatches_preview_only_plan(client: TestClient) -> None:
    worker = load_service("firebot_r1min_worker", "services/task-worker/main.py")
    token = admin_token(client)
    robot_id, trajectory_id = route_for()
    plan = client.post(
        "/api/v1/plans",
        json=plan_body(f"调度阻断-{uuid4().hex[:4]}", robot_id, trajectory_id),
        headers=bearer(token, **{"Idempotency-Key": str(uuid4())}),
    ).json()
    before = downlink_counts()
    now = datetime.now(UTC)
    with SessionLocal.begin() as db:
        schedule = db.scalar(
            select(PatrolSchedule).where(PatrolSchedule.patrol_plan_id == plan["id"])
        )
        assert schedule and schedule.enabled is False and schedule.next_run_at is None
        # 篡改：即便有人在 DB 中把调度强行启用并设为到期，worker 也必须拒绝。
        schedule.enabled = True
        schedule.next_run_at = now - timedelta(seconds=1)
        occurrence = PatrolScheduleOccurrence(
            schedule_id=schedule.id, scheduled_for=now - timedelta(seconds=2), state="QUEUED"
        )
        db.add(occurrence)
        db.flush()
        occurrence_id = occurrence.id
    with SessionLocal.begin() as db:
        worker.run_patrol_scheduler(db, now)
    with SessionLocal() as db:
        occurrence = db.get(PatrolScheduleOccurrence, occurrence_id)
        assert occurrence and occurrence.state == "REJECTED"
        assert occurrence.reason_code == "PREVIEW_ONLY" and occurrence.task_id is None
    with SessionLocal.begin() as db:
        from app.modules.tasks.patrol import build_patrol_task

        row = db.get(PatrolPlan, plan["id"])
        robot = db.get(Robot, robot_id)
        assert row and robot
        with pytest.raises(Exception) as caught:
            build_patrol_task(db, plan=row, robot=robot, actor_id=row.created_by, source="TEST")
        assert getattr(caught.value, "code", "") == "PATROL_PLAN_PREVIEW_ONLY"
    assert downlink_counts() == before


# ---------------------------------------------------------------- 火警


def _close_active_alarms() -> None:
    """隔离：关闭既有活动火警，避免 5 分钟指纹窗口把本用例事件合并进旧事件。"""
    with SessionLocal.begin() as db:
        for row in db.scalars(
            select(FireEvent).where(FireEvent.state.not_in({"RESOLVED", "CLOSED", "DISMISSED"}))
        ).all():
            row.state = "CLOSED"


def _alarm_msg(slot_code: str | None, event_id: str) -> dict:
    return {
        "message_id": str(uuid4()),
        "event_id": event_id,
        "fire_type": "smoke",
        "severity": "HIGH",
        "confidence": 0.9,
        "parking_slot_code": slot_code,
        "position": {"x": 1.0, "y": 2.0, "theta": 0.0},
    }


def test_unmatched_location_alarm_is_kept_and_not_auto_dispatched(client: TestClient) -> None:
    ingress = load_service("firebot_r1min_ingress", "services/mqtt-ingress/main.py")
    _close_active_alarms()
    now = datetime.now(UTC)
    event_id = f"r1min-{uuid4().hex[:8]}"
    unknown_code = f"ZZ-{uuid4().hex[:4]}"
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot
        ingress.handle_alarm(db, robot, _alarm_msg(unknown_code, event_id), now)
        ingress.handle_alarm(db, robot, _alarm_msg(None, f"{event_id}-b"), now)
    with SessionLocal() as db:
        rows = db.scalars(
            select(FireEvent).where(FireEvent.source_event_id.like(f"{event_id}%"))
        ).all()
        assert len(rows) == 2
        assert all(r.location_state == "UNMATCHED" and r.parking_slot_id is None for r in rows)
        assert {r.reported_slot_code for r in rows} == {unknown_code, None}
        alarm_id = next(r.id for r in rows if r.reported_slot_code == unknown_code)
    token = admin_token(client)
    detail = client.get(f"/api/v1/alarms/{alarm_id}", headers=bearer(token)).json()
    assert "位置未匹配" in detail["location_label"]
    before = downlink_counts()
    dispatch = client.post(
        f"/api/v1/alarms/{alarm_id}/create-task",
        json={"robot_id": "R001", "parameters": {"extinguish_mode": "DEPLOY_BLANKET"}},
        headers=bearer(token, **{"Idempotency-Key": str(uuid4())}),
    )
    assert dispatch.status_code == 409
    assert dispatch.json()["error"]["code"] == "ALARM_LOCATION_UNMATCHED"
    assert downlink_counts() == before
    # 未确认火警出现在活动列表；确认收到后带处置备注并写审计
    active = client.get("/api/v1/alarms", params={"active": True}, headers=bearer(token)).json()
    assert any(r["id"] == alarm_id and r["state"] == "NEW" for r in active)
    ack = client.post(
        f"/api/v1/alarms/{alarm_id}/acknowledge",
        json={"note": "已通知现场核实"},
        headers=bearer(token),
    )
    assert ack.status_code == 200 and ack.json()["state"] == "ACKNOWLEDGED"
    assert ack.json()["ack_by_name"]
    timeline = client.get(f"/api/v1/alarms/{alarm_id}/timeline", headers=bearer(token)).json()
    assert [(i["source_type"], i["state"]) for i in timeline] == [
        ("ALARM", "NEW"),
        ("ALARM", "ACKNOWLEDGED"),
    ]
    actions = client.get(f"/api/v1/alarms/{alarm_id}/actions", headers=bearer(token)).json()
    assert [(a["action"], a["note"]) for a in actions] == [("ALARM_ACKNOWLEDGED", "已通知现场核实")]
    assert actions[0]["actor"] and actions[0]["before_state"] == "NEW"
    again = client.post(f"/api/v1/alarms/{alarm_id}/acknowledge", headers=bearer(token))
    assert again.status_code == 409


def test_repeat_event_after_resolved_creates_new_alarm(client: TestClient) -> None:
    ingress = load_service("firebot_r1min_ingress_repeat", "services/mqtt-ingress/main.py")
    _close_active_alarms()
    now = datetime.now(UTC)
    event_id = f"r1min-rep-{uuid4().hex[:8]}"
    with SessionLocal() as db:
        slot = db.scalar(select(ParkingSlot).where(ParkingSlot.code == "A-12"))
        assert slot
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot
        ingress.handle_alarm(db, robot, _alarm_msg("A-12", event_id), now)
        # 活动期内重复上报 → 合并计数
        ingress.handle_alarm(db, robot, _alarm_msg("A-12", event_id), now + timedelta(seconds=5))
    with SessionLocal.begin() as db:
        first = db.scalar(select(FireEvent).where(FireEvent.source_event_id == event_id))
        assert first and first.occurrence_count == 2 and first.location_state == "MATCHED"
        first.state = "RESOLVED"
        first.resolved_at = now + timedelta(seconds=6)
        first_id = first.id
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        assert robot
        # 已解决后同 event_id / 同指纹再次上报 → 新事件，不归入已解决事件
        ingress.handle_alarm(db, robot, _alarm_msg("A-12", event_id), now + timedelta(seconds=30))
    with SessionLocal() as db:
        rows = db.scalars(select(FireEvent).where(FireEvent.source_event_id == event_id)).all()
        assert len(rows) == 2
        old = next(r for r in rows if r.id == first_id)
        new = next(r for r in rows if r.id != first_id)
        assert old.state == "RESOLVED" and old.occurrence_count == 2
        assert new.state == "NEW" and new.occurrence_count == 1


# ---------------------------------------------------------------- 记录与导出


def test_records_filters_and_export_permission(client: TestClient) -> None:
    token = admin_token(client)
    make_user(client, token, "reader", ["viewer"])
    viewer_token = activate(client, f"{PREFIX}reader")
    make_user(client, token, "auditor", ["auditor"])
    auditor_token = activate(client, f"{PREFIX}auditor")
    # viewer：可看任务，但不能导出，也看不到审计
    assert client.get("/api/v1/history/tasks", headers=bearer(viewer_token)).status_code == 200
    denied = client.get("/api/v1/history/export/tasks", headers=bearer(viewer_token))
    assert denied.status_code == 403
    assert client.get("/api/v1/history/audit", headers=bearer(viewer_token)).status_code == 403
    assert (
        client.get("/api/v1/history/export/audit", headers=bearer(viewer_token)).status_code == 403
    )
    # auditor：可导出审计，导出与页面筛选一致且留痕
    page = client.get(
        "/api/v1/history/audit", params={"action": "USER_CREATE"}, headers=bearer(auditor_token)
    ).json()
    exported = client.get(
        "/api/v1/history/export/audit",
        params={"action": "USER_CREATE"},
        headers=bearer(auditor_token),
    )
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    lines = [line for line in exported.text.lstrip("﻿").splitlines() if line]
    assert len(lines) - 1 == len(page)
    trail = client.get(
        "/api/v1/history/audit", params={"action": "EXPORT_"}, headers=bearer(token)
    ).json()
    assert {"EXPORT_RECORDS", "EXPORT_DENIED"} <= {row["action"] for row in trail}
    ops = client.get("/api/v1/history/operations", headers=bearer(auditor_token)).json()
    assert ops and all(row["actor_type"] == "USER" for row in ops)
    previews = client.get("/api/v1/history/plan-previews", headers=bearer(token))
    assert previews.status_code == 200


def test_csv_export_neutralises_formula_injection() -> None:
    from app.modules.history.router import _csv_cell

    assert _csv_cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"
    assert _csv_cell("@cmd") == "'@cmd"
    assert _csv_cell("normal") == "normal"
    assert _csv_cell(None) == ""


def test_operating_site_selection_is_stable() -> None:
    from app.core.scope import operating_site

    with SessionLocal() as db:
        site = operating_site(db)
        first_active = db.scalar(select(Site).where(Site.active.is_(True)).order_by(Site.code))
        assert site is not None and first_active is not None and site.id == first_active.id
        assert db.scalar(select(Map).where(Map.site_id == site.id)) is not None
