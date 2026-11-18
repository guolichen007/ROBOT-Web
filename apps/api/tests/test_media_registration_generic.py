"""G4-Fleet-R2：通用媒体登记（generic 入口 + app.modules.media.registration 核心）。

覆盖：显式目标 + 已登记真实资产 + 批准 Transport Profile；不创建资产；001 走专项入口；
唯一性/幂等/冲突零写入；并发两个 --apply 只产生一组行；实际 firebot-vehicle-02 受保护；
撤销前发布会话门（活动 → 拒绝；无法确认 → 需显式确认）；OFFLINE 不影响发布鉴权。
"""

from __future__ import annotations

import io
import json
import threading

import pytest
from app.db.models import AuditLog, Base, Robot, Site, StreamRegistry
from app.db.session import SessionLocal
from app.dev import register_vehicle_streams as tool
from app.main import app
from app.modules.media import registration as core
from app.modules.media.router import vehicle_srt_publish_token
from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select

NEW = "firebot-vehicle-03"
V002 = "firebot-vehicle-02"
NEW_STREAMS = (f"{NEW}-roof_rgb", f"{NEW}-roof_thermal")


def _run(capsys, *args, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    capsys.readouterr()
    code = tool.main(list(args))
    out = capsys.readouterr()
    return code, (json.loads(out.out) if code == 0 and out.out.strip() else None), out.err


def _ensure(vehicle_id):
    with SessionLocal() as db:
        r = db.scalar(select(Robot).where(Robot.vehicle_id == vehicle_id))
        if r:
            return r.id, False
        site = db.scalar(select(Site).order_by(Site.id).limit(1))
        r = Robot(vehicle_id=vehicle_id, name=vehicle_id, site_id=site.id, enabled=False)
        db.add(r)
        db.commit()
        return r.id, True


def _drop_robot(rid):
    with SessionLocal() as db:
        for t in reversed(Base.metadata.sorted_tables):
            if "robot_id" in t.c and t.name != "robots":
                db.execute(t.delete().where(t.c.robot_id == rid))
        db.execute(delete(Robot).where(Robot.id == rid))
        db.commit()


@pytest.fixture()
def assets():
    """新车 003 + 实际 002（含一条已存在的 002 流，验证受保护快照）。只清理本 fixture 创建的。"""
    new_id, new_created = _ensure(NEW)
    v2_id, v2_created = _ensure(V002)
    with SessionLocal() as db:
        assert not db.scalar(
            select(StreamRegistry).where(StreamRegistry.stream_id.in_(NEW_STREAMS))
        )
        planted = None
        if not db.scalar(
            select(StreamRegistry).where(StreamRegistry.stream_id == f"{V002}-roof_rgb")
        ):
            s = StreamRegistry(
                stream_id=f"{V002}-roof_rgb",
                robot_id=v2_id,
                camera_type="roof_rgb",
                source_ref=core.REAL_SOURCE_REF,
                state="OFFLINE",
            )
            db.add(s)
            db.commit()
            planted = s.id
    yield {"new": new_id, "v2": v2_id}
    with SessionLocal() as db:
        db.execute(delete(StreamRegistry).where(StreamRegistry.stream_id.in_(NEW_STREAMS)))
        db.execute(
            delete(AuditLog).where(
                AuditLog.action.in_([core.AUDIT_CREATE, core.AUDIT_DELETE]),
                AuditLog.robot_id == new_id,
            )
        )
        if planted:
            db.execute(delete(StreamRegistry).where(StreamRegistry.id == planted))
        db.commit()
    if new_created:
        _drop_robot(new_id)
    if v2_created:
        _drop_robot(v2_id)


def _gen(*extra):
    return ("generic", "--vehicle-id", NEW, "--transport-profile", "u-dao-standard-v1", *extra)


def _count_new():
    with SessionLocal() as db:
        return db.scalar(
            select(func.count())
            .select_from(StreamRegistry)
            .where(StreamRegistry.stream_id.in_(NEW_STREAMS))
        )


def test_generic_requires_explicit_profile_and_existing_asset(capsys, assets):
    code, _, err = _run(capsys, "generic", "--vehicle-id", NEW)
    assert code == core.EXIT_PRECONDITION and "transport-profile" in err
    code, _, err = _run(
        capsys, "generic", "--vehicle-id", NEW, "--transport-profile", "r001-independent-v1"
    )
    assert code == core.EXIT_PRECONDITION and "不允许分配" in err
    for profile in ("r001-independent-v1", "u-dao-standard-v1"):
        code, _, err = _run(
            capsys, "generic", "--vehicle-id", V002, "--transport-profile", profile, "--apply"
        )
        # 实际 002 为受保护车辆：通用入口一律拒绝（即使给出其映射 Profile）
        assert code == core.EXIT_PRECONDITION and "受保护车辆" in err, err
    with SessionLocal() as db:
        assert [
            r.stream_id
            for r in db.scalars(
                select(StreamRegistry).where(StreamRegistry.robot_id == assets["v2"])
            )
        ] == [f"{V002}-roof_rgb"]
    code, _, err = _run(
        capsys,
        "generic",
        "--vehicle-id",
        "firebot-vehicle-55",
        "--transport-profile",
        "u-dao-standard-v1",
        "--apply",
    )
    assert code == core.EXIT_PRECONDITION and "不创建机器人" in err
    with SessionLocal() as db:
        assert db.scalar(select(Robot).where(Robot.vehicle_id == "firebot-vehicle-55")) is None
    for bad in ("firebot-sim-ir-01", "R002", "R001"):
        code, _, _ = _run(
            capsys, "generic", "--vehicle-id", bad, "--transport-profile", "u-dao-standard-v1"
        )
        assert code == core.EXIT_PRECONDITION, bad
    code, _, err = _run(capsys, *_gen("--cameras", "bottom_ir"))
    assert code == core.EXIT_USAGE and "未批准" in err
    code, _, err = _run(capsys, "generic", "--vehicle-id", "firebot-vehicle-01")
    assert code == core.EXIT_USAGE and "专项入口" in err
    assert _count_new() == 0


def test_generic_apply_idempotent_protects_002_and_auth(capsys, assets):
    code, plan, err = _run(capsys, *_gen())
    assert code == 0 and plan["mode"] == "DRY_RUN" and _count_new() == 0, err
    code, data, err = _run(capsys, *_gen("--apply"))
    assert code == 0 and len(data["created"]) == 2, err
    assert data["transport_profile"] == "u-dao-standard-v1"
    code, again, _ = _run(capsys, *_gen("--apply"))
    assert [a["action"] for a in again["actions"]] == ["REUSE", "REUSE"]
    with SessionLocal() as db:
        v2_rows = db.scalars(
            select(StreamRegistry).where(StreamRegistry.robot_id == assets["v2"])
        ).all()
        assert [r.stream_id for r in v2_rows] == [f"{V002}-roof_rgb"]
        rows = db.scalars(
            select(StreamRegistry).where(StreamRegistry.stream_id.in_(NEW_STREAMS))
        ).all()
        assert {r.state for r in rows} == {"OFFLINE"}
    token = vehicle_srt_publish_token(NEW)
    with TestClient(app) as c:
        for sid in NEW_STREAMS:
            body = {
                "action": "publish",
                "path": sid,
                "protocol": "srt",
                "user": NEW,
                "password": token,
            }
            # OFFLINE 仅为显示状态：登记 + 车辆身份即可发布
            assert c.post("/api/v1/media/authorize", json=body).status_code == 200
            body["user"] = V002
            body["password"] = vehicle_srt_publish_token(V002)
            assert c.post("/api/v1/media/authorize", json=body).status_code == 401


def test_generic_002_tamper_detected(capsys, assets, monkeypatch):
    """受保护快照包含实际 firebot-vehicle-02：事务中被改动 → 回滚。"""
    real_plan = core.plan

    def plan_and_tamper(db, robot, target):
        row = db.scalar(
            select(StreamRegistry).where(StreamRegistry.stream_id == f"{V002}-roof_rgb")
        )
        row.state = "LIVE"
        db.flush()
        return real_plan(db, robot, target)

    monkeypatch.setattr(core, "plan", plan_and_tamper)
    code, _, err = _run(capsys, *_gen("--apply"))
    assert code == core.EXIT_CONFLICT and "受保护" in err
    assert _count_new() == 0
    with SessionLocal() as db:
        row = db.scalar(
            select(StreamRegistry).where(StreamRegistry.stream_id == f"{V002}-roof_rgb")
        )
        assert row.state == "OFFLINE"


def test_generic_concurrent_apply_single_set(assets):
    target = core.Target(NEW, ("roof_rgb", "roof_thermal"), "u-dao-standard-v1")
    results, errors = [], []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        try:
            results.append(
                core.register(target, apply=True, receipt_kind=tool.GENERIC_RECEIPT_KIND)
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    created = sum(len(r["created"]) for r in results)
    assert created == 2 and _count_new() == 2
    actions = sorted(a["action"] for r in results for a in r["actions"])
    assert actions == ["CREATE", "CREATE", "REUSE", "REUSE"]


def test_generic_rollback_publisher_session_gate(capsys, assets, monkeypatch):
    code, receipt, err = _run(capsys, *_gen("--apply"))
    assert code == 0, err
    text = json.dumps(receipt)
    monkeypatch.setattr(core, "mediamtx_publisher_probe", lambda ids: dict.fromkeys(ids, True))
    code, _, err = _run(
        capsys, *_gen("--rollback", "-", "--apply"), stdin=text, monkeypatch=monkeypatch
    )
    assert code == core.EXIT_ROLLBACK and "活动发布会话" in err and _count_new() == 2
    monkeypatch.setattr(core, "mediamtx_publisher_probe", lambda ids: dict.fromkeys(ids, None))
    code, _, err = _run(
        capsys, *_gen("--rollback", "-", "--apply"), stdin=text, monkeypatch=monkeypatch
    )
    assert code == core.EXIT_ROLLBACK and "无法确认" in err and _count_new() == 2
    code, done, err = _run(
        capsys,
        *_gen("--rollback", "-", "--apply", "--confirm-publishers-stopped"),
        stdin=text,
        monkeypatch=monkeypatch,
    )
    assert code == 0 and _count_new() == 0, err
    with SessionLocal() as db:
        audit = db.scalars(
            select(AuditLog).where(
                AuditLog.action == core.AUDIT_DELETE, AuditLog.robot_id == assets["new"]
            )
        ).all()
        assert len(audit) == 2 and all(a.after_json["operator_confirmed"] for a in audit)
    # 001 专项 receipt 不能用于通用目标
    bad = dict(receipt, kind=tool.RECEIPT_KIND)
    code, _, _ = _run(
        capsys, *_gen("--rollback", "-", "--apply"), stdin=json.dumps(bad), monkeypatch=monkeypatch
    )
    assert code == core.EXIT_ROLLBACK


def test_mediamtx_probe_real_http(monkeypatch):
    """probe 对 MediaMTX v3 语义：ready=true → True；404 → False；不可达 → None。"""
    import http.server
    import socketserver

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.endswith("/live-path"):
                body = json.dumps({"ready": True, "source": {"type": "srtConn"}}).encode()
                self.send_response(200)
            elif self.path.endswith("/idle-path"):
                body = json.dumps({"ready": False, "source": None}).encode()
                self.send_response(200)
            else:
                body = b'{"error":"path not found"}'
                self.send_response(404)
            self.end_headers()
            self.wfile.write(body)

    srv = socketserver.TCPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("FIREBOT_MEDIAMTX_API", f"http://127.0.0.1:{srv.server_address[1]}")
        got = core.mediamtx_publisher_probe(["live-path", "idle-path", "missing-path"])
        assert got == {"live-path": True, "idle-path": False, "missing-path": False}
    finally:
        srv.shutdown()
    monkeypatch.setenv("FIREBOT_MEDIAMTX_API", "http://127.0.0.1:1")
    assert core.mediamtx_publisher_probe(["x"]) == {"x": None}
