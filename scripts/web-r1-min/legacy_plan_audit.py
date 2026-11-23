"""WEB-R1-MIN-FINAL P0-04：历史巡检计划只读核查（READ ONLY 事务，零写入）。

用法（仓库根目录，DATABASE_URL 指向待核查库；生产库请用只读账号）：
    python scripts/web-r1-min/legacy_plan_audit.py [--json]

输出每个计划的执行模式、启用状态、调度、所属车辆在线 / 控制就绪情况，以及
“理论上能否形成真实下行”的结论。脚本只执行 SELECT，并以
SET TRANSACTION READ ONLY 运行；任何写入都会被数据库拒绝。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "apps/api"), str(ROOT)]

from app.core.config import get_settings  # noqa: E402
from app.db.models import PatrolPlan, PatrolSchedule, Robot  # noqa: E402
from app.db.session import SessionLocal  # noqa: E402
from app.modules.commands.readiness import robot_readiness  # noqa: E402
from sqlalchemy import select, text  # noqa: E402


def audit() -> dict:
    settings = get_settings()
    rows = []
    with SessionLocal() as db:
        db.execute(text("SET TRANSACTION READ ONLY"))
        for plan in db.scalars(select(PatrolPlan).order_by(PatrolPlan.code)).all():
            robot = db.get(Robot, plan.robot_id)
            schedules = db.scalars(
                select(PatrolSchedule).where(PatrolSchedule.patrol_plan_id == plan.id)
            ).all()
            readiness = robot_readiness(db, robot) if robot else {}
            patrol_ready = bool((readiness.get("autonomous_task_ready") or {}).get("patrol"))
            preview = plan.execution_mode == "PREVIEW_ONLY" or plan.status in {"DRAFT", "PAUSED"}
            rows.append(
                {
                    "code": plan.code,
                    "name": plan.name,
                    "execution_mode": plan.execution_mode,
                    "status": plan.status,
                    "enabled": plan.enabled,
                    "vehicle_id": robot.vehicle_id if robot else None,
                    "robot_online": robot.online_state if robot else None,
                    "robot_patrol_ready": patrol_ready,
                    "schedules": [
                        {
                            "cron": s.cron_expression,
                            "enabled": s.enabled,
                            "next_run_at": s.next_run_at.isoformat() if s.next_run_at else None,
                        }
                        for s in schedules
                    ],
                    # 结论：能否被调度器自动触发 / 能否被人工 HMI 启动
                    "auto_dispatch_possible": (not preview)
                    and plan.enabled
                    and any(s.enabled and s.next_run_at for s in schedules),
                    "manual_start_possible": (not preview) and plan.enabled,
                    "blocked_by_server": preview,
                }
            )
        db.rollback()
    return {
        "read_only": True,
        "downlink_switches": {
            "vehicle_agent_v1_downlink_enabled": settings.vehicle_agent_v1_downlink_enabled,
            "vehicle_agent_v1_downlink_allowlist": settings.vehicle_agent_v1_downlink_allowlist,
        },
        "plans": rows,
        "summary": {
            "total": len(rows),
            "preview_only": sum(r["execution_mode"] == "PREVIEW_ONLY" for r in rows),
            "legacy_executable": sum(r["execution_mode"] == "LEGACY_EXECUTABLE" for r in rows),
            "legacy_enabled": sum(
                r["execution_mode"] == "LEGACY_EXECUTABLE" and r["enabled"] for r in rows
            ),
            "auto_dispatch_possible": sum(r["auto_dispatch_possible"] for r in rows),
        },
    }


if __name__ == "__main__":
    result = audit()
    if "--json" in sys.argv:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(result["summary"], ensure_ascii=False))
        for row in result["plans"]:
            print(
                f"{row['code']:<32} {row['execution_mode']:<18} status={row['status']:<7} "
                f"enabled={row['enabled']!s:<5} vehicle={row['vehicle_id']} "
                f"auto={row['auto_dispatch_possible']} manual={row['manual_start_possible']}"
            )
