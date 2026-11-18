"""G4-R1 C：001 媒体流登记工具（app.dev.register_vehicle_streams）行为测试。

覆盖：默认 dry-run 零写入；--apply 单事务登记两路且幂等；登记前 authorize 403
unregistered_path、登记后只接受 001 车辆绑定身份（全局 token / R002 凭证仍拒绝）；
机器人不存在不创建；归属冲突 / 合成误标 / 同车同类异 ID 拒绝且零写入；R001/R002 不变；
撤销只删除本工具创建（receipt + 审计双证）的行，篡改 receipt 零删除。
"""

from __future__ import annotations

import io
import json

import pytest
from app.db.models import AuditLog, Base, Robot, Site, StreamRegistry
from app.db.session import SessionLocal
from app.dev import register_vehicle_streams as tool
from app.main import app
from app.modules.media.router import vehicle_srt_publish_token
from fastapi.testclient import TestClient
from sqlalchemy import delete, select

VEHICLE = tool.TARGET_VEHICLE
RGB, THERMAL = (sid for sid, _ in tool.STREAMS)


def _streams(db, *ids):
    return {
        s.stream_id: s
        for s in db.scalars(select(StreamRegistry).where(StreamRegistry.stream_id.in_(ids)))
    }


def _protected_snapshot():
    with SessionLocal() as db:
        return tool._snapshot_protected(db)


def _run(capsys, *args, stdin: str | None = None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    capsys.readouterr()
    code = tool.main(list(args))
    out = capsys.readouterr()
    data = json.loads(out.out) if code == 0 and out.out.strip() else None
    return code, data, out.err


@pytest.fixture()
def vehicle():
    """测试前置：001 机器人存在（若本测试创建则只清理本测试创建的）；两条流 ID 必须干净。"""
    created_robot = None
    with SessionLocal() as db:
        leftover = _streams(db, RGB, THERMAL)
        assert not leftover, f"shared DB leak: {sorted(leftover)}"
        robot = db.scalar(select(Robot).where(Robot.vehicle_id == VEHICLE))
        if robot is None:
            site = db.scalar(select(Site).order_by(Site.id).limit(1))
            robot = Robot(vehicle_id=VEHICLE, name="firebot-001", site_id=site.id, enabled=False)
            db.add(robot)
            db.commit()
            created_robot = robot.id
        robot_id = robot.id
    yield robot_id
    with SessionLocal() as db:
        db.execute(delete(StreamRegistry).where(StreamRegistry.stream_id.in_([RGB, THERMAL])))
        # 只清理本工具在本测试中为 001 写下的审计行
        db.execute(
            delete(AuditLog).where(
                AuditLog.action.in_([tool.AUDIT_CREATE, tool.AUDIT_DELETE]),
                AuditLog.robot_id == robot_id,
            )
        )
        if created_robot:
            for table in reversed(Base.metadata.sorted_tables):
                if "robot_id" in table.c and table.name != "robots":
                    db.execute(table.delete().where(table.c.robot_id == created_robot))
            db.execute(delete(Robot).where(Robot.id == created_robot))
        db.commit()


def _publish(client, path, *, user="", password="", token=""):
    return client.post(
        "/api/v1/media/authorize",
        json={
            "action": "publish",
            "path": path,
            "protocol": "srt",
            "user": user,
            "password": password,
            "token": token,
        },
    )


def test_missing_robot_is_not_created(capsys, monkeypatch):
    from app.modules.media import registration as core

    probe_id = "firebot-vehicle-01-absent-probe"
    real_load = core.load_registry

    def registry_with_probe():
        data = real_load()
        data["vehicles"] = {**data["vehicles"], probe_id: "r001-independent-v1"}
        return data

    monkeypatch.setattr(core, "load_registry", registry_with_probe)
    monkeypatch.setattr(tool, "TARGET_VEHICLE", probe_id)
    monkeypatch.setattr(tool, "STREAMS", ((f"{probe_id}-roof_rgb", "roof_rgb"),))
    code, _, err = _run(capsys, "--apply")
    assert code == tool.EXIT_PRECONDITION and "不创建机器人" in err, err
    with SessionLocal() as db:
        assert (
            db.scalar(select(Robot).where(Robot.vehicle_id == "firebot-vehicle-01-absent-probe"))
            is None
        )


def test_dry_run_default_writes_nothing(vehicle, capsys):
    before = _protected_snapshot()
    code, data, err = _run(capsys)
    assert code == 0, err
    assert data["mode"] == "DRY_RUN" and data["created"] == []
    assert [a["action"] for a in data["actions"]] == ["CREATE", "CREATE"]
    assert "STREAM_REGISTRATION=DRY_RUN" in err
    with SessionLocal() as db:
        assert _streams(db, RGB, THERMAL) == {}
        assert (
            db.scalar(
                select(AuditLog).where(
                    AuditLog.action == tool.AUDIT_CREATE, AuditLog.robot_id == vehicle
                )
            )
            is None
        )
    assert _protected_snapshot() == before


def test_apply_idempotent_and_authorize_semantics(vehicle, capsys):
    good = vehicle_srt_publish_token(VEHICLE)
    with TestClient(app) as client:
        # 登记前：403 unregistered_path（即使身份正确）
        assert _publish(client, RGB, user=VEHICLE, password=good).status_code == 403
        before = _protected_snapshot()
        code, data, err = _run(capsys, "--apply")
        assert code == 0, err
        assert data["mode"] == "APPLY" and len(data["created"]) == 2
        with SessionLocal() as db:
            rows = _streams(db, RGB, THERMAL)
            assert {r.camera_type for r in rows.values()} == {"roof_rgb", "roof_thermal"}
            for r in rows.values():
                assert r.robot_id == vehicle
                assert r.source_ref == tool.REAL_SOURCE_REF != "SYNTHETIC_TEST"
                assert r.state == "OFFLINE" and r.playback_url == f"/media/{r.stream_id}/whep"
            audits = db.scalars(
                select(AuditLog).where(
                    AuditLog.action == tool.AUDIT_CREATE,
                    AuditLog.resource_id.in_([r.id for r in rows.values()]),
                )
            ).all()
            assert len(audits) == 2
        assert _protected_snapshot() == before
        # 幂等：再执行 → REUSE，零新增
        code, again, err = _run(capsys, "--apply")
        assert code == 0, err
        assert [a["action"] for a in again["actions"]] == ["REUSE", "REUSE"]
        assert again["created"] == []
        # 登记后：只接受 001 车辆绑定身份
        for sid in (RGB, THERMAL):
            assert _publish(client, sid, user=VEHICLE, password=good).status_code == 200
            assert _publish(client, sid, user=VEHICLE, password="WRONG").status_code == 401
            assert (
                _publish(client, sid, user="R002", password=vehicle_srt_publish_token("R002"))
            ).status_code == 401
            from app.core.config import get_settings

            master = get_settings().effective_media_publish_token
            assert _publish(client, sid, token=master).status_code in (401, 403)
            assert _publish(client, sid, password=master).status_code in (401, 403)


@pytest.mark.parametrize("case", ["owned_by_r002", "synthetic_label", "same_camera_other_id"])
def test_conflicts_refused_with_zero_writes(vehicle, capsys, case):
    with SessionLocal() as db:
        r002 = db.scalar(select(Robot).where(Robot.vehicle_id == "R002"))
        if case == "owned_by_r002":
            planted = StreamRegistry(
                stream_id=THERMAL, robot_id=r002.id, camera_type="roof_thermal"
            )
        elif case == "synthetic_label":
            planted = StreamRegistry(
                stream_id=THERMAL,
                robot_id=vehicle,
                camera_type="roof_thermal",
                source_ref="SYNTHETIC_TEST",
            )
        else:
            planted = StreamRegistry(
                stream_id=f"{VEHICLE}-roof_thermal-legacy",
                robot_id=vehicle,
                camera_type="roof_thermal",
            )
        db.add(planted)
        db.commit()
        planted_id, planted_sid = planted.id, planted.stream_id
    before = _protected_snapshot()
    try:
        code, _, err = _run(capsys, "--apply")
        assert code == tool.EXIT_CONFLICT, err
        assert "CONFLICT" in err
        with SessionLocal() as db:
            # 零写入：RGB 也未登记（整体拒绝，不做部分登记）
            assert RGB not in _streams(db, RGB)
            row = db.get(StreamRegistry, planted_id)
            assert row is not None and row.stream_id == planted_sid  # 不覆盖已有行
        assert _protected_snapshot() == before
    finally:
        with SessionLocal() as db:
            db.execute(delete(StreamRegistry).where(StreamRegistry.id == planted_id))
            db.commit()


def test_rollback_only_removes_tool_created_rows(vehicle, capsys, monkeypatch):
    # 撤销前发布会话门：本用例模拟 MediaMTX 确认“无活动发布”（会话门另有专项测试）
    from app.modules.media import registration as core

    monkeypatch.setattr(core, "mediamtx_publisher_probe", lambda ids: dict.fromkeys(ids, False))
    # RGB 预先存在（非本工具创建，source_ref 为空）→ apply 只新增 THERMAL
    with SessionLocal() as db:
        pre = StreamRegistry(stream_id=RGB, robot_id=vehicle, camera_type="roof_rgb")
        db.add(pre)
        db.commit()
        pre_id = pre.id
    code, receipt, err = _run(capsys, "--apply")
    assert code == 0, err
    assert [a["action"] for a in receipt["actions"]] == ["REUSE", "CREATE"]
    assert [c["stream_id"] for c in receipt["created"]] == [THERMAL]
    text = json.dumps(receipt)
    before = _protected_snapshot()

    # 撤销 dry-run：零删除
    code, plan, err = _run(capsys, "--rollback", "-", stdin=text, monkeypatch=monkeypatch)
    assert code == 0, err
    assert plan["mode"] == "DRY_RUN" and [a["action"] for a in plan["actions"]] == ["DELETE"]
    with SessionLocal() as db:
        assert set(_streams(db, RGB, THERMAL)) == {RGB, THERMAL}

    # 篡改 receipt：把 R001 的流 / 预先存在的 RGB 塞进去 → 零删除
    with SessionLocal() as db:
        r001_row = db.scalar(
            select(StreamRegistry).where(StreamRegistry.stream_id == "R001-roof_rgb")
        )
    for forged in (
        {"id": r001_row.id, "stream_id": "R001-roof_rgb"},
        {"id": pre_id, "stream_id": RGB},
    ):
        bad = {**receipt, "created": [*receipt["created"], forged]}
        code, _, err = _run(
            capsys, "--rollback", "-", "--apply", stdin=json.dumps(bad), monkeypatch=monkeypatch
        )
        assert code == tool.EXIT_ROLLBACK, err
        with SessionLocal() as db:
            assert set(_streams(db, RGB, THERMAL)) == {RGB, THERMAL}
            assert db.get(StreamRegistry, r001_row.id) is not None
    # 只有审计证明能区分：同车、同 stream_id、source_ref=REAL_VEHICLE_SRT 但无 REGISTERED 审计
    with SessionLocal() as db:
        db.execute(delete(StreamRegistry).where(StreamRegistry.id == pre_id))
        imposter = StreamRegistry(
            id=pre_id,
            stream_id=RGB,
            robot_id=vehicle,
            camera_type="roof_rgb",
            source_ref=tool.REAL_SOURCE_REF,
        )
        db.add(imposter)
        db.commit()
    bad = {**receipt, "created": [*receipt["created"], {"id": pre_id, "stream_id": RGB}]}
    code, _, err = _run(
        capsys, "--rollback", "-", "--apply", stdin=json.dumps(bad), monkeypatch=monkeypatch
    )
    assert code == tool.EXIT_ROLLBACK and "审计" in err, err
    with SessionLocal() as db:
        assert set(_streams(db, RGB, THERMAL)) == {RGB, THERMAL}
    # 结构非法 / 重复 id 的 receipt：受控拒绝（退出码 5），零删除
    for created in (
        "not-a-list",
        ["not-a-dict"],
        [{"id": 1, "stream_id": THERMAL}],
        [*receipt["created"], *receipt["created"]],
    ):
        code, _, err = _run(
            capsys,
            "--rollback",
            "-",
            "--apply",
            stdin=json.dumps({**receipt, "created": created}),
            monkeypatch=monkeypatch,
        )
        assert code == tool.EXIT_ROLLBACK, (created, err)
    with SessionLocal() as db:
        assert set(_streams(db, RGB, THERMAL)) == {RGB, THERMAL}
    # dry-run receipt 不可用于撤销
    code, _, _ = _run(
        capsys,
        "--rollback",
        "-",
        "--apply",
        stdin=json.dumps({**receipt, "mode": "DRY_RUN"}),
        monkeypatch=monkeypatch,
    )
    assert code == tool.EXIT_ROLLBACK

    # 正式撤销：只删 THERMAL；预先存在的 RGB 保留
    code, done, err = _run(
        capsys, "--rollback", "-", "--apply", stdin=text, monkeypatch=monkeypatch
    )
    assert code == 0, err
    with SessionLocal() as db:
        assert set(_streams(db, RGB, THERMAL)) == {RGB}
        assert db.get(StreamRegistry, pre_id) is not None
        assert (
            db.scalar(
                select(AuditLog).where(
                    AuditLog.action == tool.AUDIT_DELETE,
                    AuditLog.resource_id == receipt["created"][0]["id"],
                )
            )
            is not None
        )
    assert _protected_snapshot() == before
    # 撤销幂等
    code, again, err = _run(
        capsys, "--rollback", "-", "--apply", stdin=text, monkeypatch=monkeypatch
    )
    assert code == 0, err
    assert [a["action"] for a in again["actions"]] == ["ALREADY_ABSENT"]


def test_scope_is_hardcoded_to_001_two_streams():
    assert tool.TARGET_VEHICLE == "firebot-vehicle-01"
    assert tool.STREAMS == (
        ("firebot-vehicle-01-roof_rgb", "roof_rgb"),
        ("firebot-vehicle-01-roof_thermal", "roof_thermal"),
    )
    assert tool.REAL_SOURCE_REF != tool.SYNTHETIC_SOURCE_REF
    # CLI 无任何可指定其他车辆/流的参数
    assert tool.main(["--vehicle", "R002"]) == tool.EXIT_USAGE
    assert tool.main(["--stream", "R002-roof_rgb"]) == tool.EXIT_USAGE


def test_onboard_script_token_accepted_by_authorize(vehicle, capsys, tmp_path, monkeypatch):
    """跨组件对拍：60-onboard-vehicle.sh --media-only 实际产出的 token 能通过真实 authorize。

    密钥文件采用生产格式（末尾换行，同 02-gen-bootstrap-secrets.sh）；API 侧从同一文件读取。
    反证：派生端若按原始字节（含换行）作 HMAC 密钥，此处 401。
    """
    import shutil
    import subprocess
    from pathlib import Path

    from app.core.config import get_settings

    if shutil.which("python3") is None:
        pytest.skip("python3 不可用")
    repo = Path(__file__).resolve().parents[3]
    runtime = tmp_path / "runtime"
    (runtime / "secrets/app").mkdir(parents=True)
    master_file = runtime / "secrets/app/media_publish_token"
    master_file.write_text("g4r1-crosscheck-master-0123456789abcdef+/=\n")
    (runtime / "secrets/app/srt_publish_passphrase").write_text("g4r1-pass\n")
    res = subprocess.run(
        ["bash", str(repo / "deploy/u-dao/scripts/60-onboard-vehicle.sh"), VEHICLE, "--media-only"],
        env={"PATH": "/usr/bin:/bin", "U_DAO_RUNTIME_ROOT": str(runtime)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert res.returncode == 0, res.stderr[-300:]
    token = (runtime / "secrets/vehicles" / VEHICLE / "srt_publish_token").read_text()
    code, _, err = _run(capsys, "--apply")
    assert code == 0, err
    settings = get_settings()
    monkeypatch.setattr(settings, "media_publish_token_file", str(master_file))
    with TestClient(app) as client:
        assert _publish(client, RGB, user=VEHICLE, password=token).status_code == 200
        assert _publish(client, THERMAL, user=VEHICLE, password=token).status_code == 200
