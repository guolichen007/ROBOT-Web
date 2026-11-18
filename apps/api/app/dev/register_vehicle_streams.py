"""Ops tool (G4-R1 C): 真实 001 媒体流登记——幂等、事务化、默认 dry-run、可精确撤销。

只做一件事：为**已存在**的真实车辆 ``firebot-vehicle-01`` 登记两条 StreamRegistry：

    firebot-vehicle-01-roof_rgb      camera_type=roof_rgb
    firebot-vehicle-01-roof_thermal  camera_type=roof_thermal

未登记时 ``/api/v1/media/authorize`` 对发布返回 403 ``unregistered_path``；登记后发布仍只认
车辆绑定身份（user == vehicle_id + HMAC 派生口令，见 media/router.py::authorize_publish）。
本工具**不**修改媒体鉴权、VideoCard、机器人行、MQTT、R001/R002 任何记录，不创建机器人。

用法（api 容器内）::

    # 1) 预演（默认 dry-run，不写库）
    docker compose -f docker-compose.server.yml exec -T api \\
        python -m app.dev.register_vehicle_streams
    # 2) 执行（单事务；输出 receipt JSON，务必保存）
    docker compose ... exec -T api python -m app.dev.register_vehicle_streams --apply \\
        > g4r1-stream-receipt.json
    # 3) 撤销预演 / 执行（只删 receipt 中、且审计日志证明由本工具创建的行）
    docker compose ... exec -T api python -m app.dev.register_vehicle_streams \\
        --rollback - < g4r1-stream-receipt.json
    docker compose ... exec -T api python -m app.dev.register_vehicle_streams \\
        --rollback - --apply < g4r1-stream-receipt.json

退出码：0 成功 / 2 用法错误 / 3 前置条件不满足（机器人不存在、身份异常） /
4 归属冲突（拒绝，零写入） / 5 撤销校验失败（零删除）。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import write_audit
from app.core.config import REAL_VEHICLE_IDS, is_synthetic_namespace
from app.db.models import AuditLog, Robot, StreamRegistry
from app.db.session import SessionLocal

TARGET_VEHICLE = "firebot-vehicle-01"
STREAMS: tuple[tuple[str, str], ...] = (
    (f"{TARGET_VEHICLE}-roof_rgb", "roof_rgb"),
    (f"{TARGET_VEHICLE}-roof_thermal", "roof_thermal"),
)
# 真实车辆流标记：任何非 SYNTHETIC_TEST 的值在 authorize_publish 中都按真实车辆流处理
# （只接受车辆绑定身份）。显式写值便于审计与撤销时识别“本工具创建”。
REAL_SOURCE_REF = "REAL_VEHICLE_SRT"
SYNTHETIC_SOURCE_REF = "SYNTHETIC_TEST"
PROTECTED_VEHICLES = frozenset({"R001", "R002"})
AUDIT_CREATE = "MEDIA_STREAM_REGISTERED"
AUDIT_DELETE = "MEDIA_STREAM_UNREGISTERED"
RECEIPT_KIND = "firebot.g4r1.stream-registration.v1"

EXIT_OK, EXIT_USAGE, EXIT_PRECONDITION, EXIT_CONFLICT, EXIT_ROLLBACK = 0, 2, 3, 4, 5


class ToolError(Exception):
    def __init__(self, code: int, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class _DryRun(Exception):  # noqa: N818 — 控制流：抛出即令 db.begin() 回滚
    """dry-run 结束：抛出以强制事务回滚（即使计划阶段有任何隐式写入也不会落库）。"""

    def __init__(self, result: dict[str, Any]) -> None:
        super().__init__("dry-run")
        self.result = result


def _row(stream: StreamRegistry) -> dict[str, Any]:
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


def _target_robot(db: Session, *, lock: bool) -> Robot:
    """校验目标机器人身份；绝不创建。lock=True 时 SELECT ... FOR UPDATE 串行化并发执行。"""
    query = select(Robot).where(Robot.vehicle_id == TARGET_VEHICLE)
    if lock:
        query = query.with_for_update()
    robot = db.scalar(query)
    if robot is None:
        raise ToolError(
            EXIT_PRECONDITION,
            f"robot {TARGET_VEHICLE!r} 不存在；本工具不创建机器人（先完成车辆注册）",
        )
    if (
        robot.vehicle_id != TARGET_VEHICLE
        or robot.vehicle_id in PROTECTED_VEHICLES
        or robot.vehicle_id not in REAL_VEHICLE_IDS
        or is_synthetic_namespace(robot.vehicle_id)
    ):
        raise ToolError(EXIT_PRECONDITION, "目标机器人身份异常（非真实 001）")
    return robot


def plan(db: Session, robot: Robot) -> list[dict[str, Any]]:
    """逐条判定 CREATE / REUSE / CONFLICT。任一 CONFLICT → 整体拒绝（零写入）。"""
    actions: list[dict[str, Any]] = []
    for stream_id, camera in STREAMS:
        existing = db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == stream_id))
        if existing is None:
            # 同车同 camera_type 已存在其他 stream_id → 会产生两张同类卡片，拒绝
            other = db.scalar(
                select(StreamRegistry).where(
                    StreamRegistry.robot_id == robot.id,
                    StreamRegistry.camera_type == camera,
                )
            )
            if other is not None:
                actions.append(
                    {
                        "stream_id": stream_id,
                        "action": "CONFLICT",
                        "reason": "same_robot_camera_has_other_stream",
                        "existing": _row(other),
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
                    "existing": _row(existing),
                }
            )
        elif existing.source_ref == SYNTHETIC_SOURCE_REF:
            actions.append(
                {
                    "stream_id": stream_id,
                    "action": "CONFLICT",
                    "reason": "real_stream_labelled_synthetic",
                    "existing": _row(existing),
                }
            )
        else:
            actions.append({"stream_id": stream_id, "action": "REUSE", "existing": _row(existing)})
    return actions


def _snapshot_protected(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(
        select(StreamRegistry, Robot.vehicle_id)
        .join(Robot, Robot.id == StreamRegistry.robot_id)
        .where(Robot.vehicle_id.in_(sorted(PROTECTED_VEHICLES)))
        .order_by(StreamRegistry.stream_id)
    ).all()
    return [{**_row(s), "vehicle_id": v} for s, v in rows]


def register(*, apply: bool) -> dict[str, Any]:
    try:
        return _register(apply=apply)
    except _DryRun as done:
        return done.result


def _register(*, apply: bool) -> dict[str, Any]:
    # 显式事务：只有 apply 且全部校验通过才 commit；dry-run / 任何异常 → rollback（零写入）
    with SessionLocal() as db, db.begin():
        robot = _target_robot(db, lock=apply)
        protected_before = _snapshot_protected(db)
        actions = plan(db, robot)
        conflicts = [a for a in actions if a["action"] == "CONFLICT"]
        result: dict[str, Any] = {
            "kind": RECEIPT_KIND,
            "mode": "APPLY" if apply else "DRY_RUN",
            "vehicle_id": TARGET_VEHICLE,
            "robot_id": robot.id,
            "actions": actions,
            "created": [],
        }
        if conflicts:
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
                after=_row(stream),
                actor_type="SYSTEM",
            )
            result["created"].append({"id": stream.id, "stream_id": stream.stream_id})
        if _snapshot_protected(db) != protected_before:
            raise ToolError(EXIT_CONFLICT, "R001/R002 流记录发生变化：中止（事务回滚）")
    return result


def _created_by_tool(db: Session, row_id: str) -> bool:
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


def rollback(receipt: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    try:
        return _rollback(receipt, apply=apply)
    except _DryRun as done:
        return done.result


def _rollback(receipt: dict[str, Any], *, apply: bool) -> dict[str, Any]:
    """只撤销 receipt 列出的、且审计日志证明由本工具 CREATE 的行；任一校验失败 → 零删除。"""
    if receipt.get("kind") != RECEIPT_KIND or receipt.get("vehicle_id") != TARGET_VEHICLE:
        raise ToolError(EXIT_ROLLBACK, "receipt 类型/目标不匹配")
    if receipt.get("mode") != "APPLY":
        raise ToolError(EXIT_ROLLBACK, "receipt 来自 dry-run，没有可撤销的记录")
    allowed_ids = {sid for sid, _ in STREAMS}
    created = receipt.get("created")
    if not isinstance(created, list) or not all(
        isinstance(e, dict) and isinstance(e.get("id"), str) and isinstance(e.get("stream_id"), str)
        for e in created
    ):
        raise ToolError(EXIT_ROLLBACK, "receipt.created 结构非法")
    if len({e["id"] for e in created}) != len(created):
        raise ToolError(EXIT_ROLLBACK, "receipt.created 含重复 id")
    with SessionLocal() as db, db.begin():
        robot = _target_robot(db, lock=apply)
        if receipt.get("robot_id") != robot.id:
            raise ToolError(EXIT_ROLLBACK, "receipt robot_id 与当前 001 不一致")
        protected_before = _snapshot_protected(db)
        actions: list[dict[str, Any]] = []
        for entry in created:
            row_id, stream_id = entry["id"], entry["stream_id"]
            row = db.get(StreamRegistry, row_id)
            if stream_id not in allowed_ids:
                raise ToolError(EXIT_ROLLBACK, f"receipt 含非本工具流 {stream_id!r}")
            if row is None:
                actions.append({"stream_id": stream_id, "id": row_id, "action": "ALREADY_ABSENT"})
                continue
            if (
                row.stream_id != stream_id
                or row.robot_id != robot.id
                or row.source_ref != REAL_SOURCE_REF
                or not _created_by_tool(db, row.id)
            ):
                raise ToolError(
                    EXIT_ROLLBACK, f"{stream_id}: 行身份与 receipt/审计不符，拒绝撤销（零删除）"
                )
            actions.append({"stream_id": stream_id, "id": row_id, "action": "DELETE"})
        result = {
            "kind": RECEIPT_KIND + ".rollback",
            "mode": "APPLY" if apply else "DRY_RUN",
            "vehicle_id": TARGET_VEHICLE,
            "actions": actions,
        }
        if not apply:
            raise _DryRun(result)
        for item in actions:
            if item["action"] != "DELETE":
                continue
            row = db.get(StreamRegistry, item["id"])
            if row is None:  # 并发删除：整体回滚，交由重跑判定 ALREADY_ABSENT
                raise ToolError(EXIT_ROLLBACK, f"{item['stream_id']}: 校验后行已消失，事务回滚")
            before = _row(row)
            db.delete(row)
            write_audit(
                db,
                action=AUDIT_DELETE,
                resource_type="STREAM",
                robot_id=robot.id,
                resource_id=item["id"],
                before=before,
                after=None,
                actor_type="SYSTEM",
            )
        db.flush()
        if _snapshot_protected(db) != protected_before:
            raise ToolError(EXIT_ROLLBACK, "R001/R002 流记录发生变化：中止（事务回滚）")
    return result


def _read_receipt(path: str) -> dict[str, Any]:
    text = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("receipt 必须是 JSON 对象")
    return data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.dev.register_vehicle_streams",
        description=f"登记 {TARGET_VEHICLE} 的 roof_rgb / roof_thermal 媒体流（默认 dry-run）",
    )
    parser.add_argument("--apply", action="store_true", help="真正写库（默认只预演）")
    parser.add_argument(
        "--rollback", metavar="RECEIPT", help="撤销：receipt 文件路径，'-' 表示 stdin"
    )
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        return EXIT_USAGE if exc.code else EXIT_OK
    try:
        if args.rollback:
            try:
                receipt = _read_receipt(args.rollback)
            except (OSError, ValueError) as exc:
                raise ToolError(EXIT_USAGE, f"receipt 读取失败: {exc}") from exc
            result = rollback(receipt, apply=args.apply)
            tag = "STREAM_ROLLBACK"
        else:
            result = register(apply=args.apply)
            tag = "STREAM_REGISTRATION"
    except ToolError as exc:
        print(f"ERROR {exc}", file=sys.stderr)
        if exc.detail is not None:
            print(json.dumps(exc.detail, ensure_ascii=False, indent=2), file=sys.stderr)
        return exc.code
    print(json.dumps(result, ensure_ascii=False, indent=2))
    summary = ",".join(f"{a['stream_id']}={a['action']}" for a in result["actions"])
    print(f"{tag}={result['mode']} {summary}", file=sys.stderr)
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
