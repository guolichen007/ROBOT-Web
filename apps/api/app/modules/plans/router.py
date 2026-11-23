"""WEB-R1-MIN 巡检计划（草稿 / 编辑 / 暂停 / 时间预演）。

复用 PatrolPlan + PatrolSchedule，不新建调度系统。安全边界：
- 本模块创建 / 编辑的计划一律 ``execution_mode=PREVIEW_ONLY``、``enabled=False``；
  数据库 CHECK ``ck_patrol_plan_preview_not_enabled`` 二次兜底。
- 关联的 PatrolSchedule 一律 ``enabled=False``、``next_run_at=None``，task-worker 不会选中。
- ``build_patrol_task`` 与 task-worker 对 PREVIEW_ONLY 计划硬拒绝（不创建 Task/Command）。
- 预演只计算未来触发时间并写入 PREVIEW 记录，不接触 Task / Command / Outbox / MQTT。
- 右侧作业方向与 0.20–0.30 m 目标侧向间距仅为配置与展示，不进入导航或机械控制。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from croniter import croniter
from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.dependencies import AuthContext, DbSession, request_meta, require_permission
from app.core.errors import PlatformError
from app.core.idempotency import lookup, store
from app.core.scope import robot_visible, scoped_robot_ids
from app.db.models import (
    Map,
    MapVersion,
    PatrolPlan,
    PatrolPlanPoint,
    PatrolSchedule,
    PatrolScheduleOccurrence,
    Robot,
    RobotSensorProfile,
    Trajectory,
    User,
)
from app.modules.robots.router import find_robot

router = APIRouter(prefix="/api/v1/plans", tags=["patrol-plans"])

PREVIEW_ONLY = "PREVIEW_ONLY"
LATERAL_MIN_M = 0.20
LATERAL_MAX_M = 0.30
WEEKDAYS = {"MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6, "SUN": 0}
PREVIEW_COUNT_MAX = 20


class ScheduleSpec(BaseModel):
    """表单级周期：每天 / 每周 + 时间（服务端转换为五段 cron）。"""

    frequency: str = Field(pattern=r"^(DAILY|WEEKLY)$")
    time: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[str] = Field(default_factory=list, max_length=7)
    timezone: str = "Asia/Shanghai"

    @model_validator(mode="after")
    def _weekly_needs_days(self) -> ScheduleSpec:
        if self.frequency == "WEEKLY":
            if not self.weekdays:
                raise ValueError("每周计划至少选择一天")
            unknown = [day for day in self.weekdays if day not in WEEKDAYS]
            if unknown:
                raise ValueError(f"未知星期：{','.join(unknown)}")
        return self

    def cron(self) -> str:
        hour, minute = self.time.split(":")
        if self.frequency == "DAILY":
            return f"{int(minute)} {int(hour)} * * *"
        days = ",".join(
            str(WEEKDAYS[d]) for d in sorted(set(self.weekdays), key=list(WEEKDAYS).index)
        )
        return f"{int(minute)} {int(hour)} * * {days}"


class PlanInput(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    robot_id: str = Field(min_length=1)
    trajectory_id: str = Field(min_length=1)
    schedule: ScheduleSpec
    target_lateral_min_m: float = Field(default=LATERAL_MIN_M, ge=LATERAL_MIN_M, le=LATERAL_MAX_M)
    target_lateral_max_m: float = Field(default=LATERAL_MAX_M, ge=LATERAL_MIN_M, le=LATERAL_MAX_M)
    work_side: str = Field(default="RIGHT", pattern=r"^RIGHT$")
    description: str = Field(default="", max_length=2000)
    # 只用于检测前端误提交：任何 true 值都被拒绝，而不是静默忽略。
    enabled: bool = False
    execution_mode: str = PREVIEW_ONLY

    @model_validator(mode="after")
    def _lateral_order(self) -> PlanInput:
        if self.target_lateral_min_m > self.target_lateral_max_m:
            raise ValueError("目标侧向间距下限不能大于上限")
        return self


class PlanUpdateInput(PlanInput):
    revision: int = Field(ge=1)


class PreviewInput(BaseModel):
    schedule: ScheduleSpec
    count: int = Field(default=5, ge=1, le=PREVIEW_COUNT_MAX)


def cron_preview(spec: ScheduleSpec, count: int, base: datetime | None = None) -> list[str]:
    try:
        zone = ZoneInfo(spec.timezone)
    except Exception as exc:
        raise PlatformError(
            "INVALID_TIMEZONE", "时区必须是有效的 IANA 时区", status_code=400
        ) from exc
    expression = spec.cron()
    if not croniter.is_valid(expression):
        raise PlatformError("INVALID_CRON", "执行周期无效", status_code=400)
    iterator = croniter(expression, (base or datetime.now(UTC)).astimezone(zone))
    return [iterator.get_next(datetime).isoformat() for _ in range(count)]


def _reject_execution_flags(payload: PlanInput) -> None:
    if payload.enabled or payload.execution_mode != PREVIEW_ONLY:
        raise PlatformError(
            "PLAN_EXECUTION_FORBIDDEN",
            "本期巡检计划仅支持保存草稿与时间预演，禁止启用真实执行",
            status_code=409,
            details={"execution_mode": PREVIEW_ONLY},
        )


def _resolve_config(
    db: Session, auth: AuthContext, payload: PlanInput
) -> tuple[Robot, Trajectory, MapVersion]:
    try:
        robot = find_robot(db, payload.robot_id, auth)
    except Exception as exc:
        raise PlatformError(
            "PLAN_ROBOT_REQUIRED", "请选择已登记且在本站点范围内的执行车辆", status_code=422
        ) from exc
    trajectory = db.get(Trajectory, payload.trajectory_id)
    if not trajectory or not trajectory.enabled:
        raise PlatformError(
            "PLAN_ROUTE_REQUIRED", "所选巡检路线不存在或已停用，请选择已有路线", status_code=422
        )
    version = db.get(MapVersion, trajectory.map_version_id)
    if not version or version.status != "PUBLISHED":
        raise PlatformError(
            "PLAN_ROUTE_REQUIRED", "巡检路线必须属于已发布地图版本", status_code=422
        )
    map_row = db.get(Map, version.map_id)
    if robot.site_id and map_row and map_row.site_id != robot.site_id:
        raise PlatformError(
            "PLAN_ROUTE_SITE_MISMATCH", "巡检路线与车辆不属于同一站点", status_code=422
        )
    return robot, trajectory, version


def right_side_mechanism(db: Session, robot: Robot) -> dict[str, Any]:
    """右侧防火罩机构状态：只展示登记信息，未经现场验收一律 NOT_VERIFIED。"""
    profile = db.scalar(
        select(RobotSensorProfile).where(
            RobotSensorProfile.robot_id == robot.id,
            RobotSensorProfile.channel == "right_fire_blanket",
        )
    )
    return {
        "mechanism": "RIGHT_FIRE_BLANKET",
        "registered": bool(profile),
        "support_state": profile.support_state if profile else "NOT_CONNECTED",
        "verification_state": "NOT_VERIFIED",
        "control_enabled": False,
        "note": "防火罩展开与精确侧向距离尚未现场验收；本平台不发送任何机构控制指令",
    }


def _schedule_for(db: Session, plan: PatrolPlan) -> PatrolSchedule | None:
    return db.scalar(
        select(PatrolSchedule)
        .where(PatrolSchedule.patrol_plan_id == plan.id)
        .order_by(PatrolSchedule.created_at.desc())
    )


def plan_view(db: Session, plan: PatrolPlan, *, detail: bool = False) -> dict[str, Any]:
    robot = db.get(Robot, plan.robot_id)
    trajectory = db.get(Trajectory, plan.trajectory_id) if plan.trajectory_id else None
    version = db.get(MapVersion, plan.map_version_id)
    creator = db.get(User, plan.created_by)
    schedule = _schedule_for(db, plan)
    lateral_verified = False  # 未经实测，永远不宣称已验证
    view: dict[str, Any] = {
        "id": plan.id,
        "code": plan.code,
        "name": plan.name,
        "description": plan.description,
        "status": plan.status,
        "execution_mode": plan.execution_mode,
        "enabled": plan.enabled,
        "revision": plan.revision,
        "robot_id": plan.robot_id,
        "robot_vehicle_id": robot.vehicle_id if robot else None,
        "robot_name": robot.name if robot else None,
        "trajectory_id": plan.trajectory_id,
        "trajectory_code": trajectory.code if trajectory else None,
        "map_version_id": plan.map_version_id,
        "map_version": version.version if version else None,
        "work_side": plan.work_side,
        "target_lateral_min_m": plan.target_lateral_min_m,
        "target_lateral_max_m": plan.target_lateral_max_m,
        "lateral_verification": "VERIFIED" if lateral_verified else "NOT_VERIFIED",
        "schedule": plan.schedule_json or {},
        "cron_expression": schedule.cron_expression if schedule else None,
        "schedule_enabled": bool(schedule and schedule.enabled),
        "created_by": plan.created_by,
        "created_by_name": creator.display_name if creator else None,
        "created_at": plan.created_at.isoformat() if plan.created_at else None,
        "updated_at": plan.updated_at.isoformat() if plan.updated_at else None,
        "real_execution": False if plan.execution_mode == PREVIEW_ONLY else plan.enabled,
    }
    if detail:
        view["right_side_mechanism"] = right_side_mechanism(db, robot) if robot else None
        view["previews"] = []
        if schedule:
            view["previews"] = [
                {
                    "id": row.id,
                    "scheduled_for": row.scheduled_for.isoformat(),
                    "state": row.state,
                    "reason_code": row.reason_code,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                }
                for row in db.scalars(
                    select(PatrolScheduleOccurrence)
                    .where(PatrolScheduleOccurrence.schedule_id == schedule.id)
                    .order_by(PatrolScheduleOccurrence.scheduled_for)
                    .limit(200)
                ).all()
            ]
        view["points"] = [
            {"sequence": p.sequence, "navigation_preset_id": p.navigation_preset_id}
            for p in db.scalars(
                select(PatrolPlanPoint)
                .where(PatrolPlanPoint.patrol_plan_id == plan.id)
                .order_by(PatrolPlanPoint.sequence)
            ).all()
        ]
    return view


def _get_plan(db: Session, auth: AuthContext, plan_id: str) -> PatrolPlan:
    plan = db.get(PatrolPlan, plan_id)
    robot = db.get(Robot, plan.robot_id) if plan else None
    if not plan or not robot or not robot_visible(db, auth.permissions, robot):
        raise PlatformError("RESOURCE_NOT_FOUND", "巡检计划不存在", status_code=404)
    return plan


def _next_code(db: Session) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    count = db.scalar(
        select(func.count(PatrolPlan.id)).where(PatrolPlan.code.like(f"WEB-{stamp}-%"))
    )
    return f"WEB-{stamp}-{int(count or 0) + 1:03d}"


def _upsert_schedule(
    db: Session, plan: PatrolPlan, spec: ScheduleSpec, actor_id: str
) -> PatrolSchedule:
    schedule = _schedule_for(db, plan)
    if not schedule:
        schedule = PatrolSchedule(patrol_plan_id=plan.id, created_by=actor_id)
        db.add(schedule)
    schedule.cron_expression = spec.cron()
    schedule.timezone = spec.timezone
    # 预演计划的调度永远不可被 task-worker 选中。
    schedule.enabled = False
    schedule.next_run_at = None
    return schedule


@router.get("")
def list_plans(
    db: DbSession,
    auth: AuthContext = Depends(require_permission("robot.read")),
    status: str | None = None,
    robot_id: str | None = None,
    q: str | None = None,
) -> list[dict]:
    query = select(PatrolPlan).order_by(PatrolPlan.updated_at.desc())
    visible = scoped_robot_ids(db, auth.permissions)
    if visible is not None:
        query = query.where(PatrolPlan.robot_id.in_(visible or {""}))
    if status:
        query = query.where(PatrolPlan.status == status)
    if robot_id:
        robot = find_robot(db, robot_id, auth)
        query = query.where(PatrolPlan.robot_id == robot.id)
    if q:
        like = f"%{q.strip()}%"
        query = query.where(PatrolPlan.name.ilike(like) | PatrolPlan.code.ilike(like))
    return [plan_view(db, row) for row in db.scalars(query).all()]


@router.get("/options")
def plan_options(
    db: DbSession, auth: AuthContext = Depends(require_permission("robot.read"))
) -> dict:
    """表单选项：已登记车辆 + 每辆车所在站点已发布地图上的已有路线（不自动选择）。"""
    robot_query = select(Robot).order_by(Robot.vehicle_id)
    visible = scoped_robot_ids(db, auth.permissions)
    if visible is not None:
        robot_query = robot_query.where(Robot.id.in_(visible or {""}))
    robots = []
    for robot in db.scalars(robot_query).all():
        routes = db.execute(
            select(Trajectory, MapVersion)
            .join(MapVersion, MapVersion.id == Trajectory.map_version_id)
            .join(Map, Map.id == MapVersion.map_id)
            .where(
                Map.site_id == robot.site_id,
                MapVersion.status == "PUBLISHED",
                Trajectory.enabled.is_(True),
            )
            .order_by(Trajectory.code)
        ).all()
        robots.append(
            {
                "id": robot.id,
                "vehicle_id": robot.vehicle_id,
                "name": robot.name,
                "online_state": robot.online_state,
                "routes": [
                    {
                        "id": trajectory.id,
                        "code": trajectory.code,
                        "map_version_id": version.id,
                        "map_version": version.version,
                        "point_count": len(trajectory.path_json or []),
                    }
                    for trajectory, version in routes
                ],
                "right_side_mechanism": right_side_mechanism(db, robot),
            }
        )
    return {
        "robots": robots,
        "lateral_range_m": [LATERAL_MIN_M, LATERAL_MAX_M],
        "work_sides": ["RIGHT"],
        "execution_mode": PREVIEW_ONLY,
    }


@router.post("/preview")
def preview_times(
    payload: PreviewInput, auth: AuthContext = Depends(require_permission("robot.read"))
) -> dict:
    return {
        "cron_expression": payload.schedule.cron(),
        "timezone": payload.schedule.timezone,
        "occurrences": cron_preview(payload.schedule, payload.count),
        "real_execution": False,
    }


@router.get("/{plan_id}")
def plan_detail(
    plan_id: str, db: DbSession, auth: AuthContext = Depends(require_permission("robot.read"))
) -> dict:
    return plan_view(db, _get_plan(db, auth, plan_id), detail=True)


@router.post("", status_code=201)
def create_plan(
    payload: PlanInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("patrol.create")),
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=8, max_length=128),
) -> dict:
    body = payload.model_dump()
    cached = lookup(db, actor_id=auth.user.id, endpoint="/plans", key=idempotency_key, payload=body)
    if cached:
        return cached.response_json
    _reject_execution_flags(payload)
    robot, trajectory, version = _resolve_config(db, auth, payload)
    duplicate = db.scalar(
        select(PatrolPlan).where(
            PatrolPlan.robot_id == robot.id,
            func.lower(PatrolPlan.name) == payload.name.strip().lower(),
        )
    )
    if duplicate:
        raise PlatformError(
            "PLAN_NAME_EXISTS",
            "该车辆已存在同名巡检计划",
            status_code=409,
            details={"plan_id": duplicate.id},
        )
    plan = PatrolPlan(
        code=_next_code(db),
        name=payload.name.strip(),
        description=payload.description or None,
        robot_id=robot.id,
        map_version_id=version.id,
        trajectory_id=trajectory.id,
        enabled=False,
        execution_mode=PREVIEW_ONLY,
        status="DRAFT",
        work_side="RIGHT",
        target_lateral_min_m=payload.target_lateral_min_m,
        target_lateral_max_m=payload.target_lateral_max_m,
        schedule_json=payload.schedule.model_dump(),
        created_by=auth.user.id,
        updated_by=auth.user.id,
        revision=1,
    )
    db.add(plan)
    db.flush()
    _upsert_schedule(db, plan, payload.schedule, auth.user.id)
    db.flush()
    response = plan_view(db, plan)
    write_audit(
        db,
        action="PATROL_PLAN_DRAFT_CREATE",
        resource_type="PATROL_PLAN",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=plan.id,
        after={**body, "execution_mode": PREVIEW_ONLY, "status": "DRAFT"},
        **request_meta(request),
    )
    store(
        db,
        actor_id=auth.user.id,
        endpoint="/plans",
        key=idempotency_key,
        payload=body,
        response=response,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise PlatformError("PLAN_SAVE_CONFLICT", "计划保存冲突，请刷新后重试") from exc
    return response


@router.put("/{plan_id}")
def update_plan(
    plan_id: str,
    payload: PlanUpdateInput,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("patrol.create")),
) -> dict:
    plan = _get_plan(db, auth, plan_id)
    if plan.execution_mode != PREVIEW_ONLY:
        raise PlatformError(
            "PLAN_LEGACY_READONLY", "历史可执行计划不能在此页面编辑", status_code=409
        )
    _reject_execution_flags(payload)
    if payload.revision != plan.revision:
        raise PlatformError(
            "PLAN_REVISION_CONFLICT",
            "计划已被其他人修改，请刷新后再保存",
            status_code=409,
            details={"current_revision": plan.revision},
        )
    robot, trajectory, version = _resolve_config(db, auth, payload)
    duplicate = db.scalar(
        select(PatrolPlan).where(
            PatrolPlan.robot_id == robot.id,
            func.lower(PatrolPlan.name) == payload.name.strip().lower(),
            PatrolPlan.id != plan.id,
        )
    )
    if duplicate:
        raise PlatformError("PLAN_NAME_EXISTS", "该车辆已存在同名巡检计划", status_code=409)
    before = plan_view(db, plan)
    # P1-01 原子乐观锁：以“WHERE revision = 期望值”的条件更新作为提交点。
    # PostgreSQL 下并发的第二个事务会在行锁上等待，待第一个提交后重新求值 WHERE，
    # 命中 0 行 → 409；不存在“先比较、后写入”的竞态窗口。
    claimed = db.execute(
        update(PatrolPlan)
        .where(PatrolPlan.id == plan.id, PatrolPlan.revision == payload.revision)
        .values(revision=PatrolPlan.revision + 1)
        .execution_options(synchronize_session=False)
    ).rowcount
    if claimed != 1:
        db.rollback()
        raise PlatformError(
            "PLAN_REVISION_CONFLICT",
            "计划已被其他人修改，请刷新后再保存",
            status_code=409,
        )
    db.refresh(plan)
    plan.name = payload.name.strip()
    plan.description = payload.description or None
    plan.robot_id = robot.id
    plan.trajectory_id = trajectory.id
    plan.map_version_id = version.id
    plan.target_lateral_min_m = payload.target_lateral_min_m
    plan.target_lateral_max_m = payload.target_lateral_max_m
    plan.schedule_json = payload.schedule.model_dump()
    plan.updated_by = auth.user.id
    plan.enabled = False
    _upsert_schedule(db, plan, payload.schedule, auth.user.id)
    db.flush()
    after = plan_view(db, plan)
    write_audit(
        db,
        action="PATROL_PLAN_UPDATE",
        resource_type="PATROL_PLAN",
        user_id=auth.user.id,
        robot_id=robot.id,
        resource_id=plan.id,
        before=before,
        after=after,
        **request_meta(request),
    )
    db.commit()
    return after


def _set_status(
    plan_id: str, target: str, request: Request, db: Session, auth: AuthContext
) -> dict:
    plan = _get_plan(db, auth, plan_id)
    if plan.execution_mode != PREVIEW_ONLY:
        raise PlatformError(
            "PLAN_LEGACY_READONLY", "历史可执行计划不能在此页面变更状态", status_code=409
        )
    allowed = {"DRAFT": {"PAUSED"}, "PAUSED": {"DRAFT"}}
    if target not in allowed.get(plan.status, set()):
        raise PlatformError(
            "PLAN_STATUS_INVALID",
            f"计划当前状态 {plan.status} 不能变更为 {target}",
            status_code=409,
        )
    before = plan.status
    plan.status = target
    plan.enabled = False
    plan.updated_by = auth.user.id
    schedule = _schedule_for(db, plan)
    if schedule:
        schedule.enabled = False
        schedule.next_run_at = None
    write_audit(
        db,
        action="PATROL_PLAN_PAUSE" if target == "PAUSED" else "PATROL_PLAN_RESUME_DRAFT",
        resource_type="PATROL_PLAN",
        user_id=auth.user.id,
        robot_id=plan.robot_id,
        resource_id=plan.id,
        before={"status": before},
        after={"status": target},
        **request_meta(request),
    )
    db.commit()
    return plan_view(db, plan)


@router.post("/{plan_id}/pause")
def pause_plan(
    plan_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("patrol.create")),
) -> dict:
    return _set_status(plan_id, "PAUSED", request, db, auth)


@router.post("/{plan_id}/resume")
def resume_plan(
    plan_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("patrol.create")),
) -> dict:
    """恢复为草稿（仍是 PREVIEW_ONLY，不会启动执行）。"""
    return _set_status(plan_id, "DRAFT", request, db, auth)


@router.post("/{plan_id}/enable")
def enable_plan(
    plan_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("patrol.create")),
) -> dict:
    """显式拒绝：本期任何计划都不能被启用为真实执行（留审计，便于追查误操作）。"""
    plan = _get_plan(db, auth, plan_id)
    write_audit(
        db,
        action="PATROL_PLAN_ENABLE_REJECTED",
        resource_type="PATROL_PLAN",
        user_id=auth.user.id,
        robot_id=plan.robot_id,
        resource_id=plan.id,
        result="REJECTED",
        after={"reason": "PREVIEW_ONLY"},
        **request_meta(request),
    )
    db.commit()
    raise PlatformError(
        "PLAN_EXECUTION_FORBIDDEN",
        "本期巡检计划仅支持保存草稿与时间预演，禁止启用真实执行",
        status_code=409,
    )


@router.post("/{plan_id}/preview-run", status_code=201)
def record_preview(
    plan_id: str,
    request: Request,
    db: DbSession,
    auth: AuthContext = Depends(require_permission("patrol.create")),
    count: int = 5,
) -> dict:
    """计划预演：把未来 N 个触发时间写入 PREVIEW 记录（不创建 Task/Command）。"""
    plan = _get_plan(db, auth, plan_id)
    if plan.execution_mode != PREVIEW_ONLY:
        raise PlatformError("PLAN_LEGACY_READONLY", "历史可执行计划不支持预演记录", status_code=409)
    spec = ScheduleSpec(**(plan.schedule_json or {}))
    schedule = _schedule_for(db, plan) or _upsert_schedule(db, plan, spec, auth.user.id)
    db.flush()
    times = cron_preview(spec, max(1, min(count, PREVIEW_COUNT_MAX)))
    created = 0
    for value in times:
        when = datetime.fromisoformat(value).astimezone(UTC)
        exists = db.scalar(
            select(PatrolScheduleOccurrence).where(
                PatrolScheduleOccurrence.schedule_id == schedule.id,
                PatrolScheduleOccurrence.scheduled_for == when,
            )
        )
        if exists:
            continue
        db.add(
            PatrolScheduleOccurrence(
                schedule_id=schedule.id,
                scheduled_for=when,
                state="PREVIEW",
                reason_code="PREVIEW_ONLY",
            )
        )
        created += 1
    write_audit(
        db,
        action="PATROL_PLAN_PREVIEW",
        resource_type="PATROL_PLAN",
        user_id=auth.user.id,
        robot_id=plan.robot_id,
        resource_id=plan.id,
        after={"occurrences": times, "created": created, "real_execution": False},
        **request_meta(request),
    )
    db.commit()
    return {"plan_id": plan.id, "occurrences": times, "created": created, "real_execution": False}
