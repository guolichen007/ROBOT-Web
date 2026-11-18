"""G3 测试资产归属反证：fixture 只创建/清理合成命名空间、且只清理本会话创建的资源。"""

from __future__ import annotations

import pytest
from conftest import CREATED, SIM, _assert_sim_namespace


@pytest.mark.parametrize(
    "vehicle_id",
    ["R001", "R002", "firebot-vehicle-01", "firebot-vehicle-02", "firebot-sim-", "sim-01"],
)
def test_guard_refuses_real_or_malformed_ids(vehicle_id):
    with pytest.raises(RuntimeError):
        _assert_sim_namespace(vehicle_id)


def test_guard_accepts_simulator_id():
    _assert_sim_namespace(SIM)


def test_preexisting_stream_is_not_deleted_by_cleanup(g3):
    """预先存在的同名合成流（非本会话创建）在 _unregister_streams 后仍保留。"""
    from app.db.models import Robot, StreamRegistry
    from app.db.session import SessionLocal
    from sqlalchemy import delete, select
    from test_media_e2e import RGB, SIM_MEDIA, _unregister_streams

    with SessionLocal() as db:
        site_robot = db.scalar(select(Robot).where(Robot.vehicle_id == SIM))
        db.add(
            StreamRegistry(
                stream_id=RGB,
                robot_id=site_robot.id,
                camera_type="roof_rgb",
                source_ref="SYNTHETIC_TEST",
                state="OFFLINE",
                playback_url=f"/media/{RGB}/whep",
            )
        )
        db.commit()
    assert RGB not in CREATED["streams"]
    try:
        _unregister_streams()
        with SessionLocal() as db:
            assert db.scalar(select(StreamRegistry).where(StreamRegistry.stream_id == RGB))
            assert db.scalar(select(Robot).where(Robot.vehicle_id == SIM_MEDIA)) is None
    finally:
        with SessionLocal() as db:
            db.execute(delete(StreamRegistry).where(StreamRegistry.stream_id == RGB))
            db.commit()


def test_real_vehicles_untouched_by_g3_session(g3, real_001_asset):
    """真实 001/002 记录在 G3 会话中存在且未被合成 fixture 改写（enabled/协议不变）。

    G4-R1 E：001 由 real_001_asset 按需准备（不再依赖 G1 先运行）；合成归属集合永不含真实车辆。
    """
    from app.db.models import Robot
    from app.db.session import SessionLocal
    from sqlalchemy import select

    with SessionLocal() as db:
        rows = db.scalars(
            select(Robot).where(Robot.vehicle_id.in_(["R001", "R002", "firebot-vehicle-01"]))
        ).all()
        assert {r.vehicle_id for r in rows} == {"R001", "R002", "firebot-vehicle-01"}
        assert not any(r.id in CREATED["robots"] for r in rows)


def test_real_001_asset_ownership_is_exact(g3, real_001_asset):
    """G4-R1 E：001 按需准备只记账本 fixture 创建的那一行；R001/R002 永不入账。"""
    from app.db.models import Robot
    from app.db.session import SessionLocal
    from conftest import REAL_001
    from sqlalchemy import select

    with SessionLocal() as db:
        robot = db.get(Robot, real_001_asset)
        assert robot is not None and robot.vehicle_id == REAL_001
        protected = db.scalars(select(Robot.id).where(Robot.vehicle_id.in_(["R001", "R002"])))
        assert not (set(protected) & CREATED["real_assets"])
    assert CREATED["real_assets"] <= {real_001_asset}


def test_real_001_cleanup_spares_preexisting_asset(g3, real_001_asset):
    """不在 CREATED 账上的 001（例如由 G1 套件预先创建）→ 清理函数不得删除。"""
    from app.db.models import Robot
    from app.db.session import SessionLocal
    from conftest import _cleanup_real_001

    owned = real_001_asset in CREATED["real_assets"]
    CREATED["real_assets"].discard(real_001_asset)
    try:
        _cleanup_real_001()
        with SessionLocal() as db:
            assert db.get(Robot, real_001_asset) is not None
    finally:
        if owned:
            CREATED["real_assets"].add(real_001_asset)
