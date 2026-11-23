"""E2E 辅助：隔离火警注入（进程内调用 ingress.handle_alarm，不经任何 MQTT Broker）
与零下行计数（Command / OutboxEvent / Task 行数 + outbox 状态）。"""

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

R = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(R / "apps/api"), str(R)]
from app.db.models import Command, FireEvent, OutboxEvent, Robot, Task  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from sqlalchemy import func, select  # noqa: E402


def counts() -> dict:
    with SessionLocal() as db:
        return {
            "commands": db.scalar(select(func.count(Command.id))),
            "outbox": db.scalar(select(func.count(OutboxEvent.id))),
            "outbox_published": db.scalar(
                select(func.count(OutboxEvent.id)).where(OutboxEvent.published_at.is_not(None))
            ),
            "tasks": db.scalar(select(func.count(Task.id))),
        }


def inject(slot_code: str | None, event_id: str | None = None) -> dict:
    path = R / "services/mqtt-ingress/main.py"
    spec = importlib.util.spec_from_file_location("e2e_ingress", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    eid = event_id or f"e2e-sim-{uuid4().hex[:8]}"
    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        mod.handle_alarm(
            db,
            robot,
            {
                "message_id": str(uuid4()),
                "event_id": eid,
                "fire_type": "smoke",
                "severity": "HIGH",
                "confidence": 0.91,
                "parking_slot_code": slot_code,
                "position": {"x": 3.2, "y": 4.1, "theta": 0.0},
                "media": {"label": "SIMULATED"},
            },
            datetime.now(UTC),
        )
    with SessionLocal() as db:
        rows = db.scalars(select(FireEvent).where(FireEvent.source_event_id == eid)).all()
        return {
            "event_id": eid,
            "alarms": [
                {
                    "id": r.id,
                    "event_code": r.event_code,
                    "location_state": r.location_state,
                    "occurrence_count": r.occurrence_count,
                    "state": r.state,
                }
                for r in rows
            ],
        }


def inject_fault_command() -> dict:
    """反证专用：在隔离 E2E 库写入一条 Command（无 dispatcher 运行，不会发出）。
    验收脚本的 VERDICT 必须因此失败。"""
    from datetime import timedelta

    from app.db.models import User

    with SessionLocal.begin() as db:
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == "R001"))
        admin = db.scalar(select(User).where(User.username == "admin"))
        now = datetime.now(UTC)
        row = Command(
            command_id=f"e2e-fault-{uuid4().hex[:8]}",
            correlation_id=str(uuid4()),
            robot_id=robot.id,
            cmd="e2e_fault_probe",
            payload_json={"fault_injection": True},
            lifecycle_status="CREATED",
            issued_by=admin.id,
            issued_at=now,
            expires_at=now + timedelta(seconds=1),
        )
        db.add(row)
    return {"injected_command": row.command_id}


def user_permissions(username: str) -> dict:
    from app.core.dependencies import load_permissions
    from app.db.models import Role, User, user_roles

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.username == username))
        roles = sorted(
            db.scalars(
                select(Role.code)
                .join(user_roles, user_roles.c.role_id == Role.id)
                .where(user_roles.c.user_id == user.id)
            ).all()
        )
        return {"roles": roles, "permissions": sorted(load_permissions(db, user.id))}


def alarm_state(alarm_id: str) -> dict:
    with SessionLocal() as db:
        row = db.get(FireEvent, alarm_id)
        return {
            "id": row.id,
            "event_code": row.event_code,
            "state": row.state,
            "ack_by": row.ack_by,
        }


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "counts":
        print(json.dumps(counts()))
    elif cmd == "inject":
        slot = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else None
        eid = sys.argv[3] if len(sys.argv) > 3 else None
        print(json.dumps(inject(slot, eid)))
    elif cmd == "inject-command":
        print(json.dumps(inject_fault_command()))
    elif cmd == "user-perms":
        print(json.dumps(user_permissions(sys.argv[2])))
    elif cmd == "alarm-state":
        print(json.dumps(alarm_state(sys.argv[2])))
