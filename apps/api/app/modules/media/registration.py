"""通用媒体流登记核心（G4-Fleet-R2；由 G4-R1 register_vehicle_streams 事务核心泛化）。

只为**已登记的真实资产**登记 ``<vehicle_id>-<camera>`` 媒体流；绝不创建机器人或任何资产。
目标必须显式给出，且必须：
  - Robot 已存在、vehicle_id 合法、非合成命名空间；
  - 在批准 Transport Profile 注册表中可解析（显式映射，或显式 --transport-profile 且可分配）；
  - camera 属于批准集合（roof_rgb / roof_thermal）。
保留：唯一性、幂等（REUSE）、冲突整体拒绝（零写入）、单事务、FOR UPDATE 并发串行化、审计、
receipt + 审计双证撤销。保护集合：除目标车辆外，R001/R002 演示资产与**实际 firebot-vehicle-02**
（以及注册表 protected_vehicles 中所有非目标车辆）的流记录前后快照一致，否则回滚。

OFFLINE 只是显示状态（VideoCard），不是鉴权关闭依据：发布鉴权只看登记 + 车辆绑定身份。
撤销前必须确认本次创建的流没有活动发布会话（MediaMTX API 查询；不可确认 → 拒绝，除非
运维显式 --confirm-publishers-stopped）。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.config import is_synthetic_namespace
from app.db.models import AuditLog, Robot, StreamRegistry
from app.db.session import SessionLocal

REAL_SOURCE_REF = "REAL_VEHICLE_SRT"
SYNTHETIC_SOURCE_REF = "SYNTHETIC_TEST"
AUDIT_CREATE = "MEDIA_STREAM_REGISTERED"
AUDIT_DELETE = "MEDIA_STREAM_UNREGISTERED"
APPROVED_CAMERAS = ("roof_rgb", "roof_thermal")
LEGACY_PROTECTED = frozenset({"R001", "R002"})
VEHICLE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

EXIT_OK, EXIT_USAGE, EXIT_PRECONDITION, EXIT_CONFLICT, EXIT_ROLLBACK = 0, 2, 3, 4, 5


class ToolError(Exception):
    def __init__(self, code: int, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class _DryRun(Exception):  # noqa: N818 — 控制流：抛出即令 db.begin() 回滚
    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__("dry-run")
        self.result = result


def row_dict(stream: StreamRegistry) -> dict[str, Any]:
    return {
        "id": stream.id,
        "stream_id": stream.stream_id,
        "robot_id": stream.robot_id,
        "camera_type": stream.camera_type,
        "provider": stream.provider,
        "source_ref": stream.source_ref,
        "playback_url": stream.playback_url,
        "codec": stream.codec,
        "state": stream.state,
    }


# ---------------------------------------------------------------- transport profile gate
def _registry_path() -> str:
    env = os.environ.get("FIREBOT_TRANSPORT_PROFILES")
    if env:
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    for up in range(2, 7):
        cand = os.path.join(here, *([".."] * up), "deploy", "u-dao", "transport-profiles.json")
        if os.path.isfile(cand):
            return os.path.normpath(cand)
    return "/workspace/deploy/u-dao/transport-profiles.json"


def load_registry() -> dict[str, Any]:
    path = _registry_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ToolError(
            EXIT_PRECONDITION, f"Transport Profile 注册表不可读（{path}）: {exc}"
        ) from None
    if data.get("schema") != "firebot.transport-profiles.v1":
        raise ToolError(EXIT_PRECONDITION, "Transport Profile 注册表 schema 不匹配")
    return data


def resolve_profile(vehicle_id: str, profile: str | None, registry: dict[str, Any]) -> str:
    mapped = registry.get("vehicles", {}).get(vehicle_id)
    if mapped and profile and profile != mapped:
        raise ToolError(EXIT_PRECONDITION, f"{vehicle_id} 已批准映射到 {mapped}，拒绝 {profile}")
    pid = mapped or profile
    if not pid:
        raise ToolError(
            EXIT_PRECONDITION, f"{vehicle_id} 未在批准注册表映射；必须显式 --transport-profile"
        )
    prof = registry.get("profiles", {}).get(pid)
    if prof is None:
        raise ToolError(EXIT_PRECONDITION, f"未知 Transport Profile {pid}")
    if not mapped and not prof.get("assignable_to_new_vehicles"):
        raise ToolError(EXIT_PRECONDITION, f"Transport Profile {pid} 不允许分配给新车")
    return pid


# ---------------------------------------------------------------- core
class Target:
    def __init__(self, vehicle_id: str, cameras: tuple[str, ...], profile: str | None) -> None:
        if not VEHICLE_RE.fullmatch(vehicle_id):
            raise ToolError(EXIT_USAGE, "vehicle_id 非法")
        if is_synthetic_namespace(vehicle_id) or vehicle_id in LEGACY_PROTECTED:
            raise ToolError(EXIT_PRECONDITION, f"{vehicle_id} 不是可登记的真实车辆资产")
        if not cameras or len(set(cameras)) != len(cameras):
            raise ToolError(EXIT_USAGE, "camera 列表为空或重复")
        bad = [c for c in cameras if c not in APPROVED_CAMERAS]
        if bad:
            raise ToolError(EXIT_USAGE, f"未批准的 camera: {bad}（允许 {APPROVED_CAMERAS}）")
        self.vehicle_id = vehicle_id
        self.cameras = cameras
        registry = load_registry()
        self.transport_profile = resolve_profile(vehicle_id, profile, registry)
        self.protected = (LEGACY_PROTECTED | frozenset(registry.get("protected_vehicles", []))) - {
            vehicle_id
        }

    @property
    def streams(self) -> tuple[tuple[str, str], ...]:
        return tuple((f"{self.vehicle_id}-{c}", c) for c in self.cameras)


def target_robot(db: Session, target: Target, *, lock: bool) -> Robot:
    """校验目标资产；绝不创建。lock=True 时 SELECT ... FOR UPDATE 串行化并发执行。"""
    query = select(Robot).where(Robot.vehicle_id == target.vehicle_id)
    if lock:
        query = query.with_for_update()
    robot = db.scalar(query)
    if robot is None:
        raise ToolError(
            EXIT_PRECONDITION,
            f"robot {target.vehicle_id!r} 不存在；本工具不创建机器人（先 fleet preregister）",
        )
    return robot


def plan(db: Session, robot: Robot, target: Target) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for stream_id, camera in target.streams:
        existing = db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == stream_id))
        if existing is None:
            other = db.scalar(
                select(StreamRegistry).where(
                    StreamRegistry.robot_id == robot.id, StreamRegistry.camera_type == camera
                )
            )
            if other is not None:
                actions.append(
                    {
                        "stream_id": stream_id,
                        "action": "CONFLICT",
                        "reason": "same_robot_camera_has_other_stream",
                        "existing": row_dict(other),
                    }
                )
            else:
                actions.append({"stream_id": stream_id, "camera_type": camera, "action": "CREATE"})
            continue
        if existing.robot_id != robot.id:
            owner = db.get(Robot, existing.robot_id)
            actions.append(
                {
                    "stream_id": stream_id,
                    "action": "CONFLICT",
                    "reason": "stream_id_owned_by_other_robot",
                    "owner_vehicle_id": owner.vehicle_id if owner else None,
                }
            )
        elif existing.camera_type != camera:
            actions.append(
                {
                    "stream_id": stream_id,
                    "action": "CONFLICT",
                    "reason": "camera_type_mismatch",
                    "existing": row_dict(existing),
                }
            )
        elif existing.source_ref == SYNTHETIC_SOURCE_REF:
            actions.append(
                {
                    "stream_id": stream_id,
                    "action": "CONFLICT",
                    "reason": "real_stream_labelled_synthetic",
                    "existing": row_dict(existing),
                }
            )
        else:
            actions.append(
                {"stream_id": stream_id, "action": "REUSE", "existing": row_dict(existing)}
            )
    return actions


def snapshot_protected(db: Session, protected: frozenset[str]) -> list[dict[str, Any]]:
    rows = db.execute(
        select(StreamRegistry, Robot.vehicle_id)
        .join(Robot, Robot.id == StreamRegistry.robot_id)
        .where(Robot.vehicle_id.in_(sorted(protected)))
        .order_by(StreamRegistry.stream_id)
    ).all()
    return [{**row_dict(s), "vehicle_id": v} for s, v in rows]


def register(target: Target, *, apply: bool, receipt_kind: str) -> dict[str, Any]:
    try:
        return _register(target, apply=apply, receipt_kind=receipt_kind)
    except _DryRun as done:
        return done.result


def _register(target: Target, *, apply: bool, receipt_kind: str) -> dict[str, Any]:
    with SessionLocal() as db, db.begin():
        robot = target_robot(db, target, lock=apply)
        protected_before = snapshot_protected(db, target.protected)
        actions = plan(db, robot, target)
        result: dict[str, Any] = {
            "kind": receipt_kind,
            "mode": "APPLY" if apply else "DRY_RUN",
            "vehicle_id": target.vehicle_id,
            "transport_profile": target.transport_profile,
            "robot_id": robot.id,
            "actions": actions,
            "created": [],
        }
        if any(a["action"] == "CONFLICT" for a in actions):
            raise ToolError(EXIT_CONFLICT, "归属冲突：拒绝登记（零写入）", result)
        if not apply:
            raise _DryRun(result)
        for item in actions:
            if item["action"] != "CREATE":
                continue
            stream = StreamRegistry(
                stream_id=item["stream_id"],
                robot_id=robot.id,
                camera_type=item["camera_type"],
                provider="MEDIAMTX",
                source_ref=REAL_SOURCE_REF,
                playback_url=f"/media/{item['stream_id']}/whep",
                codec="H264",
                state="OFFLINE",
            )
            db.add(stream)
            db.flush()
            write_audit(
                db,
                action=AUDIT_CREATE,
                resource_type="STREAM",
                robot_id=robot.id,
                resource_id=stream.id,
                before=None,
                after={**row_dict(stream), "transport_profile": target.transport_profile},
                actor_type="SYSTEM",
            )
            result["created"].append({"id": stream.id, "stream_id": stream.stream_id})
        if snapshot_protected(db, target.protected) != protected_before:
            raise ToolError(EXIT_CONFLICT, "受保护车辆流记录发生变化：中止（事务回滚）")
    return result


def created_by_tool(db: Session, row_id: str) -> bool:
    return (
        db.scalar(
            select(AuditLog.id).where(
                AuditLog.action == AUDIT_CREATE,
                AuditLog.resource_type == "STREAM",
                AuditLog.resource_id == row_id,
            )
        )
        is not None
    )


# ---------------------------------------------------------------- publisher-session gate
PublisherProbe = Callable[[list[str]], dict[str, bool | None]]


def mediamtx_publisher_probe(stream_ids: list[str]) -> dict[str, bool | None]:
    """经 MediaMTX v3 API 查询每条流是否有活动发布者（ready 或 source 非空）。

    返回 {stream_id: True(有发布)/False(无)/None(无法确认)}。
    """
    base = os.environ.get("FIREBOT_MEDIAMTX_API", "http://mediamtx:9997").rstrip("/")
    out: dict[str, bool | None] = {}
    for sid in stream_ids:
        try:
            with urllib.request.urlopen(f"{base}/v3/paths/get/{sid}", timeout=3) as resp:
                data = json.load(resp)
            out[sid] = bool(data.get("ready") or data.get("source"))
        except urllib.error.HTTPError as exc:
            out[sid] = False if exc.code == 404 else None
        except (OSError, ValueError):
            out[sid] = None
    return out


def rollback(
    target: Target,
    receipt: dict[str, Any],
    *,
    apply: bool,
    receipt_kind: str,
    probe: PublisherProbe | None = None,
    confirm_publishers_stopped: bool = False,
) -> dict[str, Any]:
    try:
        return _rollback(
            target,
            receipt,
            apply=apply,
            receipt_kind=receipt_kind,
            probe=probe,
            confirm=confirm_publishers_stopped,
        )
    except _DryRun as done:
        return done.result


def _rollback(
    target: Target,
    receipt: dict[str, Any],
    *,
    apply: bool,
    receipt_kind: str,
    probe: PublisherProbe | None,
    confirm: bool,
) -> dict[str, Any]:
    if receipt.get("kind") != receipt_kind or receipt.get("vehicle_id") != target.vehicle_id:
        raise ToolError(EXIT_ROLLBACK, "receipt 类型/目标不匹配")
    if receipt.get("mode") != "APPLY":
        raise ToolError(EXIT_ROLLBACK, "receipt 来自 dry-run，没有可撤销的记录")
    allowed_ids = {sid for sid, _ in target.streams}
    created = receipt.get("created")
    if not isinstance(created, list) or not all(
        isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("stream_id"), str)
        for e in created
    ):
        raise ToolError(EXIT_ROLLBACK, "receipt.created 结构非法")
    if len({e["id"] for e in created}) != len(created):
        raise ToolError(EXIT_ROLLBACK, "receipt.created 含重复 id")
    with SessionLocal() as db, db.begin():
        robot = target_robot(db, target, lock=apply)
        if receipt.get("robot_id") != robot.id:
            raise ToolError(EXIT_ROLLBACK, "receipt robot_id 与当前资产不一致")
        protected_before = snapshot_protected(db, target.protected)
        actions: list[dict[str, Any]] = []
        for entry in created:
            row_id, stream_id = entry["id"], entry["stream_id"]
            if stream_id not in allowed_ids:
                raise ToolError(EXIT_ROLLBACK, f"receipt 含非本次目标流 {stream_id!r}")
            row = db.get(StreamRegistry, row_id)
            if row is None:
                actions.append({"stream_id": stream_id, "id": row_id, "action": "ALREADY_ABSENT"})
                continue
            if (
                row.stream_id != stream_id
                or row.robot_id != robot.id
                or row.source_ref != REAL_SOURCE_REF
                or not created_by_tool(db, row.id)
            ):
                raise ToolError(
                    EXIT_ROLLBACK, f"{stream_id}: 行身份与 receipt/审计不符，拒绝撤销（零删除）"
                )
            actions.append({"stream_id": stream_id, "id": row_id, "action": "DELETE"})
        to_delete = [a["stream_id"] for a in actions if a["action"] == "DELETE"]
        sessions = (probe or mediamtx_publisher_probe)(to_delete) if to_delete else {}
        active = [s for s, v in sessions.items() if v is True]
        unknown = [s for s, v in sessions.items() if v is None]
        result = {
            "kind": receipt_kind + ".rollback",
            "mode": "APPLY" if apply else "DRY_RUN",
            "vehicle_id": target.vehicle_id,
            "actions": actions,
            "publisher_sessions": sessions,
        }
        if active:
            # 撤销登记不会断开正在进行的发布；先停车端 firebot-media@<camera> 再撤销
            raise ToolError(
                EXIT_ROLLBACK,
                f"仍有活动发布会话 {active}：先在车端 media stop 再撤销（零删除）",
                result,
            )
        if unknown and not confirm:
            raise ToolError(
                EXIT_ROLLBACK,
                f"无法确认发布会话状态 {unknown}（MediaMTX API 不可达）；"
                "确认车端已停止后加 --confirm-publishers-stopped（零删除）",
                result,
            )
        if not apply:
            raise _DryRun(result)
        for item in actions:
            if item["action"] != "DELETE":
                continue
            row = db.get(StreamRegistry, item["id"])
            if row is None:
                raise ToolError(EXIT_ROLLBACK, f"{item['stream_id']}: 校验后行已消失，事务回滚")
            before = row_dict(row)
            db.delete(row)
            write_audit(
                db,
                action=AUDIT_DELETE,
                resource_type="STREAM",
                robot_id=robot.id,
                resource_id=item["id"],
                before=before,
                after={"publisher_sessions_checked": sessions, "operator_confirmed": confirm},
                actor_type="SYSTEM",
            )
        db.flush()
        if snapshot_protected(db, target.protected) != protected_before:
            raise ToolError(EXIT_ROLLBACK, "受保护车辆流记录发生变化：中止（事务回滚）")
    return result
